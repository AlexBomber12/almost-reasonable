"""Synthetic EventStore erasure, restart and quarantine integration."""

from dataclasses import replace
import sqlite3

import pytest

from app.core.agent_output_store import AgentOutputStore
from app.core.events import EventAudience
from app.core.self_state_store import SelfStateStore
from app.core.store import (
    AppendStatus,
    EventStore,
    EventStoreError,
    EventValidationError,
)
from app.privacy import DeletionCoordinator, DeletionDisposition, PrivacyLedger
from app.privacy.application import (
    EventStoreErasureAdapter,
    EventStoreRestoreAdapter,
    PrivacyReadGate,
    PrivacyReadBlocked,
)
from app.privacy.recovery import QuarantinedRestore, create_sqlite_backup
from test_agent_output_store import operation, persist
from test_event_store import principal, private_input
from test_privacy_deletion import NOW, context, request
from test_self_state import interest_change, belief_change, goal_change

CANARY = "synthetic private orbital canary"


@pytest.fixture
def setup(tmp_path):
    store = EventStore(tmp_path / "events.db")
    for name in ("a", "a2", "b"):
        who = "b" if name == "b" else "a"
        store.append_private_input(
            principal(who),
            private_input(
                name,
                CANARY if name == "a" else "unrelated synthetic input",
                audience=EventAudience.private_for(principal(who).principal_id),
            ),
        )
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    adapter = EventStoreErasureAdapter(store)
    return (
        store,
        ledger,
        adapter,
        DeletionCoordinator(ledger, adapter, clock=lambda: NOW),
    )


def deletion(**changes):
    return request(content_reference="event-a", **changes)


def test_real_cascade_history_multisource_replay_and_isolation(setup):
    store, ledger, adapter, coordinator = setup
    state = SelfStateStore(store)
    changes = []
    for kind, factory in (
        ("interest", interest_change),
        ("belief", belief_change),
        ("goal", goal_change),
    ):
        change = factory("event-a")
        fields = {
            "interest": {"topic": CANARY, "reason_for_interest": CANARY},
            "belief": {"proposition": CANARY, "alternatives": (CANARY,)},
            "goal": {
                "description": CANARY,
                "rationale": CANARY,
                "constraints": (CANARY,),
                "review_condition": CANARY,
            },
        }[kind]
        change = replace(
            change,
            **fields,
            supporting_event_ids=("event-a", "event-a2"),
            revision=replace(change.revision, reason=CANARY),
        )
        changes.append((kind, change))
        getattr(state, f"create_{kind}_candidate")(principal("a"), change)
        later = factory("event-a2", operation=f"{kind}-later", expected=1)
        later = replace(
            later,
            **{"strength": 80}
            if kind == "interest"
            else {"confidence": 0.8}
            if kind == "belief"
            else {"priority": 80},
        )
        getattr(state, f"revise_{kind}_candidate")(principal("a"), later)
        changes.append((kind, later))
    state.create_interest_candidate(principal("b"), interest_change("event-b"))
    outputs = AgentOutputStore(store)
    op = operation(source_event_ids=("event-a", "event-a2"))
    outputs.reserve_reply(principal("a"), op)
    persist(outputs, op, content=CANARY)
    before_b = store.retrieve_private_events(principal("b"))
    assert (
        coordinator.delete(context(), deletion()).disposition
        is DeletionDisposition.APPLIED
    )
    assert adapter.erase(deletion()).blockers == ()
    assert (
        coordinator.delete(context(), deletion()).disposition
        is DeletionDisposition.APPLIED
    )
    assert [e.event_id for e in store.retrieve_private_events(principal("a"))] == [
        "event-a2"
    ]
    assert store.retrieve_private_events(principal("b")) == before_b
    assert state.retrieve_private_candidates(principal("a")) == ()
    assert len(state.retrieve_private_candidates(principal("b"))) == 1
    for kind, change in changes:
        method = "create" if change.revision.expected_revision is None else "revise"
        with pytest.raises(ValueError):
            getattr(state, f"{method}_{kind}_candidate")(principal("a"), change)
        if method == "revise":
            fresh = replace(
                change,
                revision=replace(
                    change.revision,
                    operation_id=f"resurrect-{kind}",
                    expected_revision=2,
                ),
            )
            with pytest.raises((ValueError, EventStoreError)):
                getattr(state, f"revise_{kind}_candidate")(principal("a"), fresh)
    assert outputs.read_reply(principal("a"), op.operation_id) is None
    with pytest.raises(EventValidationError):
        outputs.reserve_reply(principal("a"), op)
    with pytest.raises(EventValidationError):
        persist(outputs, op, content=CANARY)
    assert (
        store.append_private_input(principal("a"), private_input("a", CANARY)).status
        is AppendStatus.DUPLICATE
    )
    with sqlite3.connect(store.database_path) as db:
        assert db.execute(
            "SELECT payload_id FROM event_envelopes WHERE event_id='event-a'"
        ).fetchone() == (None,)
        assert db.execute(
            "SELECT count(*) FROM state_revision_payloads"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM state_payload_alternatives"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM state_payload_constraints"
        ).fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM agent_reply_payloads").fetchone() == (
            0,
        )
        assert CANARY not in "\n".join(db.iterdump())
    assert CANARY not in repr(ledger.audit_records())


