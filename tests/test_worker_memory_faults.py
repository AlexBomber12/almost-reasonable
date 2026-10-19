"""Fault and identity tests for the durable worker extraction handoff."""

from pathlib import Path
from datetime import datetime, timedelta
from hashlib import sha256
import json
import os
import sqlite3

import pytest

from app.cognition.contracts import (
    CognitionCapabilities,
    CognitionOutcome,
    CognitionOutcomeStatus,
    CognitionReason,
)
from app.cognition.generic_store import GenericMemoryStore
from app.cognition.pipeline import CognitionEngine
from app.cognition.worker_extraction import ExtractionProposal
from app.core.provider_spend import ProviderStateStore
from app.core.openai_worker_client import OpenAIWorkerClient
from app.core.store import EventStore
from app.core.self_state_store import SelfStateStore
from app.private_web.extraction_audit import ExtractionAuditStore
from app.private_web.runtime import WorkerConversationRuntime
from app.privacy.application import (
    ApplicationPrivacyError,
    EventStoreErasureAdapter,
    EventStoreRestoreAdapter,
    PrivacyReadBlocked,
    PrivacyReadGate,
    _validate_graph,
)
from app.privacy.deletion import DeletionCoordinator
from app.privacy.ledger import PrivacyLedger
from app.privacy.recovery import QuarantinedRestore, create_sqlite_backup
from test_cognition_pipeline import NOW, append_event, principal
from test_privacy_deletion import (
    context as deletion_context,
    request as deletion_request,
)


@pytest.fixture
def state(tmp_path):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    audit = ExtractionAuditStore(provider, events)
    who = principal("a")
    source = append_event(events, who, "one", "I collect old maps")
    proposal = ExtractionProposal(
        "new_memory", "candidate_useful_memory", "Alice collects old maps", 0.8
    )
    return events, provider, audit, who, source, proposal


