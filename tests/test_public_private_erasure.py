"""Actual SDK/IPC ancestry across private A, public cognition, and private B."""

from dataclasses import replace
import json
import sqlite3

import pytest

from app.cognition.shared import SharedIntelligence
from app.integration.public_intellectual_source import PublicIntellectualSource
from app.integration.shared_intellectual_source import SharedIntellectualSource
from app.private_web.runtime import WorkerConversationRuntime
from app.privacy.application import EventStoreErasureAdapter, EventStoreRestoreAdapter
from app.privacy.deletion import DeletionCoordinator
from app.privacy.recovery import QuarantinedRestore, create_sqlite_backup
from test_agent_intellectual_state import private_setup, private_submit, proposal
from test_int24_runtime import NOW, setup as public_setup, run_once
from test_privacy_deletion import (
    context as deletion_context,
    request as deletion_request,
)
from test_reviewed_shared import candidate


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


@pytest.fixture
def cycle(tmp_path, request):
    private_dir, public_dir = tmp_path / "private", tmp_path / "public"
    private_dir.mkdir(mode=0o700)
    public_dir.mkdir(mode=0o700)
    app, a, b, events, ledger, gate, sdk, server = private_setup(private_dir)
    public_worker = None
    try:
        private_submit(server, a, "a-root", "PRIVATE_A_ROOT_CANARY Обсудим объяснения.")
        with events._open() as db:
            root = db.execute(
                "SELECT event_id FROM event_envelopes WHERE source_message_id='a-root'"
            ).fetchone()[0]
        shared = SharedIntelligence(
            events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
        )
        audience = (
            ("participant-a", "public:account-1")
            if getattr(request, "param", None) == "public-only"
            else ("participant-a", "participant-b", "public:account-1")
        )
        item = replace(
            candidate(root, value="Compare causal models of uncertainty."),
            audience=audience,
        )
        shared.transition(item)
        change = proposal()
        change["fields"]["topic"] = "Различение причинных объяснений"
        public, public_worker, public_sdk, _, clock = public_setup(
            public_dir,
            {
                "decision": "no_action",
                "reason_category": "other",
                "intellectual": change,
            },
            shared_intellectual_source=SharedIntellectualSource(shared, "account-1"),
        )
        run_once(public, public_worker)
        public_frame = json.loads(public_sdk.calls[-1]["input"][1]["content"])
        assert (
            public_frame["intellectual_state"]["sources"][0]["kind"]
            == "shared_abstract_influence"
        )
        bridge = PublicIntellectualSource(
            account=public.account,
            journal_path=public.journal.path,
            threads_path=public.service.store.database,
            ledger_path=public.ledger.path,
            shared_source=SharedIntellectualSource(shared, public.account),
            clock=clock.now,
        )
        old = app._conversations
        app._conversations = WorkerConversationRuntime(
            events,
            gate,
            old._transport,
            provider_state=old._provider,
            clock=old._clock,
            public_intellectual_source=bridge,
        )

        def learn():
            frame = json.loads(sdk.calls[-1]["input"])["intellectual_state"]
            source = next(
                (
                    row
                    for row in frame["sources"]
                    if row["kind"] == "public_agent_state"
                ),
                None,
            )
            sdk.change = (
                None
                if source is None
                else proposal(source["handle"], fields=change["fields"])
            )

        sdk.before_result = learn
        private_submit(
            server, b, "b-import", "B_OWN_QUESTION_CANARY Как изменились твои интересы?"
        )
        assert "PRIVATE_A_ROOT_CANARY" not in sdk.calls[-1]["input"]
        with events._open() as db:
            operation = db.execute(
                "SELECT operation_id FROM agent_reply_operations WHERE audience_principal_id='participant-b'"
            ).fetchone()[0]
        yield app, events, ledger, gate, sdk, server, root, operation, item, bridge, b
    finally:
        server.close()
        if public_worker is not None:
            public_worker.close()


def test_public_to_private_reply_carries_private_shared_ancestor(cycle):
    _, events, _, _, _, _, root, operation, item, _, _ = cycle
    with events._open() as db:
        assert db.execute(
            "SELECT shared_operation FROM protected_reply_shared WHERE operation_id=?",
            (operation,),
        ).fetchall() == [(item.proposal.operation_id,)]
        raw = db.execute(
            "SELECT snapshot FROM agent_intellectual_operations WHERE operation=?",
            (operation,),
        ).fetchone()[0]
        assert json.loads(raw)["dependencies"]["event:" + root] == root
        assert json.loads(raw)["external_receipts"]