def test_cross_principal_exact_identity_and_conflicting_operations(setup):
    store, _, adapter, coordinator = setup
    original = store.retrieve_private_events(principal("a"))
    wrong = deletion(principal_id="participant-b")
    assert adapter.erase(wrong).blockers == ("source_ownership_mismatch",)
    assert (
        coordinator.delete(context(), wrong).disposition
        is DeletionDisposition.INVALID_AUTHORIZATION
    )
    assert (
        coordinator.delete(context("participant-b"), wrong).disposition
        is DeletionDisposition.INCOMPLETE_BLOCKED
    )
    assert store.retrieve_private_events(principal("a")) == original
    with pytest.raises(ValueError, match="identity conflict"):
        coordinator.delete(
            context("participant-b"), replace(wrong, content_reference="event-b")
        )


def test_old_wal_backup_replayed_in_quarantine(setup, tmp_path):
    store, ledger, _, coordinator = setup
    state = SelfStateStore(store)
    state.create_goal_candidate(principal("a"), goal_change("event-a"))
    outputs = AgentOutputStore(store)
    outputs.reserve_reply(principal("a"), operation())
    persist(outputs, content=CANARY)
    writer = sqlite3.connect(store.database_path)
    try:
        backup = tmp_path / "old.db"
        create_sqlite_backup(store.database_path, backup, 6, NOW)
        assert (
            coordinator.delete(context(), deletion()).disposition
            is DeletionDisposition.APPLIED
        )
        quarantine = tmp_path / "quarantine.db"
        result = QuarantinedRestore(ledger, 6, EventStoreRestoreAdapter).restore(
            backup, quarantine, NOW
        )
        assert result.activation_eligible
        with sqlite3.connect(f"file:{quarantine}?immutable=1", uri=True) as db:
            assert CANARY not in "\n".join(db.iterdump())
            assert db.execute("SELECT count(*) FROM private_payloads").fetchone() == (
                2,
            )
        assert CANARY.encode() not in quarantine.read_bytes()
        assert quarantine.stat().st_mode & 0o777 == 0o400
    finally:
        writer.close()


def test_startup_gate_closed_pending_failed_and_reconciled(setup):
    store, ledger, adapter, coordinator = setup
    gate = PrivacyReadGate(ledger, store)
    with pytest.raises(PrivacyReadBlocked):
        gate.retrieve_private_events(principal("a"))
    assert gate.reconcile()
    assert len(gate.retrieve_private_events(principal("a"))) == 2

    def crash(_):
        raise SystemExit

    with pytest.raises(SystemExit):
        coordinator.delete(context(), deletion(), after_pending=crash)
    with pytest.raises(PrivacyReadBlocked):
        gate.retrieve_private_events(principal("a"))
    assert gate.reconcile()
    assert [e.event_id for e in gate.retrieve_private_events(principal("a"))] == [
        "event-a2"
    ]
    assert adapter.erase(deletion()).tombstone_written


