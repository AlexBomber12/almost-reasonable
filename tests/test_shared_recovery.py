"""Real v6 migration rollback, stale backups, and ordered privacy races."""

from dataclasses import replace
import sqlite3
from threading import Event, Thread
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.cognition.shared import SharedIntelligence
from app.core.store import EventStore, FaultPoint, schema_objects
from app.privacy.application import EventStoreRestoreAdapter, PrivacyReadBlocked
from app.privacy.recovery import (
    create_sqlite_backup,
    QuarantinedRestore,
    _schema_version,
)
import test_shared_intelligence as shared_tests
from test_shared_intelligence import approval


from test_cognition_pipeline import principal, NOW
from test_privacy_deletion import context, request
from app.privacy.application import EventStoreErasureAdapter
from app.privacy.deletion import DeletionCoordinator
from shared_schema_helpers import remove_v6


@pytest.fixture
def shared(tmp_path):
    return shared_tests.shared.__wrapped__(tmp_path)


def test_populated_genuine_v5_atomic_upgrade_and_interruption(shared, tmp_path):
    events, _, _, _, _ = shared
    with sqlite3.connect(events.database_path) as db:
        remove_v6(db)
        db.execute("UPDATE schema_metadata SET version=5")
        db.execute("PRAGMA user_version=5")
        before = schema_objects(db)
        original = db.execute("SELECT * FROM event_envelopes").fetchall()
    create_sqlite_backup(events.database_path, tmp_path / "v5-backup.db", 5, NOW)

    def crash(point):
        if point is FaultPoint.BEFORE_V6_MIGRATION_COMMIT:
            raise RuntimeError("synthetic v6 interruption")

    with pytest.raises(RuntimeError):
        EventStore(events.database_path, fault_injector=crash)
    with sqlite3.connect(events.database_path) as db:
        assert schema_objects(db) == before
        assert db.execute("PRAGMA user_version").fetchone() == (5,)
        assert db.execute("SELECT version FROM schema_metadata").fetchone() == (5,)
        assert db.execute("SELECT * FROM event_envelopes").fetchall() == original
    upgraded = EventStore(events.database_path)
    assert upgraded.diagnostics().schema_version == 8
    with upgraded._open() as db:
        assert db.execute("SELECT * FROM event_envelopes").fetchall() == original


@pytest.mark.parametrize("deleted", [False, True])
def test_v6_old_backup_shared_payloads_obey_later_deletion(shared, tmp_path, deleted):
    events, ledger, gate, a, b = shared
    one = approval(a)
    app = SharedIntelligence(events, gate, approvals=(one,), clock=lambda: NOW)
    app.commit(one)
    from app.private_web.runtime import Stage0ConversationRuntime
    from app.trusted.openai_transport import FakeOpenAITransport
    from test_private_conversation_runtime import response

    Stage0ConversationRuntime(
        events,
        gate,
        FakeOpenAITransport([response()]),
        shared_intelligence=app,
        clock=lambda: NOW,
    ).submit(principal("b"), "received", "Обсудим вопросы")
    backup = tmp_path / "v6-backup.db"
    create_sqlite_backup(events.database_path, backup, 8, NOW)
    if deleted:
        DeletionCoordinator(ledger, EventStoreErasureAdapter(events)).delete(
            context(), request(content_reference=a)
        )
    quarantine = tmp_path / "quarantine.db"
    restored = QuarantinedRestore(ledger, 8, EventStoreRestoreAdapter).restore(
        backup, quarantine, NOW
    )
    assert restored.activation_eligible
    assert _schema_version(quarantine) == 8
    with sqlite3.connect(f"file:{quarantine}?immutable=1", uri=True) as db:
        assert db.execute("SELECT COUNT(*) FROM shared_payloads").fetchone() == (
            0 if deleted else 1,
        )
        assert db.execute(
            "SELECT COUNT(*) FROM protected_shared_manifests"
        ).fetchone() == (0 if deleted else 1,)
        assert db.execute("SELECT COUNT(*) FROM agent_reply_payloads").fetchone() == (
            0 if deleted else 1,
        )
    assert quarantine.stat().st_mode & 0o777 == 0o400