def test_private_deletion_erases_public_derived_reply_and_old_backup(cycle, tmp_path):
    _, events, ledger, gate, _, _, root, operation, _, _, _ = cycle
    backup, restored_path = tmp_path / "before.db", tmp_path / "restored.db"
    create_sqlite_backup(events.database_path, backup, 14, NOW)
    result = DeletionCoordinator(ledger, EventStoreErasureAdapter(events)).delete(
        deletion_context(), deletion_request(content_reference=root)
    )
    assert result.disposition.value == "applied" and gate.reconcile()
    restored = QuarantinedRestore(ledger, 14, EventStoreRestoreAdapter).restore(
        backup, restored_path, NOW
    )
    assert restored.activation_eligible
    for path in (events.database_path, restored_path):
        with sqlite3.connect(path) as db:
            assert db.execute(
                "SELECT retention_status FROM agent_reply_operations WHERE operation_id=?",
                (operation,),
            ).fetchone() == ("invalidated",)
            assert not db.execute(
                "SELECT 1 FROM agent_reply_payloads WHERE operation_id=?", (operation,)
            ).fetchone()
            dump = "\n".join(db.iterdump())
            assert "PRIVATE_A_ROOT_CANARY" not in dump
            assert "Различение причинных объяснений" not in dump
            assert "B_OWN_QUESTION_CANARY" in dump


@pytest.mark.parametrize("cycle", ["public-only"], indirect=True)
def test_public_audience_does_not_grant_private_b_access(cycle):
    _, events, _, _, sdk, _, _, operation, _, _, _ = cycle
    frame = json.loads(sdk.calls[-1]["input"])
    assert not any(
        row["kind"] == "public_agent_state"
        for row in frame["intellectual_state"]["sources"]
    )
    assert "Различение причинных объяснений" not in sdk.calls[-1]["input"]
    with events._open() as db:
        assert not db.execute(
            "SELECT 1 FROM protected_reply_shared WHERE operation_id=?", (operation,)
        ).fetchone()
        assert not db.execute(
            "SELECT 1 FROM agent_intellectual_revisions WHERE owner='participant-b'"
        ).fetchone()


def test_read_only_audience_view_and_protected_config_preserve_scope(cycle, tmp_path):
    from test_event_store import principal

    _, _, _, _, _, _, _, _, item, bridge, _ = cycle
    current = bridge.sources(principal=principal("b"))
    assert current and all(
        row["receipt"]["shared"] == [item.proposal.operation_id]
        for row in current.values()
    )
    assert bridge.sources(principal=principal("outsider")) == {}
    view = bridge.view
    config = tmp_path / "public-source.json"
    config.write_text(
        json.dumps(
            {
                "version": "public-intellectual-source-v1",
                "account": view.account,
                "journal_path": str(view.journal.path),
                "threads_path": str(view.service.store.database),
                "ledger_path": str(view.ledger.path),
                "mission_path": None,
            }
        )
    )
    config.chmod(0o400)
    restored = PublicIntellectualSource.from_config(
        config, shared=view.shared_intellectual_source.shared, clock=view.clock.now
    )
    assert restored.sources(principal=principal("b")) == current


def test_restored_history_cannot_release_imports_without_live_source_binding(
    cycle, tmp_path
):
    from app.core.store import EventStore
    from app.privacy.application import PrivacyReadGate

    app, events, ledger, _, sdk, server, _, _, _, _, b = cycle
    backup, restored_path = tmp_path / "before.db", tmp_path / "restored.db"
    create_sqlite_backup(events.database_path, backup, 14, NOW)
    assert (
        QuarantinedRestore(ledger, 14, EventStoreRestoreAdapter)
        .restore(backup, restored_path, NOW)
        .activation_eligible
    )
    restored = EventStore(restored_path)
    gate = PrivacyReadGate(ledger, restored)
    assert gate.reconcile()
    old = app._conversations
    app._conversations = WorkerConversationRuntime(
        restored, gate, old._worker, provider_state=old._provider, clock=old._clock
    )
    history = json.loads(b.request("GET", "/api/history")[2])["messages"]
    assert not any(row["role"] == "agent" for row in history)
    assert any("B_OWN_QUESTION_CANARY" in row["content"] for row in history)
    sdk.before_result = lambda: None
    sdk.change = None
    private_submit(server, b, "without-source", "Продолжим разговор.")
    assert "Различение причинных объяснений" not in sdk.calls[-1]["input"]