@pytest.mark.parametrize(
    "sql,reason",
    [
        (
            "UPDATE state_items SET scope='shared_agent', audience_type='shared_agent', audience_principal_id=''",
            "unsupported_shared_state",
        ),
        (
            "DELETE FROM protected_event_taints WHERE event_id='event-a'",
            "invalid_event_provenance",
        ),
        (
            "UPDATE protected_event_taints SET source_event_id='event-a2' WHERE event_id='event-a'",
            "invalid_event_provenance",
        ),
        (
            "UPDATE private_payloads SET audience_principal_id='participant-b' WHERE payload_id='payload-a'",
            "invalid_private_payload",
        ),
        ("UPDATE state_items SET current_revision=2", "invalid_state_chain"),
        (
            "UPDATE state_operations SET audience_principal_id='participant-b'",
            "invalid_state_operation",
        ),
        (
            "DELETE FROM protected_state_provenance WHERE evidence_role='initiating'",
            "missing_state_provenance",
        ),
        (
            "DELETE FROM protected_state_provenance WHERE evidence_role='supporting'",
            "missing_state_provenance",
        ),
        (
            "UPDATE protected_state_provenance SET source_principal_id='participant-b'",
            "invalid_state_provenance",
        ),
        ("DELETE FROM state_revision_payloads", "invalid_state_payload"),
        ("DELETE FROM agent_reply_sources", "missing_reply_provenance"),
        (
            "UPDATE agent_reply_sources SET source_principal_id='participant-b'",
            "invalid_reply_provenance",
        ),
        ("DELETE FROM agent_reply_payloads", "invalid_reply_payload"),
        (
            "UPDATE private_payloads SET content='' WHERE payload_id='payload-a'",
            "application_integrity_failure",
        ),
        (
            "UPDATE agent_reply_sources SET event_id='event-missing'",
            "orphaned_application_data",
        ),
    ],
)
def test_missing_corrupt_or_unsupported_graph_is_blocked_without_mutation(
    setup, sql, reason
):
    store, ledger, adapter, coordinator = setup
    SelfStateStore(store).create_interest_candidate(
        principal("a"), interest_change("event-a")
    )
    outputs = AgentOutputStore(store)
    outputs.reserve_reply(principal("a"), operation())
    persist(outputs, content=CANARY)
    with sqlite3.connect(store.database_path) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(sql)
        before = "\n".join(db.iterdump())
    report = adapter.erase(deletion())
    assert report.blockers == (reason,)
    assert not report.payload_removed
    assert (
        coordinator.delete(context(), deletion()).disposition
        is DeletionDisposition.INCOMPLETE_BLOCKED
    )
    with sqlite3.connect(store.database_path) as db:
        assert "\n".join(db.iterdump()) == before
    assert CANARY not in repr(ledger.audit_records())
    gate = PrivacyReadGate(ledger, store)
    assert not gate.reconcile()
    with pytest.raises(PrivacyReadBlocked):
        gate.retrieve_private_events(principal("a"))


def test_unknown_schema_missing_source_and_unsupported_category(setup):
    from app.privacy import ContentCategory

    store, _, adapter, coordinator = setup
    assert adapter.erase(
        replace(deletion(), content_reference="event-missing")
    ).blockers == ("source_identity_missing",)
    assert adapter.erase(
        deletion(content_category=ContentCategory.REVISION)
    ).blockers == ("unsupported_source_category",)
    with sqlite3.connect(store.database_path) as db:
        db.execute("CREATE TABLE unsupported_private_cache(content TEXT)")
    assert (
        coordinator.delete(context(), deletion()).disposition
        is DeletionDisposition.INCOMPLETE_BLOCKED
    )
    assert not PrivacyReadGate(coordinator.ledger, store).reconcile()