def test_competing_shared_commit_and_exact_duplicates_are_serialized(shared):
    events, _, gate, a, _ = shared
    one = approval(a)
    other = replace(one, proposal=replace(one.proposal, operation_id="competing"))
    app = SharedIntelligence(events, gate, approvals=(one, other), clock=lambda: NOW)

    def commit(a):
        try:
            return app.commit(a)
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(commit, (one, other)))
    assert results.count("conflict") == 1
    winner = one if results[0] != "conflict" else other
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert (
            list(pool.map(commit, (winner, winner)))
            == [winner.proposal.operation_id] * 2
        )
    with events._open() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM shared_operations WHERE status='active'"
        ).fetchone() == (1,)


def test_deletion_during_screen_never_promotes(shared):
    from app.cognition.sharing import screen_abstract_candidate

    events, ledger, gate, a, _ = shared
    one = approval(a)
    screened = Event()
    resume = Event()

    def screen(candidate, sources):
        screened.set()
        assert resume.wait(5)
        return screen_abstract_candidate(candidate, sources)

    app = SharedIntelligence(
        events, gate, approvals=(one,), clock=lambda: NOW, privacy_screen=screen
    )
    outcomes = []

    def promote():
        try:
            outcomes.append(app.commit(one))
        except (ValueError, PrivacyReadBlocked):
            outcomes.append("blocked")

    worker = Thread(target=promote)
    worker.start()
    assert screened.wait(5)
    # A pending intent waits for the short ledger read fence. Once the H screen
    # returns, either the commit is ordered before intent and then erased, or the
    # transition is blocked. Neither outcome leaves readable deleted material.
    deletion = Thread(
        target=lambda: DeletionCoordinator(
            ledger, EventStoreErasureAdapter(events)
        ).delete(context(), request(content_reference=a))
    )
    deletion.start()
    resume.set()
    worker.join(5)
    deletion.join(5)
    assert not worker.is_alive() and not deletion.is_alive()
    assert gate.reconcile()
    assert app.context(principal("b")) == ()
    with events._open() as db:
        assert db.execute("SELECT * FROM shared_payloads").fetchall() == []


def test_held_shared_reply_after_model_crash_is_not_repeated_or_released(shared):
    from app.private_web.runtime import Stage0ConversationRuntime
    from app.trusted.openai_transport import FakeOpenAITransport
    from test_private_conversation_runtime import response

    events, ledger, gate, a, _ = shared
    one = approval(a)
    app = SharedIntelligence(events, gate, approvals=(one,), clock=lambda: NOW)
    app.commit(one)
    fake = FakeOpenAITransport([response()])

    def crash(point):
        if point == "after_model":
            raise RuntimeError("synthetic shared inference interruption")

    runtime = Stage0ConversationRuntime(
        events, gate, fake, shared_intelligence=app, clock=lambda: NOW, checkpoint=crash
    )
    with pytest.raises(RuntimeError):
        runtime.submit(principal("b"), "interrupted", "Продолжим")
    fresh = Stage0ConversationRuntime(
        events, gate, fake, shared_intelligence=app, clock=lambda: NOW
    )
    retry = fresh.submit(principal("b"), "interrupted", "Продолжим")
    assert retry.model_state.value == "pending" and retry.response_content is None
    assert len(fake.calls) == 1
    DeletionCoordinator(ledger, EventStoreErasureAdapter(events)).delete(
        context(), request(content_reference=a)
    )
    assert gate.reconcile()
    assert all(r.role == "participant" for r in fresh.history(principal("b")))


@pytest.mark.parametrize(
    "point,active",
    [
        ("after_shared_screen", False),
        ("before_shared_commit", False),
        ("after_shared_commit", True),
    ],
)
def test_shared_transition_crash_recovers_exact_operation(shared, point, active):
    events, _, gate, a, _ = shared
    one = approval(a)

    def crash(checkpoint):
        if checkpoint == point:
            raise RuntimeError("synthetic transition interruption")

    app = SharedIntelligence(
        events, gate, approvals=(one,), clock=lambda: NOW, checkpoint=crash
    )
    with pytest.raises(RuntimeError):
        app.commit(one)
    recovered = SharedIntelligence(events, gate, approvals=(one,), clock=lambda: NOW)
    assert bool(recovered.context(principal("b"))) == active
    assert recovered.commit(one) == one.proposal.operation_id
    with events._open() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_operations").fetchone() == (1,)