def test_normal_restore_rebinds_live_public_identity_to_restored_private_graph(
    cycle, tmp_path
):
    from app.core.store import EventStore
    from app.privacy.application import PrivacyReadGate

    app, events, ledger, _, sdk, server, _, _, _, bridge, b = cycle
    backup, restored_path = tmp_path / "before.db", tmp_path / "restored.db"
    create_sqlite_backup(events.database_path, backup, 14, NOW)
    assert (
        QuarantinedRestore(ledger, 14, EventStoreRestoreAdapter)
        .restore(backup, restored_path, NOW)
        .activation_eligible
    )
    restored = EventStore(restored_path)
    gate = PrivacyReadGate(ledger, restored)
    assert gate.reconcile()
    shared = SharedIntelligence(
        restored, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )
    view = bridge.view
    rebound = PublicIntellectualSource(
        account=view.account,
        journal_path=view.journal.path,
        threads_path=view.service.store.database,
        ledger_path=view.ledger.path,
        shared_source=SharedIntellectualSource(shared, view.account),
        clock=view.clock.now,
    )
    old = app._conversations
    app._conversations = WorkerConversationRuntime(
        restored,
        gate,
        old._worker,
        provider_state=old._provider,
        clock=old._clock,
        public_intellectual_source=rebound,
    )
    sdk.before_result = lambda: None
    sdk.change = None
    private_submit(server, b, "live-restored", "Продолжим исследовать объяснения.")
    assert "Различение причинных объяснений" in sdk.calls[-1]["input"]
    assert "PRIVATE_A_ROOT_CANARY" not in sdk.calls[-1]["input"]
    assert gate.reconcile()


@pytest.mark.parametrize("foreign", ["store", "ledger"])
def test_private_runtime_rejects_foreign_ancestry_store_or_ledger(
    cycle, tmp_path, foreign
):
    from app.core.store import EventStore
    from app.privacy.application import PrivacyReadGate
    from app.privacy.ledger import PrivacyLedger

    app, events, ledger, gate, _, _, _, _, _, bridge, _ = cycle
    other_events = EventStore(tmp_path / "foreign.db") if foreign == "store" else events
    other_ledger = (
        PrivacyLedger(tmp_path / "foreign-ledger.db") if foreign == "ledger" else ledger
    )
    other_gate = PrivacyReadGate(other_ledger, other_events)
    assert other_gate.reconcile()
    shared = SharedIntelligence(
        other_events, other_gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )
    view = bridge.view
    bad = PublicIntellectualSource(
        account=view.account,
        journal_path=view.journal.path,
        threads_path=view.service.store.database,
        ledger_path=view.ledger.path,
        shared_source=SharedIntellectualSource(shared, view.account),
        clock=view.clock.now,
    )
    old = app._conversations
    with pytest.raises(ValueError, match="same private store and ledger"):
        WorkerConversationRuntime(
            events,
            gate,
            old._worker,
            provider_state=old._provider,
            clock=old._clock,
            public_intellectual_source=bad,
        )


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "foreign-account",
        "binding",
        "extra-field",
        "revision",
        "generation",
        "digest",
        "parent",
        "root",
        "projection",
    ],
)
def test_quarantine_rejects_forged_public_receipts(cycle, tmp_path, corruption):
    from app.core.intellectual_state import digest, encoded

    _, events, ledger, _, _, _, root, operation, _, _, _ = cycle
    with events._open() as db, db:
        value = json.loads(
            db.execute(
                "SELECT snapshot FROM agent_intellectual_operations WHERE operation=?",
                (operation,),
            ).fetchone()[0]
        )
        key, receipt = next(iter(value["external_receipts"].items()))
        if corruption == "missing":
            del value["external_receipts"]
        elif corruption == "foreign-account":
            receipt["account"] = "other-account"
        elif corruption == "binding":
            value["dependencies"][key] = "0" * 64
        elif corruption == "extra-field":
            receipt["authority"] = "owner"
        elif corruption == "revision":
            receipt["revision"] = True
        elif corruption == "generation":
            receipt["generation"] = -1
        elif corruption == "digest":
            receipt["state_digest"] = "not-a-digest"
        elif corruption == "parent":
            receipt["shared"] = ["missing-parent"]
            value["dependencies"][key] = digest(receipt)
        elif corruption == "root":
            del value["dependencies"]["event:" + root]
        else:
            receipt["record_digest"] = "0" * 64
            value["dependencies"][key] = digest(receipt)
        db.execute(
            "UPDATE agent_intellectual_operations SET snapshot=? WHERE operation=?",
            (encoded(value), operation),
        )
    backup, restored = tmp_path / "bad.db", tmp_path / "quarantine.db"
    create_sqlite_backup(events.database_path, backup, 14, NOW)
    result = QuarantinedRestore(ledger, 14, EventStoreRestoreAdapter).restore(
        backup, restored, NOW
    )
    assert not result.activation_eligible and not restored.exists()