@pytest.mark.parametrize(
    "point", ["before-commit", "after-commit", "ledger-update", "after-ledger-update"]
)
def test_crash_restarts_pending_first_without_authoritative_early_success(
    setup, monkeypatch, point
):
    from app.privacy import application

    store, ledger, adapter, coordinator = setup
    state = SelfStateStore(store)
    state.create_goal_candidate(principal("a"), goal_change("event-a"))
    if point == "before-commit":
        original = application._validate_graph
        calls = 0

        def fail(db, schema_version):
            nonlocal calls
            calls += 1
            original(db, schema_version)
            if calls == 2:
                raise SystemExit

        monkeypatch.setattr(application, "_validate_graph", fail)
    elif point == "after-commit":
        original = adapter.erase

        def fail(req):
            original(req)
            raise SystemExit

        monkeypatch.setattr(adapter, "erase", fail)
    else:
        original = ledger._record_verified_deletion

        def fail(*args):
            if point == "after-ledger-update":
                original(*args)
            raise SystemExit

        monkeypatch.setattr(ledger, "_record_verified_deletion", fail)
    with pytest.raises(SystemExit):
        coordinator.delete(context(), deletion())
    assert ledger.deletion(deletion().operation_id).disposition is (
        DeletionDisposition.APPLIED
        if point == "after-ledger-update"
        else DeletionDisposition.PENDING
    )
    if point == "before-commit":
        assert len(store.retrieve_private_events(principal("a"))) == 2
        assert len(state.retrieve_private_candidates(principal("a"))) == 1
    else:
        assert len(store.retrieve_private_events(principal("a"))) == 1
        assert state.retrieve_private_candidates(principal("a")) == ()
    monkeypatch.undo()
    restarted = PrivacyReadGate(
        PrivacyLedger(store.database_path.parent / "ledger.db"),
        EventStore(store.database_path),
    )
    assert restarted.reconcile()
    assert len(restarted.retrieve_private_events(principal("a"))) == 1
    assert (
        ledger.deletion(deletion().operation_id).disposition
        is DeletionDisposition.APPLIED
    )


def test_ordinary_adapter_failure_has_sanitized_audit_and_read_block(
    setup, monkeypatch
):
    store, ledger, adapter, coordinator = setup

    def fail(_):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(adapter, "erase", fail)
    assert (
        coordinator.delete(context(), deletion()).disposition
        is DeletionDisposition.INCOMPLETE_BLOCKED
    )
    assert CANARY not in repr(ledger.audit_records())
    # New adapter retries against the real store; no payload is lost on failure.
    assert PrivacyReadGate(ledger, store).reconcile()


def test_gate_all_read_paths_and_history_taint(setup):
    from app.core.self_state import StateType

    store, ledger, _, coordinator = setup
    gate = PrivacyReadGate(ledger, store)
    state = SelfStateStore(store)
    change = interest_change("event-a")
    state.create_interest_candidate(principal("a"), change)
    outputs = AgentOutputStore(store)
    outputs.reserve_reply(principal("a"), operation())
    persist(outputs, content=CANARY)
    assert gate.reconcile()
    assert len(gate.retrieve_private_candidates(principal("a"))) == 1
    assert (
        len(
            gate.retrieve_private_revision_history(
                principal("a"), StateType.INTEREST, change.item_id
            )
        )
        == 1
    )
    assert (
        len(
            gate.inspect_private_revision_evidence(
                principal("a"), StateType.INTEREST, change.item_id, 1
            )
        )
        == 2
    )
    assert gate.read_reply(principal("a"), "reply-1").content == CANARY
    coordinator.delete(context(), deletion())
    for read in (
        lambda: gate.retrieve_private_candidates(principal("a")),
        lambda: gate.retrieve_private_revision_history(
            principal("a"), StateType.INTEREST, change.item_id
        ),
        lambda: gate.inspect_private_revision_evidence(
            principal("a"), StateType.INTEREST, change.item_id, 1
        ),
        lambda: gate.read_reply(principal("a"), "reply-1"),
    ):
        with pytest.raises(PrivacyReadBlocked):
            read()
    assert gate.reconcile()
    assert gate.retrieve_private_candidates(principal("a")) == ()
    assert (
        gate.retrieve_private_revision_history(
            principal("a"), StateType.INTEREST, change.item_id
        )
        == ()
    )
    assert (
        gate.inspect_private_revision_evidence(
            principal("a"), StateType.INTEREST, change.item_id, 1
        )
        == ()
    )
    assert gate.read_reply(principal("a"), "reply-1") is None