def test_audit_binds_input_and_rejects_identity_conflicts(state):
    events, provider, audit, who, source, proposal = state
    op = "memory:" + "a" * 64
    assert audit.read(op, who.principal_id.value, source, "I collect old maps") is None
    assert audit.reserve(
        op, who.principal_id.value, source, "I collect old maps"
    ).created
    other_source = append_event(events, who, "two", "I collect old maps")
    with pytest.raises(ValueError, match="identity conflict"):
        audit.reserve(op, who.principal_id.value, other_source, "I collect old maps")
    with pytest.raises(ValueError, match="identity conflict"):
        audit.read(op, who.principal_id.value, other_source, "I collect old maps")
    with provider._event_store._open() as db:
        binding = db.execute(
            "SELECT binding FROM private_extraction_audit WHERE operation_id=?", (op,)
        ).fetchone()[0]
    assert (
        binding
        == sha256(
            json.dumps([who.principal_id.value, source], ensure_ascii=False).encode()
        ).hexdigest()
    )
    assert (
        binding
        != sha256(
            json.dumps(
                [who.principal_id.value, source, "I collect old maps"],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    )
    with pytest.raises(ValueError, match="identity conflict"):
        audit.reserve(op, who.principal_id.value, source, "changed")
    with pytest.raises(ValueError, match="identity conflict"):
        audit.read(op, who.principal_id.value, source, "changed")
    with pytest.raises(ValueError, match="source unavailable"):
        audit.receive(op, who.principal_id.value, source, "changed", proposal)
    audit.receive(op, who.principal_id.value, source, "I collect old maps", proposal)
    assert (
        audit.read(op, who.principal_id.value, source, "I collect old maps").proposal
        == proposal
    )
    with events._open() as db:
        db.execute(
            "UPDATE private_extraction_receipts SET source_principal_id='forged' WHERE operation_id=?",
            (op,),
        )
        with pytest.raises(ApplicationPrivacyError, match="invalid_extraction_receipt"):
            _validate_graph(db)
    with pytest.raises(ValueError, match="receipt conflict"):
        audit.read(op, who.principal_id.value, source, "I collect old maps")
    with events._open() as db:
        db.execute(
            "UPDATE private_extraction_receipts SET source_principal_id=? WHERE operation_id=?",
            (who.principal_id.value, op),
        )
    with pytest.raises(ValueError, match="invalid extraction outcome"):
        audit.finish(op, "approved")
    audit.finish(op, "accepted")
    with pytest.raises(ValueError, match="outcome conflict"):
        audit.finish(op, "accepted")
    assert (
        audit.read(op, who.principal_id.value, source, "I collect old maps").proposal
        is None
    )
    with events._open() as db:
        assert db.execute(
            "SELECT count(*) FROM private_extraction_receipts"
        ).fetchone() == (0,)


@pytest.mark.parametrize("replay_method", ["read", "reserve"])
def test_final_audit_replay_cleans_receipt_after_cross_store_exit(
    state, monkeypatch, replay_method
):
    events, provider, audit, who, source, proposal = state
    op = "memory:" + "f" * 64
    args = (op, who.principal_id.value, source, "I collect old maps")
    audit.reserve(*args)
    audit.receive(*args, proposal)

    def fail_cleanup(_operation_id):
        raise OSError("exit")

    with monkeypatch.context() as patch:
        patch.setattr(audit, "_delete_receipt", fail_cleanup)
        with pytest.raises(OSError, match="exit"):
            audit.finish(op, "accepted")
    with provider._event_store._open() as db:
        assert db.execute(
            "SELECT status FROM private_extraction_audit WHERE operation_id=?", (op,)
        ).fetchone() == ("accepted",)
    with events._open() as db:
        assert db.execute(
            "SELECT count(*) FROM private_extraction_receipts"
        ).fetchone() == (1,)
    replay = getattr(ExtractionAuditStore(provider, events), replay_method)(*args)
    assert replay.status == "accepted" and replay.proposal is None
    with events._open() as db:
        assert db.execute(
            "SELECT count(*) FROM private_extraction_receipts"
        ).fetchone() == (0,)


def test_durable_receipt_reconciles_reserved_intent_and_source_deletion_erases_it(
    state, tmp_path
):
    events, provider, audit, who, source, proposal = state
    op = "memory:" + "b" * 64
    audit.reserve(op, who.principal_id.value, source, "I collect old maps")
    audit.receive(op, who.principal_id.value, source, "I collect old maps", proposal)
    # A crash after receipt but before IPC status update leaves the original intent.
    with provider._event_store._open() as db:
        db.execute(
            "UPDATE private_extraction_audit SET status='reserved' WHERE operation_id=?",
            (op,),
        )
    replay = audit.reserve(op, who.principal_id.value, source, "I collect old maps")
    assert replay.status == "received" and not replay.created
    assert replay.proposal == proposal
    backup = tmp_path / "pending-receipt-backup.db"
    create_sqlite_backup(events.database_path, backup, 9, NOW)
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    assert (
        DeletionCoordinator(ledger, EventStoreErasureAdapter(events))
        .delete(deletion_context(), deletion_request(content_reference=source))
        .disposition.value
        == "applied"
    )
    with events._open() as db:
        assert db.execute(
            "SELECT count(*) FROM private_extraction_receipts"
        ).fetchone() == (0,)
    with pytest.raises(ValueError, match="source unavailable"):
        audit.reserve(
            "memory:" + "d" * 64, who.principal_id.value, source, "I collect old maps"
        )
    assert (
        audit.read(op, who.principal_id.value, source, "I collect old maps").proposal
        is None
    )
    quarantine = tmp_path / "pending-receipt-quarantine.db"
    assert (
        QuarantinedRestore(ledger, 9, EventStoreRestoreAdapter)
        .restore(backup, quarantine, NOW)
        .activation_eligible
    )
    with sqlite3.connect(f"{quarantine.as_uri()}?mode=ro", uri=True) as db:
        assert db.execute(
            "SELECT count(*) FROM private_extraction_receipts"
        ).fetchone() == (0,)


def test_audit_constructor_and_receipt_status_fail_closed(state, tmp_path):
    events, provider, audit, who, source, proposal = state
    with pytest.raises(TypeError):
        ExtractionAuditStore(provider, Path(tmp_path))
    op = "memory:" + "c" * 64
    audit.reserve(op, who.principal_id.value, source, "I collect old maps")
    with provider._event_store._open() as db:
        db.execute(
            "UPDATE private_extraction_audit SET status='accepted' WHERE operation_id=?",
            (op,),
        )
    with pytest.raises(ValueError, match="receipt conflict"):
        audit.receive(
            op, who.principal_id.value, source, "I collect old maps", proposal
        )
    # The receipt remains protected by the source edge even after an IPC race.
    with events._open() as db:
        assert db.execute(
            "SELECT source_event_id FROM private_extraction_receipts"
        ).fetchone() == (source,)


@pytest.mark.parametrize(
    "capabilities,source_id,operation_id,proposal,reason",
    [
        (
            CognitionCapabilities(memory_extraction=False, generic_memory_write=True),
            "event-one",
            "memory:op",
            ExtractionProposal("new_memory", "candidate_useful_memory", "Maps", 0.8),
            CognitionReason.CAPABILITY_DISABLED,
        ),
        (
            CognitionCapabilities(generic_memory_write=False),
            "event-one",
            "memory:op",
            ExtractionProposal("new_memory", "candidate_useful_memory", "Maps", 0.8),
            CognitionReason.CAPABILITY_DISABLED,
        ),
        (
            CognitionCapabilities(generic_memory_write=True),
            "event-one",
            "memory:op",
            None,
            CognitionReason.INVALID_PROPOSAL,
        ),
        (
            CognitionCapabilities(generic_memory_write=True),
            "event-one",
            "memory:op",
            ExtractionProposal("no_change", None, None, None),
            CognitionReason.INVALID_PROPOSAL,
        ),
        (
            CognitionCapabilities(generic_memory_write=True),
            "bad source",
            "memory:op",
            ExtractionProposal("new_memory", "candidate_useful_memory", "Maps", 0.8),
            CognitionReason.INVALID_PROPOSAL,
        ),
        (
            CognitionCapabilities(generic_memory_write=True),
            "event-one",
            "bad operation",
            ExtractionProposal("new_memory", "candidate_useful_memory", "Maps", 0.8),
            CognitionReason.INVALID_PROPOSAL,
        ),
        (
            CognitionCapabilities(generic_memory_write=True),
            "event-missing",
            "memory:op",
            ExtractionProposal("new_memory", "candidate_useful_memory", "Maps", 0.8),
            CognitionReason.SOURCE_NOT_FOUND,
        ),
        (
            CognitionCapabilities(generic_memory_write=True),
            "event-one",
            "memory:op",
            ExtractionProposal("new_memory", "forged-kind", "Maps", 0.8),
            CognitionReason.INVALID_MEMORY_KIND,
        ),
    ],
)
def test_external_extraction_rejects_unbound_or_unavailable_proposals(
    state, capabilities, source_id, operation_id, proposal, reason
):
    events, _provider, _audit, who, _source, _candidate = state
    engine = CognitionEngine(
        events, SelfStateStore(events), capabilities=capabilities, clock=lambda: NOW
    )
    assert (
        engine.commit_worker_extraction(who, source_id, operation_id, proposal).reason
        is reason
    )


def test_external_extraction_rechecks_source_and_clock(state, monkeypatch):
    events, _provider, _audit, who, source, proposal = state
    engine = CognitionEngine(
        events,
        SelfStateStore(events),
        capabilities=CognitionCapabilities(generic_memory_write=True),
        clock=lambda: datetime(2026, 1, 1),
    )
    assert (
        engine.commit_worker_extraction(who, source, "memory:replay", proposal).status
        is CognitionOutcomeStatus.PERSISTED
    )
    engine._clock = lambda: NOW + timedelta(days=5)
    assert (
        engine.commit_worker_extraction(who, source, "memory:replay", proposal).status
        is CognitionOutcomeStatus.PERSISTED
    )
    original = engine._read.retrieve_exact_private_event
    seen = [0]

    def disappearing(context, event_id):
        seen[0] += 1
        return original(context, event_id) if seen[0] == 1 else None

    monkeypatch.setattr(engine._read, "retrieve_exact_private_event", disappearing)
    assert (
        engine.commit_worker_extraction(who, source, "memory:stale", proposal).reason
        is CognitionReason.STALE_REVISION
    )


@pytest.fixture
def runtime_state(tmp_path):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, events)
    assert gate.reconcile()
    client = OpenAIWorkerClient(
        tmp_path / "absent.sock", expected_worker_uid=os.getuid()
    )
    runtime = WorkerConversationRuntime(
        events,
        gate,
        client,
        provider_state=provider,
        memory_extraction_enabled=True,
        clock=lambda: NOW,
    )
    who = principal("a")
    source = append_event(events, who, "one", "I collect old maps")
    reply = "reply:" + "a" * 64
    memory = "memory:" + "a" * 64
    return events, provider, gate, runtime, who, source, reply, memory


def test_worker_runtime_admission_and_replay_fail_closed(runtime_state):
    events, provider, _gate, runtime, who, source, reply, memory = runtime_state
    runtime._stop = lambda: True
    runtime._extract_worker_memory(who, source, "I collect old maps", "corr", reply)
    assert (
        runtime._extraction_audit.read(
            memory, who.principal_id.value, source, "I collect old maps"
        )
        is None
    )
    runtime._stop = lambda: False
    runtime._extract_worker_memory(who, "event-missing", "text", "corr", reply)
    assert (
        runtime._extraction_audit.read(
            memory, who.principal_id.value, source, "I collect old maps"
        )
        is None
    )
    runtime._extraction_audit.reserve(
        memory, who.principal_id.value, source, "I collect old maps"
    )
    runtime._extract_worker_memory(who, source, "I collect old maps", "corr", reply)
    assert (
        runtime._extraction_audit.read(
            memory, who.principal_id.value, source, "I collect old maps"
        ).status
        == "reserved"
    )
    proposal = ExtractionProposal(
        "new_memory", "candidate_useful_memory", "Alice collects old maps", 0.8
    )
    runtime._extraction_audit.receive(
        memory, who.principal_id.value, source, "I collect old maps", proposal
    )
    runtime._extract_worker_memory(who, source, "I collect old maps", "corr", reply)
    assert (
        runtime._extraction_audit.read(
            memory, who.principal_id.value, source, "I collect old maps"
        ).status
        == "accepted"
    )
    assert len(GenericMemoryStore(events).current(who)) == 1
    assert provider.usage_attempt_count() == 0


@pytest.mark.parametrize("failure", ["input", "reservation", "client"])
def test_worker_runtime_failure_does_not_create_memory(tmp_path, failure, monkeypatch):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, events)
    assert gate.reconcile()
    client = OpenAIWorkerClient(
        tmp_path / "absent.sock", expected_worker_uid=os.getuid()
    )
    runtime = WorkerConversationRuntime(
        events,
        gate,
        client,
        provider_state=provider,
        memory_extraction_enabled=True,
        clock=lambda: NOW,
    )
    who = principal("a")
    content = "x" * 8193 if failure == "input" else "I collect old maps"
    source = append_event(events, who, "one", content)
    reply = "reply:" + "a" * 64
    memory = "memory:" + "a" * 64
    if failure == "reservation":
        provider.reserve_attempt("ipc:" + memory, "corr", NOW)
    if failure == "client":

        def fail(*_args, **_kwargs):
            raise ValueError("synthetic encode failure")

        monkeypatch.setattr(client, "infer", fail)
    runtime._extract_worker_memory(who, source, content, "corr", reply)
    expected = "rejected" if failure in {"input", "client"} else "unavailable"
    assert (
        runtime._extraction_audit.read(
            memory, who.principal_id.value, source, content
        ).status
        == expected
    )
    assert GenericMemoryStore(events).current(who) == ()