def test_delete_after_durable_screen_intent_blocks_promotion(shared):
    events, ledger, gate, a, _ = shared
    one = approval(a)

    def delete(point):
        if point == "after_shared_screen":
            DeletionCoordinator(ledger, EventStoreErasureAdapter(events)).delete(
                context(), request(content_reference=a)
            )

    app = SharedIntelligence(
        events, gate, approvals=(one,), clock=lambda: NOW, checkpoint=delete
    )
    with pytest.raises(PrivacyReadBlocked):
        app.commit(one)
    assert gate.reconcile()
    assert app.context(principal("b")) == ()


def test_shared_context_capture_then_concurrent_deletion_blocks_reply_commit(shared):
    from app.private_web.runtime import Stage0ConversationRuntime
    from app.trusted.openai_transport import FakeOpenAITransport
    from test_private_conversation_runtime import response

    events, ledger, gate, a, _ = shared
    one = approval(a)
    app = SharedIntelligence(events, gate, approvals=(one,), clock=lambda: NOW)
    app.commit(one)
    fake = FakeOpenAITransport([response()])

    def delete(point):
        if point == "after_reservation":
            DeletionCoordinator(ledger, EventStoreErasureAdapter(events)).delete(
                context(), request(content_reference=a)
            )

    runtime = Stage0ConversationRuntime(
        events,
        gate,
        fake,
        shared_intelligence=app,
        clock=lambda: NOW,
        checkpoint=delete,
    )
    with pytest.raises(PrivacyReadBlocked):
        runtime.submit(principal("b"), "race", "Обсудим идеи")
    assert fake.calls == []
    assert gate.reconcile()
    with events._open() as db:
        assert db.execute("SELECT * FROM agent_reply_payloads").fetchall() == []


@pytest.mark.parametrize("guard", ["stop", "spend"])
def test_shared_path_stop_and_spend_prevent_model_contact(shared, guard):
    from app.private_web.runtime import Stage0ConversationRuntime
    from app.core.provider_spend import ProviderStateStore
    from test_provider_spend import attempt
    from app.trusted.openai_transport import FakeOpenAITransport

    events, _, gate, a, _ = shared
    one = approval(a)
    app = SharedIntelligence(events, gate, approvals=(one,), clock=lambda: NOW)
    app.commit(one)
    if guard == "spend":
        ProviderStateStore(events).record_attempt(
            attempt(outcome="confirmed_spend_limit"),
            confirmed_spend_limit_code="project_spend_limit_exceeded",
        )
    fake = FakeOpenAITransport([])
    runtime = Stage0ConversationRuntime(
        events,
        gate,
        fake,
        shared_intelligence=app,
        clock=lambda: NOW,
        emergency_stop=lambda: guard == "stop",
    )
    result = runtime.submit(principal("b"), "guarded", "Обсудим идеи")
    assert result.model_state.value == "skipped" and fake.calls == []


def test_genuine_v5_quarantine_replays_later_deletion(shared, tmp_path):
    events, ledger, _, a, _ = shared
    with sqlite3.connect(events.database_path) as db:
        remove_v6(db)
        db.execute("UPDATE schema_metadata SET version=5")
        db.execute("PRAGMA user_version=5")
    backup = tmp_path / "genuine-v5.db"
    create_sqlite_backup(events.database_path, backup, 5, NOW)
    current = EventStore(events.database_path)
    assert (
        DeletionCoordinator(ledger, EventStoreErasureAdapter(current))
        .delete(context(), request(content_reference=a))
        .disposition.value
        == "applied"
    )
    quarantine = tmp_path / "v5-quarantine.db"
    result = QuarantinedRestore(ledger, 5, EventStoreRestoreAdapter).restore(
        backup, quarantine, NOW
    )
    assert result.activation_eligible and _schema_version(quarantine) == 5
    with sqlite3.connect(f"file:{quarantine}?immutable=1", uri=True) as db:
        assert db.execute(
            "SELECT payload_id FROM event_envelopes WHERE event_id=?", (a,)
        ).fetchone() == (None,)