def test_duplicate_source_identity_cannot_recreate_erased_payload(setup):
    store, _, _, coordinator = setup
    original = private_input("a", CANARY)
    assert (
        coordinator.delete(context(), deletion()).disposition
        is DeletionDisposition.APPLIED
    )
    replay = replace(original, event_id="event-retry", payload_id="payload-retry")
    assert (
        store.append_private_input(principal("a"), replay).status
        is AppendStatus.DUPLICATE
    )
    second = deletion(operation_id="delete-again")
    assert (
        coordinator.delete(context(), second).disposition is DeletionDisposition.APPLIED
    )
    with sqlite3.connect(store.database_path) as db:
        assert (
            db.execute(
                "SELECT 1 FROM private_payloads WHERE payload_id='payload-retry'"
            ).fetchone()
            is None
        )


def test_current_revocation_no_application_consent_cache_and_released_writer(
    setup, tmp_path
):
    from app.privacy import ConsentService
    from test_privacy_consent import grant

    store, ledger, _, coordinator = setup
    consent = replace(
        grant(), content_reference="event-a", authentication_event_id="auth-a"
    )
    service = ConsentService(ledger, clock=lambda: NOW)
    assert service.grant(context(), consent).created
    service.revoke(
        context(),
        consent_id=consent.consent_id,
        operation_id="revoke-1",
        revoked_at=NOW,
        correlation_id="revoke-correlation",
    )
    backup = tmp_path / "revoked-backup.db"
    create_sqlite_backup(store.database_path, backup, 6, NOW)
    adapters = []

    class TrackingAdapter(EventStoreRestoreAdapter):
        def apply_revocation(self, consent_id, revoked_at):
            assert (consent_id, revoked_at) == (consent.consent_id, NOW)
            return super().apply_revocation(consent_id, revoked_at)

    def factory(path):
        adapter = TrackingAdapter(path)
        adapters.append(adapter)
        return adapter

    assert (
        QuarantinedRestore(ledger, 6, factory)
        .restore(backup, tmp_path / "revoked-quarantine.db", NOW)
        .activation_eligible
    )
    from app.privacy.application import ApplicationPrivacyError

    with pytest.raises(ApplicationPrivacyError, match="released"):
        adapters[0].rebuild_dependencies()
    assert adapters[0].release_writers()
    # No database-switch interface exists and the current database still serves
    # its original rows. Quarantine eligibility cannot authorize a live switch.
    assert not hasattr(adapters[0], "activate")
    assert not hasattr(QuarantinedRestore, "activate")
    assert len(store.retrieve_private_events(principal("a"))) == 2
    assert (
        coordinator.delete(context(), deletion()).disposition
        is DeletionDisposition.APPLIED
    )


@pytest.mark.parametrize(
    "failure", ["provenance", "revocation", "rebuild", "incomplete"]
)
def test_real_quarantine_failures_never_become_ready_or_apply_live_ledger(
    setup, tmp_path, failure
):
    from app.privacy import ConsentService, ErasureReport
    from test_privacy_consent import grant

    store, ledger, _, coordinator = setup
    backup = tmp_path / "failure-backup.db"
    if failure == "provenance":
        with sqlite3.connect(store.database_path) as db:
            db.execute("DELETE FROM protected_event_taints WHERE event_id='event-a'")
    create_sqlite_backup(store.database_path, backup, 6, NOW)

    def crash(_):
        raise SystemExit

    with pytest.raises(SystemExit):
        coordinator.delete(context(), deletion(), after_pending=crash)
    if failure == "revocation":
        service = ConsentService(ledger, clock=lambda: NOW)
        consent = grant(authentication_event_id="auth-a")
        service.grant(context(), consent)
        service.revoke(
            context(),
            consent_id=consent.consent_id,
            operation_id="revoke-1",
            revoked_at=NOW,
            correlation_id="revoke-correlation",
        )

    class FailingAdapter(EventStoreRestoreAdapter):
        def apply_deletion(self, record):
            report = super().apply_deletion(record)
            if failure == "incomplete":
                return ErasureReport(
                    True, True, frozenset(), ("unsupported_future_category",)
                )
            return report

        def apply_revocation(self, consent_id, revoked_at):
            super().apply_revocation(consent_id, revoked_at)
            return False

        def rebuild_dependencies(self):
            super().rebuild_dependencies()
            return failure != "rebuild"

    quarantine = tmp_path / "blocked-quarantine.db"
    result = QuarantinedRestore(ledger, 6, FailingAdapter).restore(
        backup, quarantine, NOW
    )
    assert not result.activation_eligible
    assert not quarantine.exists()
    assert not quarantine.with_suffix(".db.ready.json").exists()
    assert (
        ledger.deletion(deletion().operation_id).disposition
        is DeletionDisposition.PENDING
    )