def test_worker_runtime_rechecks_privacy_and_stale_commit(runtime_state, monkeypatch):
    _events, _provider, gate, runtime, who, source, _reply, memory = runtime_state
    proposal = ExtractionProposal(
        "new_memory", "candidate_useful_memory", "Alice collects old maps", 0.8
    )
    runtime._extraction_audit.reserve(
        memory, who.principal_id.value, source, "I collect old maps"
    )
    runtime._extraction_audit.receive(
        memory, who.principal_id.value, source, "I collect old maps", proposal
    )
    original = gate.authorized

    def blocked(_callback):
        raise PrivacyReadBlocked("synthetic privacy fence")

    monkeypatch.setattr(gate, "authorized", blocked)
    runtime._commit_worker_memory(who, source, memory, proposal)
    assert (
        runtime._extraction_audit.read(
            memory, who.principal_id.value, source, "I collect old maps"
        ).status
        == "unavailable"
    )
    monkeypatch.setattr(gate, "authorized", original)
    second = "memory:" + "b" * 64
    runtime._extraction_audit.reserve(
        second, who.principal_id.value, source, "I collect old maps"
    )
    monkeypatch.setattr(
        runtime._worker_cognition,
        "commit_worker_extraction",
        lambda *_args: CognitionOutcome(
            CognitionOutcomeStatus.REJECTED, CognitionReason.STALE_REVISION
        ),
    )
    runtime._commit_worker_memory(who, source, second, proposal)
    assert (
        runtime._extraction_audit.read(
            second, who.principal_id.value, source, "I collect old maps"
        ).status
        == "unavailable"
    )


def test_worker_runtime_requires_boolean_enablement(tmp_path):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, events)
    assert gate.reconcile()
    client = OpenAIWorkerClient(
        tmp_path / "absent.sock", expected_worker_uid=os.getuid()
    )
    with pytest.raises(TypeError, match="memory_extraction_enabled"):
        WorkerConversationRuntime(
            events,
            gate,
            client,
            provider_state=provider,
            memory_extraction_enabled="yes",
        )


def test_worker_memory_commit_rechecks_application_stop(runtime_state):
    _events, _provider, _gate, runtime, who, source, _reply, memory = runtime_state
    runtime._extraction_audit.reserve(
        memory, who.principal_id.value, source, "I collect old maps"
    )
    runtime._stop = lambda: True
    proposal = ExtractionProposal("no_change", None, None, None)
    runtime._commit_worker_memory(who, source, memory, proposal)
    assert (
        runtime._extraction_audit.read(
            memory, who.principal_id.value, source, "I collect old maps"
        ).status
        == "unavailable"
    )