def test_gate_failure_and_generation_race_remain_closed(setup, monkeypatch):
    store, ledger, _, _ = setup
    gate = PrivacyReadGate(ledger, store)
    original = ledger.control_generation
    monkeypatch.setattr(ledger, "control_generation", lambda: original() + 1)
    assert not gate.reconcile()
    with pytest.raises(PrivacyReadBlocked):
        gate.retrieve_private_events(principal("a"))
    monkeypatch.undo()
    with sqlite3.connect(store.database_path) as db:
        db.execute("DELETE FROM protected_event_taints WHERE event_id='event-a'")
    assert not gate.reconcile()
    with pytest.raises(PrivacyReadBlocked):
        gate.retrieve_private_events(principal("a"))


def test_concurrent_erasure_and_retrieval_order_at_durable_intent(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    store, ledger, _, coordinator = setup
    gate = PrivacyReadGate(ledger, store)
    assert gate.reconcile()
    reading, finish_read, pending, finish_erasure = (Event() for _ in range(4))
    original = store.retrieve_private_events

    def held_read(ctx):
        reading.set()
        assert finish_read.wait(5)
        return original(ctx)

    monkeypatch.setattr(store, "retrieve_private_events", held_read)

    def held_pending(_):
        pending.set()
        assert finish_erasure.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(gate.retrieve_private_events, principal("a"))
        assert reading.wait(5)
        erase = pool.submit(
            coordinator.delete, context(), deletion(), after_pending=held_pending
        )
        # Reader owns the ledger fence: an erasure cannot commit its pending
        # intent until this preceding read has finished.
        assert not pending.is_set()
        finish_read.set()
        assert len(read.result(5)) == 2
        assert pending.wait(5)
        with pytest.raises(PrivacyReadBlocked):
            gate.retrieve_private_events(principal("a"))
        finish_erasure.set()
        assert erase.result(5).disposition is DeletionDisposition.APPLIED
    assert gate.reconcile()
    assert len(gate.retrieve_private_events(principal("a"))) == 1


def test_reconcile_applied_record_against_stale_live_copy_fails_closed(setup, tmp_path):
    # A startup boundary replays APPLIED records too; terminal live-ledger state
    # alone never proves the configured EventStore has been reconciled.
    store, ledger, _, coordinator = setup
    backup = tmp_path / "startup-backup.db"
    create_sqlite_backup(store.database_path, backup, 6, NOW)
    assert (
        coordinator.delete(context(), deletion()).disposition
        is DeletionDisposition.APPLIED
    )
    # This is a synthetic alternate store, never a live database switch.
    import shutil

    alternate = tmp_path / "alternate.db"
    shutil.copyfile(backup, alternate)
    gate = PrivacyReadGate(ledger, EventStore(alternate))
    with pytest.raises(PrivacyReadBlocked):
        gate.retrieve_private_events(principal("a"))
    assert gate.reconcile()
    assert len(gate.retrieve_private_events(principal("a"))) == 1
    assert (
        ledger.deletion(deletion().operation_id).disposition
        is DeletionDisposition.APPLIED
    )


def test_unrelated_same_principal_state_and_reply_preserved(setup):
    store, _, _, coordinator = setup
    state = SelfStateStore(store)
    change = interest_change("event-a2")
    before = state.create_interest_candidate(principal("a"), change).state
    outputs = AgentOutputStore(store)
    op = operation(
        operation_id="unrelated-reply",
        initiating_event_id="event-a2",
        correlation_id="correlation-a2",
        source_event_ids=("event-a2",),
    )
    pending = outputs.reserve_reply(principal("a"), op)
    assert (
        coordinator.delete(context(), deletion()).disposition
        is DeletionDisposition.APPLIED
    )
    assert state.retrieve_private_candidates(principal("a")) == (before,)
    assert outputs.read_reply(principal("a"), op.operation_id) == pending


def test_counterevidence_source_deletion_taints_whole_item(setup):
    store, _, _, coordinator = setup
    state = SelfStateStore(store)
    change = belief_change(
        "event-a2", supporting=("event-a2",), conflicting=("event-a",)
    )
    state.create_belief_candidate(principal("a"), change)
    assert (
        coordinator.delete(context(), deletion()).disposition
        is DeletionDisposition.APPLIED
    )
    assert state.retrieve_private_candidates(principal("a")) == ()
    with pytest.raises(ValueError):
        state.create_belief_candidate(principal("a"), change)


def test_applied_ledger_does_not_open_gate_with_unverifiable_application_data(setup):
    store, ledger, _, coordinator = setup
    assert (
        coordinator.delete(context(), deletion()).disposition
        is DeletionDisposition.APPLIED
    )
    with sqlite3.connect(store.database_path) as db:
        db.execute("DELETE FROM protected_event_taints WHERE event_id='event-a'")
    gate = PrivacyReadGate(ledger, store)
    assert not gate.reconcile()
    with pytest.raises(PrivacyReadBlocked):
        gate.retrieve_private_events(principal("a"))
    assert (
        ledger.deletion(deletion().operation_id).disposition
        is DeletionDisposition.APPLIED
    )


def test_restore_missing_identity_is_blocked_instead_of_fabricated_tombstone(
    setup, tmp_path
):
    store, ledger, _, coordinator = setup
    backup = tmp_path / "before-source.db"
    create_sqlite_backup(store.database_path, backup, 6, NOW)
    store.append_private_input(principal("a"), private_input("later", CANARY))
    assert (
        coordinator.delete(
            context(), replace(deletion(), content_reference="event-later")
        ).disposition
        is DeletionDisposition.APPLIED
    )
    result = QuarantinedRestore(ledger, 6, EventStoreRestoreAdapter).restore(
        backup, tmp_path / "missing-identity.db", NOW
    )
    assert not result.activation_eligible
    assert result.reason == "deletion_adapter_failure"


def test_gate_readiness_bound_to_reconciled_database_inode(setup, tmp_path):
    import os
    import shutil

    store, ledger, _, _ = setup
    gate = PrivacyReadGate(ledger, store)
    assert gate.reconcile()
    backup = tmp_path / "alternate-backup.db"
    create_sqlite_backup(store.database_path, backup, 6, NOW)
    # No restore activation occurs. A synthetic inode replacement must invalidate
    # an already reconciled gate even when the ledger generation is unchanged.
    replacement = tmp_path / "replacement.db"
    shutil.copyfile(backup, replacement)
    os.chmod(replacement, 0o600)
    os.replace(replacement, store.database_path)
    with pytest.raises(PrivacyReadBlocked):
        gate.retrieve_private_events(principal("a"))


def test_gate_discards_read_when_database_inode_changes_during_retrieval(
    setup, tmp_path, monkeypatch
):
    import os
    import shutil

    store, ledger, _, _ = setup
    gate = PrivacyReadGate(ledger, store)
    assert gate.reconcile()
    backup = tmp_path / "during-read-backup.db"
    create_sqlite_backup(store.database_path, backup, 6, NOW)
    original = store.retrieve_private_events

    def swapped(ctx):
        events = original(ctx)
        replacement = tmp_path / "during-read.db"
        shutil.copyfile(backup, replacement)
        os.chmod(replacement, 0o600)
        os.replace(replacement, store.database_path)
        return events

    monkeypatch.setattr(store, "retrieve_private_events", swapped)
    with pytest.raises(PrivacyReadBlocked, match="application_database_changed"):
        gate.retrieve_private_events(principal("a"))
