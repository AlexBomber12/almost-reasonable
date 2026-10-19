"""Closed model contract for one bounded private generic-memory change."""

import json
import multiprocessing
import os
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

from app.cognition.worker_evolution import (
    EvolutionModelRequest,
    EvolutionMemory,
    EvolutionProposal,
    parse_evolution_proposal,
)
from app.core.provider_contract import (
    DeploymentStage,
    build_worker_evolution_request,
    default_openai_provider_config,
)
from app.core.provider_spend import (
    AttemptReservationStatus,
    ProviderPersistenceError,
    ProviderStateStore,
)
from app.core.openai_worker_client import (
    OpenAIWorkerClient,
    ClientResult,
    CLIENT_OUTCOME_UNCERTAIN,
)
from app.core import openai_worker_client as client_module
from app.core.context import ContextLimits
from app.core.store import EventStore
from app.core.events import VerificationStatus
from app.core.core_self import CORE_SELF_VERSION
from app.core.self_state_store import SelfStateStore
from app.cognition.contracts import (
    CognitionCapabilities,
    CognitionOutcomeStatus,
    CognitionReason,
    GenericMemoryProposal,
    MemoryKind,
    ProposalContext,
)
from app.cognition.generic_store import GenericMemoryStore
from app.cognition.pipeline import CognitionEngine
from app.cognition.worker_extraction import ExtractionProposal
from app.private_web.evolution_audit import EvolutionAuditStore, EvolutionSnapshot
import app.private_web.evolution_audit as evolution_audit_module
from app.private_web.runtime import WorkerConversationRuntime
from app.private_web.cli import main as web_main
from app.privacy.application import (
    PrivacyReadGate,
    PrivacyReadBlocked,
    EventStoreErasureAdapter,
    EventStoreRestoreAdapter,
)
from app.privacy.deletion import DeletionCoordinator
from app.privacy.ledger import PrivacyLedger
from app.privacy.recovery import QuarantinedRestore, create_sqlite_backup
from app.trusted.openai_gateway import (
    GatewayOutcome,
    TrustedOpenAIGateway,
    _parse_response,
)
from app.trusted.openai_transport import FakeOpenAITransport
from app.trusted.openai_transport import OpenAIResponsesTransport
from app.trusted.openai_worker_server import OpenAIWorkerServer, WorkerServerConfig
from app.trusted.private_pilot import PilotAllowance, load_activation
from app.trusted.private_pilot_worker import (
    PilotRequestGate,
    PilotStop,
    provider_config,
)
from app.trusted import private_pilot_worker
from app.trusted.openai_worker_protocol import (
    EVOLUTION_PROTOCOL_VERSION,
    InferenceRequest,
    WorkerProtocolError,
    WorkerResponse,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)
from test_openai_gateway import response as provider_response
from test_cognition_pipeline import NOW, append_event, principal
from test_int19_worker_runtime import _app
from test_int22_pilot import activation_file
from test_privacy_deletion import (
    context as deletion_context,
    request as deletion_request,
)


def test_evolution_request_contains_only_bounded_untrusted_input_and_handles():
    request = EvolutionModelRequest(
        "I will learn Italian in 2028",
        (EvolutionMemory("h1", "candidate_useful_memory", "Italian in 2027", 0.8),),
    )
    projection = json.loads(request.serialize())
    assert projection == {
        "contract_version": "model-contract-private-evolution-v1",
        "task": "evolve_private_memory",
        "participant_input": "I will learn Italian in 2028",
        "memories": [
            {
                "handle": "h1",
                "kind": "candidate_useful_memory",
                "statement": "Italian in 2027",
                "confidence": 0.8,
                "trust": "untrusted_memory_data",
                "can_instruct": False,
            }
        ],
    }
    assert "item_id" not in request.serialize()
    with pytest.raises(ValueError):
        EvolutionModelRequest("", ()).serialize()
    with pytest.raises(ValueError):
        EvolutionModelRequest("x" * 8193, ()).serialize()
    with pytest.raises(ValueError):
        EvolutionModelRequest("\ud800", ()).serialize()
    valid = EvolutionMemory("h1", "candidate_useful_memory", "Synthetic memory", 0.5)
    for malformed in (
        EvolutionModelRequest("Text", [valid]),
        EvolutionModelRequest("Text", (valid,) * 9),
        EvolutionModelRequest("Text", ("raw",)),
        EvolutionModelRequest("Text", (replace(valid, handle="bad handle"),)),
        EvolutionModelRequest("Text", (valid, valid)),
        EvolutionModelRequest("Text", (replace(valid, kind="goal_or_intention"),)),
        EvolutionModelRequest("Text", (replace(valid, statement=""),)),
        EvolutionModelRequest("Text", (replace(valid, statement="x" * 4097),)),
        EvolutionModelRequest("Text", (replace(valid, confidence=True),)),
        EvolutionModelRequest("Text", (replace(valid, confidence=1.1),)),
        EvolutionModelRequest(
            "x" * 8192,
            (
                replace(valid, statement="y" * 4096),
                replace(valid, handle="h2", statement="z" * 4096),
            ),
        ),
    ):
        with pytest.raises(ValueError, match="invalid evolution request"):
            malformed.serialize()


@pytest.mark.parametrize(
    ("decision", "handles"),
    [
        ("new_memory", []),
        ("revise_memory", ["h1"]),
        ("consolidate_memories", ["h1", "h2"]),
    ],
)
def test_evolution_proposal_accepts_one_closed_action(decision, handles):
    payload = {
        "decision": decision,
        "handles": handles,
        "kind": "candidate_useful_memory",
        "statement": "Italian in 2028",
        "confidence": 0.7,
    }
    assert parse_evolution_proposal(json.dumps(payload)) == EvolutionProposal(
        decision, tuple(handles), payload["kind"], payload["statement"], 0.7
    )


def test_evolution_proposal_rejects_forged_authority_and_invalid_values():
    valid = {
        "decision": "revise_memory",
        "handles": ["h1"],
        "kind": "candidate_useful_memory",
        "statement": "Italian in 2028",
        "confidence": 0.7,
    }
    bad = [
        {**valid, "item_id": "forged"},
        {**valid, "handles": ["h1", "h1"]},
        {**valid, "handles": ["h1", "h2"]},
        {**valid, "decision": "consolidate_memories"},
        {**valid, "confidence": 10**400},
        {**valid, "confidence": True},
        {**valid, "statement": "x" * 4097},
        {**valid, "kind": "goal_or_intention"},
        {**valid, "decision": "no_change"},
        {**valid, "handles": None},
        {**valid, "handles": ["h" + str(index) for index in range(9)]},
        {**valid, "handles": ["bad handle"]},
        {**valid, "handles": [1]},
        {**valid, "statement": ""},
        {**valid, "confidence": -0.1},
        {**valid, "confidence": "0.7"},
    ]
    for payload in bad:
        with pytest.raises(ValueError, match="invalid evolution proposal"):
            parse_evolution_proposal(json.dumps(payload))
    with pytest.raises(ValueError, match="invalid evolution proposal"):
        parse_evolution_proposal('{"decision":"no_change","decision":"no_change"}')
    for raw in (None, "x" * 8193, "\ud800"):
        with pytest.raises(ValueError, match="invalid evolution proposal"):
            parse_evolution_proposal(raw)
    assert parse_evolution_proposal(
        '{"decision":"no_change","handles":[],"kind":null,"statement":null,"confidence":null}'
    ) == EvolutionProposal("no_change", (), None, None, None)


def test_evolution_codec_builder_and_gateway_account_for_invalid_output(tmp_path):
    request = EvolutionModelRequest(
        "I will learn Italian in 2028",
        (EvolutionMemory("h1", "candidate_useful_memory", "Italian in 2027", 0.8),),
    )
    wire = encode_request(InferenceRequest("evolution:test", "corr", request))
    assert json.loads(wire)["protocol_version"] == EVOLUTION_PROTOCOL_VERSION
    assert decode_request(wire).model_request == request
    with pytest.raises(WorkerProtocolError, match="evolution frame is too large"):
        decode_request(wire + b" " * 17_500)
    sdk = build_worker_evolution_request(
        request, default_openai_provider_config()
    ).to_sdk_kwargs()
    assert sdk["tools"] == [] and sdk["store"] is False
    assert sdk["text"]["format"]["name"] == "private_memory_evolution_proposal"
    assert "Italian in 2027" in sdk["input"]
    assert "untrusted data" in sdk["instructions"]
    with pytest.raises(TypeError):
        build_worker_evolution_request("wrong", default_openai_provider_config())
    tampered = json.loads(wire)
    tampered["model_request"]["memories"][0]["item_id"] = "forged"
    with pytest.raises(WorkerProtocolError):
        decode_request(json.dumps(tampered).encode())
    for change in (
        lambda payload: payload["model_request"].update(contract_version="wrong"),
        lambda payload: payload["model_request"].update(task="infer"),
        lambda payload: payload["model_request"].update(memories={}),
        lambda payload: payload["model_request"]["memories"][0].update(
            trust="instructions"
        ),
        lambda payload: payload["model_request"]["memories"][0].update(
            can_instruct=True
        ),
    ):
        malformed = json.loads(wire)
        change(malformed)
        with pytest.raises(WorkerProtocolError):
            decode_request(json.dumps(malformed).encode())
    proposal = EvolutionProposal(
        "revise_memory", ("h1",), "candidate_useful_memory", "Italian in 2028", 0.8
    )
    response = WorkerResponse(
        "completed", 1, None, evolution=proposal, evolution_operation=True
    )
    assert decode_response(encode_response(response)) == response
    invalid = json.loads(encode_response(response))
    invalid["memory_proposal"]["source_event_id"] = "forged"
    with pytest.raises(WorkerProtocolError):
        decode_response(json.dumps(invalid).encode())
    invalid = json.loads(encode_response(response))
    invalid["attempts"] = -1
    with pytest.raises(WorkerProtocolError):
        decode_response(json.dumps(invalid).encode())
    with pytest.raises(ValueError):
        encode_response(WorkerResponse("completed", 1, None, evolution=proposal))
    with pytest.raises(ValueError):
        encode_response(
            WorkerResponse(
                "completed",
                1,
                None,
                evolution=proposal,
                extraction_operation=True,
                evolution_operation=True,
            )
        )
    raw = json.dumps(
        {
            "decision": "revise_memory",
            "handles": ["h1"],
            "kind": "candidate_useful_memory",
            "statement": "Italian in 2028",
            "confidence": 10**400,
        }
    )
    transport = FakeOpenAITransport([provider_response(raw)])
    state = ProviderStateStore(EventStore(tmp_path / "provider.db"))
    gateway = TrustedOpenAIGateway(
        store=state,
        transport=transport,
        config=replace(
            default_openai_provider_config(),
            deployment_stage=DeploymentStage.STAGE_1,
            paid_calls_enabled=True,
            max_attempts=1,
        ),
    )
    server_config = WorkerServerConfig(
        tmp_path / "worker.sock", frozenset({os.getuid()})
    )
    with pytest.raises(TypeError):
        OpenAIWorkerServer(server_config, gateway, evolution_enabled="yes")
    with pytest.raises(TypeError):
        OpenAIWorkerServer(server_config, gateway, evolution_commit_guard="invalid")
    with pytest.raises(TypeError):
        gateway.submit_evolution(
            "wrong", correlation_id="corr", operation_id="evolution:wrong"
        )
    result = gateway.submit_evolution(
        request, correlation_id="corr", operation_id="evolution:test"
    )
    assert result.outcome is GatewayOutcome.INVALID_PROPOSAL
    assert result.evolution is None and result.attempts == 1
    usage = state.usage_attempt("evolution:test:1")
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (
        100,
        20,
        120,
    )
    assert len(transport.calls) == 1


def test_evolution_worker_rechecks_commit_guard_after_provider(tmp_path):
    proposal = json.dumps(
        {
            "decision": "no_change",
            "handles": [],
            "kind": None,
            "statement": None,
            "confidence": None,
        }
    )
    transport = FakeOpenAITransport([provider_response(proposal)])
    gateway = TrustedOpenAIGateway(
        store=ProviderStateStore(EventStore(tmp_path / "provider.db")),
        transport=transport,
        config=replace(
            default_openai_provider_config(),
            deployment_stage=DeploymentStage.STAGE_1,
            paid_calls_enabled=True,
            max_attempts=1,
        ),
    )
    socket_path = tmp_path / "worker.sock"
    server = OpenAIWorkerServer(
        WorkerServerConfig(socket_path, frozenset({os.getuid()})),
        gateway,
        evolution_enabled=True,
        evolution_commit_guard=lambda: False,
    )
    server.start()
    try:
        thread = Thread(target=server.serve_once)
        thread.start()
        result = OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()).infer(
            EvolutionModelRequest("No change please", ()),
            operation_id="evolution:" + "a" * 64,
            correlation_id="corr",
        )
        thread.join(5)
        assert not thread.is_alive()
        assert result.outcome == "authorization_rejected"
        assert result.evolution is None
        assert len(transport.calls) == 1
    finally:
        server.close()
    huge = (
        '{"decision":"new_memory","handles":[],"kind":"candidate_useful_memory","statement":"Synthetic","confidence":'
        + "9" * 5000
        + "}"
    )
    assert (
        _parse_response(provider_response(huge), evolution=True).outcome
        is GatewayOutcome.INVALID_RESPONSE
    )


def test_evolution_flag_requires_explicit_worker_mode(tmp_path):
    with pytest.raises(
        SystemExit, match="privacy and owner stores require worker mode"
    ):
        web_main(
            [
                "serve",
                "--auth-db",
                str(tmp_path / "auth.db"),
                "--event-db",
                str(tmp_path / "events.db"),
                "--allowed-host",
                "127.0.0.1:8765",
                "--allowed-origin",
                "http://127.0.0.1:8765",
                "--insecure-http",
                "--enable-memory-evolution",
            ]
        )


def test_client_rejects_operation_mismatched_worker_response(tmp_path, monkeypatch):
    from threading import Thread

    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    raw = '{"decision":"no_change","handles":[],"kind":null,"statement":null,"confidence":null}'
    gateway = TrustedOpenAIGateway(
        store=ProviderStateStore(EventStore(tmp_path / "provider.db")),
        transport=FakeOpenAITransport([provider_response(raw)]),
        config=replace(
            default_openai_provider_config(),
            deployment_stage=DeploymentStage.STAGE_1,
            paid_calls_enabled=True,
            max_attempts=1,
        ),
    )
    server = OpenAIWorkerServer(
        WorkerServerConfig(socket_path, frozenset({os.getuid()})),
        gateway,
        evolution_enabled=True,
    )
    server.start()
    original = client_module.decode_response
    monkeypatch.setattr(
        client_module,
        "decode_response",
        lambda payload: replace(original(payload), evolution_operation=False),
    )
    try:
        thread = Thread(target=server.serve_once)
        thread.start()
        result = OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()).infer(
            EvolutionModelRequest("Synthetic input", ()),
            correlation_id="corr",
            operation_id="evolution:" + "a" * 64,
        )
        thread.join(5)
        assert not thread.is_alive()
        assert result.outcome == CLIENT_OUTCOME_UNCERTAIN
    finally:
        server.close()


def test_bound_revision_and_consolidation_preserve_history_and_exact_parents(tmp_path):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    audit = EvolutionAuditStore(provider, events)
    who = principal("a")
    engine = CognitionEngine(
        events,
        SelfStateStore(events),
        capabilities=CognitionCapabilities(
            private_state_write=False,
            generic_memory_write=True,
            private_consolidation=True,
        ),
    )
    first = append_event(events, who, "one", "I plan to learn Italian in 2027")
    second = append_event(events, who, "two", "I collect Italian maps")
    created = {}
    for operation, source, statement in (
        ("memory:first", first, "Learn Italian in 2027"),
        ("memory:second", second, "Collect Italian maps"),
    ):
        outcome = engine.commit_worker_extraction(
            who,
            source,
            operation,
            ExtractionProposal("new_memory", "candidate_useful_memory", statement, 0.8),
        )
        assert outcome.status is CognitionOutcomeStatus.PERSISTED
        created[operation] = outcome.generic_revision[0]
    correction = append_event(
        events, who, "three", "Correction: I plan to learn Italian in 2028"
    )
    received = audit.reserve(
        "evolution:revision",
        who,
        correction,
        "Correction: I plan to learn Italian in 2028",
    )
    assert received.request is not None and len(received.snapshot.selected) == 2
    original = next(
        row for row in received.snapshot.selected if row[1] == created["memory:first"]
    )
    proposal = EvolutionProposal(
        "revise_memory",
        (original[0],),
        "candidate_useful_memory",
        "Learn Italian in 2028",
        0.8,
    )
    audit.receive(
        "evolution:revision",
        who.principal_id.value,
        correction,
        "Correction: I plan to learn Italian in 2028",
        proposal,
    )
    replay = audit.read(
        "evolution:revision",
        who.principal_id.value,
        correction,
        "Correction: I plan to learn Italian in 2028",
    )
    assert replay.status == "received" and replay.snapshot == received.snapshot
    outcome = engine.commit_worker_evolution(
        who, correction, "evolution:revision", replay.snapshot, replay.proposal
    )
    assert outcome.status is CognitionOutcomeStatus.PERSISTED
    assert outcome.generic_revision == (original[1], 2)
    assert (
        engine.commit_worker_evolution(
            who, correction, "evolution:revision", replay.snapshot, replay.proposal
        ).generic_revision
        == outcome.generic_revision
    )
    audit.finish("evolution:revision", "accepted")
    assert (
        audit.read(
            "evolution:revision",
            who.principal_id.value,
            correction,
            "Correction: I plan to learn Italian in 2028",
        ).status
        == "accepted"
    )
    combined = append_event(events, who, "four", "I use Italian maps while studying")
    candidate = audit.reserve(
        "evolution:combined", who, combined, "I use Italian maps while studying"
    )
    handles = tuple(row[0] for row in candidate.snapshot.selected)
    consolidated = EvolutionProposal(
        "consolidate_memories",
        handles,
        "candidate_useful_memory",
        "Study Italian using maps",
        0.7,
    )
    result = engine.commit_worker_evolution(
        who, combined, "evolution:combined", candidate.snapshot, consolidated
    )
    assert result.status is CognitionOutcomeStatus.PERSISTED
    with events._open() as db:
        parents = db.execute(
            "SELECT parent_operation_id FROM generic_memory_parents WHERE operation_id='evolution:combined' ORDER BY parent_operation_id"
        ).fetchall()
        assert parents == [("evolution:revision",), ("memory:second",)]
        assert db.execute(
            "SELECT revision FROM generic_memory_operations WHERE item_id=? ORDER BY revision",
            (original[1],),
        ).fetchall() == [(1,), (2,)]
    current = GenericMemoryStore(events).current(who)
    assert len(current) == 3
    assert all(
        read.view.verification is VerificationStatus.UNVERIFIED for read in current
    )


def _evolution_worker(
    socket_path, database_path, ready, count, pilot=None, entered=None, release=None
):
    class FakeSDK:
        def __init__(self):
            self.requests = []

        def create(self, **kwargs):
            self.requests.append(kwargs)
            data = json.loads(kwargs["input"])
            if data["task"] == "evolve_private_memory":
                if entered is not None:
                    entered.set()
                    if not release.wait(10):
                        raise RuntimeError("synthetic worker was not released")
                content = data["participant_input"]
                shown = data["memories"]
                if content.startswith("No change"):
                    proposal = {
                        "decision": "no_change",
                        "handles": [],
                        "kind": None,
                        "statement": None,
                        "confidence": None,
                    }
                elif content.startswith("Invalid confidence"):
                    proposal = {
                        "decision": "new_memory",
                        "handles": [],
                        "kind": "candidate_useful_memory",
                        "statement": "Invalid synthetic output",
                        "confidence": 10**400,
                    }
                elif content.startswith("Correction"):
                    decision, handles, statement = (
                        "revise_memory",
                        [
                            next(
                                item["handle"]
                                for item in shown
                                if "2027" in item["statement"]
                            )
                        ],
                        "Learn Italian in 2028",
                    )
                elif content.startswith("I use maps"):
                    decision, handles, statement = (
                        "consolidate_memories",
                        [item["handle"] for item in shown],
                        "Study Italian using maps",
                    )
                else:
                    decision, handles, statement = "new_memory", [], content
                if not content.startswith(("No change", "Invalid confidence")):
                    proposal = {
                        "decision": decision,
                        "handles": handles,
                        "kind": "candidate_useful_memory",
                        "statement": statement,
                        "confidence": 0.8,
                    }
            else:
                proposal = {
                    "decision": "reply",
                    "reason_category": None,
                    "content": "Да, я помню итальянский язык.",
                    "brief": None,
                    "draft": None,
                }
            return {
                "id": "synthetic-response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": json.dumps(proposal)}
                        ],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }

    sdk = FakeSDK()
    provider_store = ProviderStateStore(EventStore(Path(database_path)))
    gateway = TrustedOpenAIGateway(
        store=provider_store,
        transport=OpenAIResponsesTransport(SimpleNamespace(responses=sdk)),
        config=provider_config()
        if pilot is not None
        else replace(
            default_openai_provider_config(),
            deployment_stage=DeploymentStage.STAGE_1,
            paid_calls_enabled=True,
            max_attempts=1,
        ),
    )
    pilot_gate = None
    if pilot is not None:
        activation_path, allowance_path, stop_path, permission_path = map(Path, pilot)
        activation = load_activation(activation_path)
        pilot_gate = PilotRequestGate(
            activation,
            PilotAllowance(allowance_path, activation),
            activation_path=activation_path,
            stop=PilotStop(stop_path),
            evolution_permission_path=permission_path,
            provider_spend_suspended=provider_store.is_spend_suspended,
        )
    server = OpenAIWorkerServer(
        WorkerServerConfig(Path(socket_path), frozenset({os.getuid()})),
        gateway,
        evolution_enabled=True,
        request_guard=None if pilot_gate is None else pilot_gate.authorize,
        evolution_commit_guard=None
        if pilot_gate is None
        else pilot_gate.evolution_commit_allowed,
    )
    try:
        server.start()
        ready.send(True)
        server.serve(max_connections=count)
        ready.send(sdk.requests)
    finally:
        server.close()
        ready.close()


def test_authenticated_http_worker_revises_consolidates_and_restart_retrieves(tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    _auth, events, ledger, gate, app, a, b = _app(tmp_path, socket_path)
    app._conversations = WorkerConversationRuntime(
        events,
        gate,
        OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
        provider_state=ProviderStateStore(EventStore(tmp_path / "ipc.db")),
        memory_evolution_enabled=True,
        context_limits=ContextLimits(32000, 1),
    )
    parent, child = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_evolution_worker,
        args=(str(socket_path), str(tmp_path / "provider.db"), child, 10),
    )
    process.start()
    child.close()
    try:
        assert parent.poll(5) and parent.recv() is True
        for index, content in enumerate(
            (
                "I plan to learn Italian in 2027",
                "I collect Italian maps",
                "Correction: I plan to learn Italian in 2028",
                "I use maps while studying Italian",
            )
        ):
            assert (
                json.loads(a.submit(str(index), content)[2])["model_state"]
                == "completed"
            )
        assert (
            json.loads(b.submit("other", "I learn French")[2])["model_state"]
            == "completed"
        )
        assert a.submit("0", "I plan to learn Italian in 2027")[0] == "200 OK"
        assert parent.poll(5)
        requests = parent.recv()
        assert len(requests) == 10
        projections = [
            json.loads(item["input"])
            for item in requests
            if json.loads(item["input"])["task"] == "evolve_private_memory"
        ]
        assert len(projections) == 5
        assert projections[0]["memories"] == []
        assert len(projections[2]["memories"]) == 2
        assert {item["statement"] for item in projections[3]["memories"]} == {
            "Learn Italian in 2028",
            "I collect Italian maps",
        }
        assert projections[4]["participant_input"] == "I learn French"
        assert projections[4]["memories"] == []
        for item in requests:
            assert item["tools"] == [] and item["store"] is False
        for item in requests:
            if json.loads(item["input"])["task"] == "evolve_private_memory":
                assert "untrusted data" in item["instructions"]
                assert (
                    item["text"]["format"]["name"]
                    == "private_memory_evolution_proposal"
                )
                assert "principal_id" not in item["input"]
                assert "source_event_id" not in item["input"]
                assert "item_id" not in item["input"]
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
    with events._open() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM generic_memory_operations"
        ).fetchone() == (5,)
        assert db.execute("SELECT COUNT(*) FROM generic_memory_parents").fetchone() == (
            2,
        )
    reopened = EventStore(events.database_path)
    reopened_gate = PrivacyReadGate(ledger, reopened)
    assert reopened_gate.reconcile()
    app._conversations = WorkerConversationRuntime(
        reopened,
        reopened_gate,
        OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
        provider_state=ProviderStateStore(EventStore(tmp_path / "ipc.db")),
        context_limits=ContextLimits(32000, 1),
    )
    parent, child = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_evolution_worker,
        args=(str(socket_path), str(tmp_path / "provider.db"), child, 1),
    )
    process.start()
    child.close()
    try:
        assert parent.poll(5) and parent.recv() is True
        assert (
            json.loads(a.submit("after", "What do you remember?")[2])["model_state"]
            == "completed"
        )
        assert parent.poll(5)
        projection = json.loads(parent.recv()[0]["input"])
        assert "Study Italian using maps" in json.dumps(projection)
        assert "I learn French" not in json.dumps(projection)
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()


def test_exposed_unselected_context_is_erased_and_old_backup_cannot_restore(tmp_path):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    audit = EvolutionAuditStore(provider, events)
    who = principal("a")
    engine = CognitionEngine(
        events,
        SelfStateStore(events),
        capabilities=CognitionCapabilities(
            private_state_write=False,
            generic_memory_write=True,
            private_consolidation=True,
        ),
    )
    created = []
    for name in ("one", "two", "three"):
        source = append_event(events, who, name, f"Synthetic fact {name}")
        outcome = engine.commit_worker_extraction(
            who,
            source,
            f"memory:{name}",
            ExtractionProposal(
                "new_memory", "candidate_useful_memory", f"Synthetic memory {name}", 0.7
            ),
        )
        assert outcome.status is CognitionOutcomeStatus.PERSISTED
        created.append((source, outcome.generic_revision[0]))
    source = append_event(events, who, "combined", "Combine first and second")
    reserved = audit.reserve(
        "evolution:combined", who, source, "Combine first and second"
    )
    assert len(reserved.snapshot.selected) == 3
    handles = tuple(
        next(row[0] for row in reserved.snapshot.selected if row[1] == item)
        for _event, item in created[:2]
    )
    proposal = EvolutionProposal(
        "consolidate_memories",
        handles,
        "candidate_useful_memory",
        "Combined synthetic memory",
        0.8,
    )
    assert (
        engine.commit_worker_evolution(
            who, source, "evolution:combined", reserved.snapshot, proposal
        ).status
        is CognitionOutcomeStatus.PERSISTED
    )
    with events._open() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM generic_memory_context WHERE operation_id='evolution:combined'"
        ).fetchone() == (3,)
    pending = audit.reserve(
        "evolution:pending",
        who,
        append_event(events, who, "pending", "Pending change"),
        "Pending change",
    )
    assert created[2][0] in pending.snapshot.sources
    backup = tmp_path / "before-deletion.db"
    create_sqlite_backup(
        events.database_path, backup, 11, datetime(2030, 1, 1, tzinfo=UTC)
    )
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    assert (
        DeletionCoordinator(ledger, EventStoreErasureAdapter(events))
        .delete(deletion_context(), deletion_request(content_reference=created[2][0]))
        .disposition.value
        == "applied"
    )
    with events._open() as db:
        assert (
            db.execute(
                "SELECT 1 FROM generic_memory_payloads WHERE operation_id='evolution:combined'"
            ).fetchone()
            is None
        )
        assert (
            db.execute(
                "SELECT 1 FROM private_extraction_receipts WHERE operation_id='evolution:pending'"
            ).fetchone()
            is None
        )
    assert (
        audit.read(
            "evolution:pending",
            who.principal_id.value,
            "event-pending",
            "Pending change",
        ).status
        == "unavailable"
    )
    quarantine = tmp_path / "quarantine.db"
    assert (
        QuarantinedRestore(ledger, 11, EventStoreRestoreAdapter)
        .restore(backup, quarantine, datetime(2030, 1, 2, tzinfo=UTC))
        .activation_eligible
    )
    with sqlite3.connect(f"{quarantine.as_uri()}?mode=ro", uri=True) as db:
        assert (
            db.execute(
                "SELECT 1 FROM generic_memory_payloads WHERE operation_id='evolution:combined'"
            ).fetchone()
            is None
        )
        assert (
            db.execute(
                "SELECT 1 FROM private_extraction_receipts WHERE operation_id='evolution:pending'"
            ).fetchone()
            is None
        )


def test_evolution_requires_own_protected_permission_and_bounded_allowance(tmp_path):
    activation_path, _ = activation_file(tmp_path)
    activation = load_activation(activation_path)
    allowance = PilotAllowance.initialize(tmp_path / "allowance.db", activation)
    stop_path = activation_path.parent / "stop.json"
    stop_path.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": False})
    )
    stop_path.chmod(0o400)
    extraction_path = activation_path.parent / "extraction.json"
    extraction_path.write_text(
        json.dumps(
            {
                "version": "private-memory-extraction-permission-v1",
                "pilot_id": activation.pilot_id,
                "max_operations": 5,
            }
        )
    )
    extraction_path.chmod(0o400)
    evolution_path = activation_path.parent / "evolution.json"
    evolution_path.write_text(
        json.dumps(
            {
                "version": "private-memory-evolution-permission-v1",
                "pilot_id": activation.pilot_id,
                "max_operations": 1,
            }
        )
    )
    evolution_path.chmod(0o400)
    request = InferenceRequest(
        "evolution:" + "a" * 64,
        "corr",
        EvolutionModelRequest("Synthetic input", ()),
    )
    gate = PilotRequestGate(
        activation,
        allowance,
        activation_path=activation_path,
        stop=PilotStop(stop_path),
        extraction_permission_path=extraction_path,
    )
    assert gate.authorize(request).outcome == "authorization_rejected"
    gate.evolution_permission_path = evolution_path
    assert gate.authorize(request) is None
    assert (
        gate.authorize(
            InferenceRequest("evolution:" + "b" * 64, "corr", request.model_request)
        ).outcome
        == "authorization_rejected"
    )
    assert gate.evolution_commit_allowed()
    evolution_path.chmod(0o600)
    evolution_path.write_text("{}")
    evolution_path.chmod(0o400)
    assert not gate.evolution_commit_allowed()
    assert (
        gate.authorize(
            InferenceRequest("evolution:" + "c" * 64, "corr", request.model_request)
        ).outcome
        == "authorization_rejected"
    )
    evolution_path.chmod(0o600)
    evolution_path.write_text(
        json.dumps(
            {
                "version": "private-memory-evolution-permission-v1",
                "pilot_id": activation.pilot_id,
                "max_operations": 1,
            }
        )
    )
    evolution_path.chmod(0o400)
    gate.provider_spend_suspended = lambda: True
    assert gate.authorize(request).outcome == "authorization_rejected"
    assert not gate.evolution_commit_allowed()

    def unavailable_spend():
        raise ProviderPersistenceError("synthetic store failure")

    gate.provider_spend_suspended = unavailable_spend
    assert gate.authorize(request).outcome == "authorization_rejected"
    assert not gate.evolution_commit_allowed()
    gate.provider_spend_suspended = lambda: False
    stop_path.chmod(0o600)
    stop_path.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": True})
    )
    stop_path.chmod(0o400)
    assert gate.authorize(request).outcome == "emergency_stop"
    assert not gate.evolution_commit_allowed()


def test_production_worker_builder_requires_evolution_permission_file(
    tmp_path, monkeypatch
):
    activation_path, _ = activation_file(tmp_path)
    activation = load_activation(activation_path)
    allowance_path = tmp_path / "allowance.db"
    PilotAllowance.initialize(allowance_path, activation)
    provider_path = tmp_path / "provider.db"
    EventStore(provider_path)
    stop_path = activation_path.parent / "stop.json"
    stop_path.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": False})
    )
    stop_path.chmod(0o400)
    permission_path = activation_path.parent / "evolution.json"
    permission_path.write_text(
        json.dumps(
            {
                "version": "private-memory-evolution-permission-v1",
                "pilot_id": activation.pilot_id,
                "max_operations": 1,
            }
        )
    )
    permission_path.chmod(0o400)
    monkeypatch.setattr(
        private_pilot_worker,
        "build_worker_openai_transport",
        lambda **_scope: OpenAIResponsesTransport(SimpleNamespace(responses=object())),
    )
    worker = private_pilot_worker.build_worker(
        socket_path=tmp_path / "worker.sock",
        provider_database=provider_path,
        allowance_database=allowance_path,
        activation_path=activation_path,
        stop_path=stop_path,
        evolution_permission_path=permission_path,
    )
    assert worker._evolution_enabled and not worker._extraction_enabled
    request = InferenceRequest(
        "evolution:" + "a" * 64, "corr", EvolutionModelRequest("Synthetic input", ())
    )
    assert worker._request_guard(request) is None
    assert worker._evolution_commit_guard()


def test_no_change_unknown_cross_principal_handle_and_stale_snapshot(tmp_path):
    events = EventStore(tmp_path / "events.db")
    audit = EvolutionAuditStore(
        ProviderStateStore(EventStore(tmp_path / "ipc.db")), events
    )
    engine = CognitionEngine(
        events,
        SelfStateStore(events),
        capabilities=CognitionCapabilities(
            private_state_write=False,
            generic_memory_write=True,
            private_consolidation=True,
        ),
    )
    alice, bob = principal("a"), principal("b")
    alice_source = append_event(events, alice, "alice-one", "I learn Italian")
    bob_source = append_event(events, bob, "bob-one", "I learn French")
    for who, source, operation, statement in (
        (alice, alice_source, "memory:alice", "Alice learns Italian"),
        (bob, bob_source, "memory:bob", "Bob learns French"),
    ):
        assert (
            engine.commit_worker_extraction(
                who,
                source,
                operation,
                ExtractionProposal(
                    "new_memory", "candidate_useful_memory", statement, 0.8
                ),
            ).status
            is CognitionOutcomeStatus.PERSISTED
        )
    alice_change = append_event(events, alice, "alice-two", "Correction to my plan")
    bob_change = append_event(events, bob, "bob-two", "Correction to my plan")
    alice_snapshot = audit.reserve(
        "evolution:" + "a" * 64, alice, alice_change, "Correction to my plan"
    ).snapshot
    bob_snapshot = audit.reserve(
        "evolution:" + "b" * 64, bob, bob_change, "Correction to my plan"
    ).snapshot
    assert alice_snapshot.selected[0][0] != bob_snapshot.selected[0][0]
    forged = EvolutionProposal(
        "revise_memory",
        (bob_snapshot.selected[0][0],),
        "candidate_useful_memory",
        "Forged",
        0.8,
    )
    assert (
        engine.commit_worker_evolution(
            alice, alice_change, "evolution:" + "a" * 64, alice_snapshot, forged
        ).reason
        is CognitionReason.INVALID_PROPOSAL
    )
    second_input = append_event(events, alice, "alice-other", "A separate operation")
    second_snapshot = audit.reserve(
        "evolution:" + "c" * 64, alice, second_input, "A separate operation"
    ).snapshot
    assert second_snapshot.selected[0][0] != alice_snapshot.selected[0][0]
    mismatched = EvolutionProposal(
        "revise_memory",
        (alice_snapshot.selected[0][0],),
        "candidate_useful_memory",
        "Forged across operations",
        0.8,
    )
    assert (
        engine.commit_worker_evolution(
            alice, second_input, "evolution:" + "c" * 64, second_snapshot, mismatched
        ).reason
        is CognitionReason.INVALID_PROPOSAL
    )
    no_change = EvolutionProposal("no_change", (), None, None, None)
    assert (
        engine.commit_worker_evolution(
            alice, alice_change, "evolution:" + "a" * 64, alice_snapshot, no_change
        ).status
        is CognitionOutcomeStatus.NO_CHANGE
    )
    later = append_event(events, alice, "alice-three", "I read Italian books")
    assert (
        engine.commit_worker_extraction(
            alice,
            later,
            "memory:later",
            ExtractionProposal(
                "new_memory",
                "candidate_useful_memory",
                "Alice reads Italian books",
                0.8,
            ),
        ).status
        is CognitionOutcomeStatus.PERSISTED
    )
    valid = EvolutionProposal(
        "revise_memory",
        (alice_snapshot.selected[0][0],),
        "candidate_useful_memory",
        "Alice learns Italian in 2028",
        0.8,
    )
    assert (
        engine.commit_worker_evolution(
            alice, alice_change, "evolution:" + "a" * 64, alice_snapshot, valid
        ).reason
        is CognitionReason.STALE_REVISION
    )
    assert len(GenericMemoryStore(events).current(alice)) == 2


def test_application_revalidates_typed_evolution_before_source_or_store_access(
    tmp_path,
):
    events = EventStore(tmp_path / "events.db")
    who = principal("a")
    source = append_event(events, who, "one", "Synthetic input")
    snapshot = EvolutionSnapshot((), (), ())
    no_change = EvolutionProposal("no_change", (), None, None, None)
    disabled = CognitionEngine(events, SelfStateStore(events))
    assert (
        disabled.commit_worker_evolution(
            who, source, "evolution:one", snapshot, no_change
        ).reason
        is CognitionReason.CAPABILITY_DISABLED
    )
    enabled = CognitionEngine(
        events,
        SelfStateStore(events),
        capabilities=CognitionCapabilities(generic_memory_write=True),
    )
    assert (
        enabled.commit_worker_evolution(
            who, source, "evolution:one", snapshot, "forged"
        ).reason
        is CognitionReason.INVALID_PROPOSAL
    )
    assert (
        enabled.commit_worker_evolution(
            who, source, "bad operation", snapshot, no_change
        ).reason
        is CognitionReason.INVALID_PROPOSAL
    )
    invalid = EvolutionProposal("no_change", (), None, None, 0.8)
    assert (
        enabled.commit_worker_evolution(
            who, source, "evolution:one", snapshot, invalid
        ).reason
        is CognitionReason.INVALID_PROPOSAL
    )
    noncanonical = EvolutionProposal(
        "new_memory", [], "candidate_useful_memory", "Synthetic memory", 0.8
    )
    assert (
        enabled.commit_worker_evolution(
            who, source, "evolution:one", snapshot, noncanonical
        ).reason
        is CognitionReason.INVALID_PROPOSAL
    )
    assert (
        enabled.commit_worker_evolution(
            who, "event-missing", "evolution:one", snapshot, no_change
        ).reason
        is CognitionReason.SOURCE_NOT_FOUND
    )


@pytest.mark.parametrize(
    "crash_point", ["after_extraction_receipt", "after_memory_store_commit"]
)
def test_reconcile_received_evolution_without_repeating_provider_or_revision(
    tmp_path, crash_point
):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, events)
    assert gate.reconcile()
    who = principal("a")
    old_source = append_event(events, who, "old", "I plan Italian in 2027")
    engine = CognitionEngine(
        events,
        SelfStateStore(events),
        capabilities=CognitionCapabilities(
            private_state_write=False, generic_memory_write=True
        ),
    )
    assert (
        engine.commit_worker_extraction(
            who,
            old_source,
            "memory:old",
            ExtractionProposal(
                "new_memory", "candidate_useful_memory", "Italian in 2027", 0.8
            ),
        ).status
        is CognitionOutcomeStatus.PERSISTED
    )
    source = append_event(events, who, "new", "Correction: Italian in 2028")
    calls = []

    def checkpoint(point):
        if point == crash_point:
            raise RuntimeError("synthetic crash")

    client = OpenAIWorkerClient(
        tmp_path / "absent.sock", expected_worker_uid=os.getuid()
    )

    def infer(request, *, correlation_id, operation_id):
        calls.append(operation_id)
        return ClientResult(
            operation_id,
            correlation_id,
            "completed",
            1,
            None,
            evolution=EvolutionProposal(
                "revise_memory",
                (request.memories[0].handle,),
                "candidate_useful_memory",
                "Italian in 2028",
                0.8,
            ),
        )

    client.infer = infer
    runtime = WorkerConversationRuntime(
        events,
        gate,
        client,
        provider_state=provider,
        memory_evolution_enabled=True,
        checkpoint=checkpoint,
    )
    reply = "reply:" + "a" * 64
    with pytest.raises(RuntimeError, match="synthetic crash"):
        runtime._evolve_worker_memory(
            who, source, "Correction: Italian in 2028", "corr", reply
        )
    operation = "evolution:" + "a" * 64
    assert (
        runtime._evolution_audit.read(
            operation, who.principal_id.value, source, "Correction: Italian in 2028"
        ).status
        == "received"
    )
    assert calls == [operation]
    resumed = WorkerConversationRuntime(
        events, gate, client, provider_state=provider, memory_evolution_enabled=True
    )
    if crash_point == "after_extraction_receipt":
        resumed._evolve_worker_memory(
            who, source, "Correction: Italian in 2028", "corr", reply
        )
    else:
        resumed._reconcile_worker_evolution(
            who, source, "Correction: Italian in 2028", reply
        )
    assert (
        resumed._evolution_audit.read(
            operation, who.principal_id.value, source, "Correction: Italian in 2028"
        ).status
        == "accepted"
    )
    assert calls == [operation]
    with events._open() as db:
        assert db.execute(
            "SELECT revision FROM generic_memory_operations WHERE item_id=(SELECT item_id FROM generic_memory_operations WHERE operation_id='memory:old') ORDER BY revision"
        ).fetchall() == [(1,), (2,)]


@pytest.mark.parametrize("blocked", ["stop", "spend", "session"])
def test_application_blocks_evolution_before_provider_and_after_receipt(
    tmp_path, blocked
):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, events)
    assert gate.reconcile()
    client = OpenAIWorkerClient(
        tmp_path / "absent.sock", expected_worker_uid=os.getuid()
    )
    client.infer = lambda *_args, **_kwargs: pytest.fail("provider was called")
    runtime = WorkerConversationRuntime(
        events, gate, client, provider_state=provider, memory_evolution_enabled=True
    )
    who = principal("a")
    source = append_event(events, who, "one", "Synthetic memory")
    operation = "evolution:" + "a" * 64
    reply = "reply:" + "a" * 64
    if blocked == "stop":
        runtime._stop = lambda: True
    elif blocked == "spend":
        runtime._provider.is_spend_suspended = lambda: True
    else:
        runtime._current_request.set(lambda: False)
    runtime._evolve_worker_memory(who, source, "Synthetic memory", "corr", reply)
    assert (
        runtime._evolution_audit.read(
            operation, who.principal_id.value, source, "Synthetic memory"
        )
        is None
    )
    runtime._evolution_audit.reserve(operation, who, source, "Synthetic memory")
    runtime._evolution_audit.receive(
        operation,
        who.principal_id.value,
        source,
        "Synthetic memory",
        EvolutionProposal(
            "new_memory", (), "candidate_useful_memory", "Synthetic memory", 0.8
        ),
    )
    received = runtime._evolution_audit.read(
        operation, who.principal_id.value, source, "Synthetic memory"
    )
    runtime._commit_worker_evolution(who, source, operation, received)
    assert (
        runtime._evolution_audit.read(
            operation, who.principal_id.value, source, "Synthetic memory"
        ).status
        == "unavailable"
    )
    assert GenericMemoryStore(events).current(who) == ()


def test_evolution_receipt_rejects_corruption_and_reconciles_cross_store_windows(
    tmp_path,
):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    audit = EvolutionAuditStore(provider, events)
    who = principal("a")
    source = append_event(events, who, "one", "Synthetic input")
    operation = "evolution:" + "a" * 64
    reserved = audit.reserve(operation, who, source, "Synthetic input")
    assert audit.reserve(operation, who, source, "Synthetic input").request is None
    with events._open() as db:
        original = db.execute(
            "SELECT proposal FROM private_extraction_receipts WHERE operation_id=?",
            (operation,),
        ).fetchone()[0]
    envelope = json.loads(original)
    corrupt = (
        "x" * 131_073,
        json.dumps({**envelope, "version": "forged"}),
        json.dumps({**envelope, "snapshot": []}),
        json.dumps({**envelope, "snapshot": {**envelope["snapshot"], "sources": [1]}}),
        json.dumps(
            {
                **envelope,
                "snapshot": {
                    **envelope["snapshot"],
                    "selected": [["h1", "unknown", 1]],
                },
            }
        ),
    )
    for text in corrupt:
        with events._open() as db:
            db.execute(
                "UPDATE private_extraction_receipts SET proposal=? WHERE operation_id=?",
                (text, operation),
            )
        with pytest.raises(ValueError, match="invalid evolution receipt"):
            audit.read(operation, who.principal_id.value, source, "Synthetic input")
    with events._open() as db:
        db.execute(
            "UPDATE private_extraction_receipts SET proposal=? WHERE operation_id=?",
            (original, operation),
        )
        db.execute(
            "UPDATE private_extraction_receipts SET source_principal_id='forged' WHERE operation_id=?",
            (operation,),
        )
    with pytest.raises(ValueError, match="receipt conflict"):
        audit.read(operation, who.principal_id.value, source, "Synthetic input")
    with events._open() as db:
        db.execute(
            "UPDATE private_extraction_receipts SET source_principal_id=? WHERE operation_id=?",
            (who.principal_id.value, operation),
        )
    with pytest.raises(ValueError, match="identity conflict"):
        audit.read(
            operation, principal("b").principal_id.value, source, "Synthetic input"
        )
    proposal = EvolutionProposal(
        "new_memory", (), "candidate_useful_memory", "Synthetic memory", 0.8
    )
    audit.receive(
        operation, who.principal_id.value, source, "Synthetic input", proposal
    )
    with pytest.raises(ValueError, match="receipt conflict"):
        audit.receive(
            operation, who.principal_id.value, source, "Synthetic input", proposal
        )
    with provider._event_store._open() as db:
        db.execute(
            "UPDATE private_extraction_audit SET status='reserved' WHERE operation_id=?",
            (operation,),
        )
    assert (
        audit.read(operation, who.principal_id.value, source, "Synthetic input").status
        == "received"
    )
    with pytest.raises(ValueError, match="receipt conflict"):
        audit.receive(
            operation, who.principal_id.value, source, "Synthetic input", proposal
        )
    with events._open() as db:
        db.execute(
            "DELETE FROM private_extraction_receipts WHERE operation_id=?", (operation,)
        )
    assert (
        audit.read(operation, who.principal_id.value, source, "Synthetic input").status
        == "unavailable"
    )
    assert reserved.snapshot.selected == ()
    missing_proposal = "evolution:" + "b" * 64
    audit.reserve(missing_proposal, who, source, "Synthetic input")
    with provider._event_store._open() as db:
        db.execute(
            "UPDATE private_extraction_audit SET status='received' WHERE operation_id=?",
            (missing_proposal,),
        )
    assert (
        audit.read(
            missing_proposal, who.principal_id.value, source, "Synthetic input"
        ).status
        == "unavailable"
    )
    with events._open() as db:
        assert (
            db.execute(
                "SELECT 1 FROM private_extraction_receipts WHERE operation_id=?",
                (missing_proposal,),
            ).fetchone()
            is None
        )
    missing_receipt = "evolution:" + "c" * 64
    audit.reserve(missing_receipt, who, source, "Synthetic input")
    with events._open() as db:
        db.execute(
            "DELETE FROM private_extraction_receipts WHERE operation_id=?",
            (missing_receipt,),
        )
    with pytest.raises(ValueError, match="receipt unavailable"):
        audit.receive(
            missing_receipt, who.principal_id.value, source, "Synthetic input", proposal
        )
    assert (
        audit.read(
            missing_receipt, who.principal_id.value, source, "Synthetic input"
        ).status
        == "unavailable"
    )
    with provider._event_store._open() as db:
        assert db.execute(
            "SELECT status FROM private_extraction_audit WHERE operation_id=?",
            (missing_receipt,),
        ).fetchone() == ("unavailable",)
    finalized = "evolution:" + "d" * 64
    audit.reserve(finalized, who, source, "Synthetic input")
    with provider._event_store._open() as db:
        db.execute(
            "UPDATE private_extraction_audit SET status='rejected' WHERE operation_id=?",
            (finalized,),
        )
    with pytest.raises(ValueError, match="receipt conflict"):
        audit.receive(
            finalized, who.principal_id.value, source, "Synthetic input", proposal
        )


def test_selection_enforces_record_and_serialized_byte_bounds(tmp_path):
    events = EventStore(tmp_path / "events.db")
    audit = EvolutionAuditStore(
        ProviderStateStore(EventStore(tmp_path / "ipc.db")), events
    )
    engine = CognitionEngine(
        events,
        SelfStateStore(events),
        capabilities=CognitionCapabilities(
            private_state_write=False, generic_memory_write=True
        ),
    )
    who = principal("a")
    for index in range(9):
        source = append_event(events, who, f"short-{index}", f"Synthetic note {index}")
        assert (
            engine.commit_worker_extraction(
                who,
                source,
                f"memory:short-{index}",
                ExtractionProposal(
                    "new_memory",
                    "candidate_useful_memory",
                    f"Short memory {index}",
                    0.8,
                ),
            ).status
            is CognitionOutcomeStatus.PERSISTED
        )
    input_source = append_event(events, who, "short-query", "What do you recall?")
    selected = audit.reserve(
        "evolution:" + "1" * 64, who, input_source, "What do you recall?"
    )
    assert len(selected.request.memories) == 8
    assert len(selected.snapshot.visible) == 9
    assert len({item.handle for item in selected.request.memories}) == 8
    assert len(selected.request.serialize().encode()) <= 16_384
    for index in range(2):
        source = append_event(events, who, f"long-{index}", f"Long note {index}")
        assert (
            engine.commit_worker_extraction(
                who,
                source,
                f"memory:long-{index}",
                ExtractionProposal(
                    "new_memory", "candidate_useful_memory", chr(65 + index) * 4000, 0.8
                ),
            ).status
            is CognitionOutcomeStatus.PERSISTED
        )
    large_input = "z" * 8000
    source = append_event(events, who, "long-query", large_input)
    bounded = audit.reserve("evolution:" + "2" * 64, who, source, large_input)
    assert len(bounded.request.serialize().encode()) <= 16_384
    assert len(bounded.request.memories) < 11
    assert len(bounded.snapshot.selected) == len(bounded.request.memories)


def test_selection_omits_memory_whose_complete_source_union_exceeds_store_bound(
    tmp_path,
):
    events = EventStore(tmp_path / "events.db")
    audit = EvolutionAuditStore(
        ProviderStateStore(EventStore(tmp_path / "ipc.db")), events
    )
    who = principal("a")
    sources = tuple(
        append_event(events, who, f"many-{index}", f"Synthetic source {index}")
        for index in range(100)
    )
    proposal = GenericMemoryProposal(
        ProposalContext(
            "memory:many",
            sources[0],
            sources[1:],
            (),
            None,
            "synthetic_many_sources",
            NOW,
            NOW + timedelta(days=30),
        ),
        MemoryKind.CANDIDATE_USEFUL_MEMORY,
        "Memory with many sources",
        0.8,
        VerificationStatus.UNVERIFIED,
        item_id="memory:many",
    )
    assert GenericMemoryStore(events).commit(
        who,
        proposal,
        model_identifier="worker-owned",
        prompt_version="synthetic-v1",
        core_self_version=CORE_SELF_VERSION,
        decided_at=NOW,
        expected_visible_revisions=(),
        expected_candidate_revisions=(),
    ) == ("memory:many", 1)
    content = "New independent input"
    source = append_event(events, who, "query-many", content)
    reserved = audit.reserve("evolution:" + "3" * 64, who, source, content)
    assert reserved.request.memories == ()
    assert reserved.snapshot.visible == (("memory:many", 1),)
    assert reserved.snapshot.sources == ()


def test_selection_counts_provenance_roles_before_provider_projection(tmp_path):
    events = EventStore(tmp_path / "events.db")
    audit = EvolutionAuditStore(
        ProviderStateStore(EventStore(tmp_path / "ipc.db")), events
    )
    who = principal("a")
    memory = GenericMemoryStore(events)
    for item in ("one", "two"):
        sources = tuple(
            append_event(events, who, f"{item}-{index}", f"Synthetic {item} {index}")
            for index in range(49)
        )
        proposal = GenericMemoryProposal(
            ProposalContext(
                f"memory:{item}",
                sources[0],
                sources,
                (),
                None,
                "synthetic_role_bound",
                NOW,
                NOW + timedelta(days=30),
            ),
            MemoryKind.CANDIDATE_USEFUL_MEMORY,
            f"Memory {item}",
            0.8,
            VerificationStatus.UNVERIFIED,
            item_id=f"memory:{item}",
        )
        assert memory.commit(
            who,
            proposal,
            model_identifier="worker-owned",
            prompt_version="synthetic-v1",
            core_self_version=CORE_SELF_VERSION,
            decided_at=NOW,
        ) == (f"memory:{item}", 1)
    source = append_event(events, who, "query-roles", "Consolidate memories")
    reserved = audit.reserve(
        "evolution:" + "4" * 64, who, source, "Consolidate memories"
    )
    assert len(reserved.snapshot.visible) == 2
    assert len(reserved.snapshot.selected) == len(reserved.request.memories) == 1
    assert len(set(reserved.snapshot.sources) | {source}) == 50
    with events._open() as db:
        rows = db.execute(
            "SELECT COUNT(*) FROM generic_memory_sources WHERE operation_id=?",
            (reserved.snapshot.selected[0][1],),
        ).fetchone()[0]
    assert rows == 50


def test_full_generic_ledger_blocks_evolution_before_provider(tmp_path, monkeypatch):
    events, _provider, _gate, client, runtime, who, source = _fault_runtime(tmp_path)
    other = principal("b")
    other_source = append_event(events, other, "full-ledger", "Synthetic memory")
    engine = CognitionEngine(
        events,
        SelfStateStore(events),
        capabilities=CognitionCapabilities(
            private_state_write=False, generic_memory_write=True
        ),
    )
    assert (
        engine.commit_worker_extraction(
            other,
            other_source,
            "memory:full-ledger",
            ExtractionProposal(
                "new_memory", "candidate_useful_memory", "Synthetic memory", 0.8
            ),
        ).status
        is CognitionOutcomeStatus.PERSISTED
    )
    monkeypatch.setattr(evolution_audit_module, "MAX_GENERIC_GRAPH_OPERATIONS", 1)
    client.infer = lambda *_args, **_kwargs: pytest.fail("provider was called")
    runtime._evolve_worker_memory(
        who, source, "Synthetic input", "corr", "reply:" + "a" * 64
    )
    assert (
        runtime._evolution_audit.read(
            "evolution:" + "a" * 64,
            who.principal_id.value,
            source,
            "Synthetic input",
        )
        is None
    )


@pytest.mark.parametrize("change", ["new_head", "erase_context"])
def test_in_flight_snapshot_change_or_context_deletion_cannot_commit(tmp_path, change):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, events)
    assert gate.reconcile()
    who = principal("a")
    engine = CognitionEngine(
        events,
        SelfStateStore(events),
        capabilities=CognitionCapabilities(
            private_state_write=False, generic_memory_write=True
        ),
    )
    old_source = append_event(events, who, "old", "I plan Italian in 2027")
    assert (
        engine.commit_worker_extraction(
            who,
            old_source,
            "memory:old",
            ExtractionProposal(
                "new_memory", "candidate_useful_memory", "Italian in 2027", 0.8
            ),
        ).status
        is CognitionOutcomeStatus.PERSISTED
    )
    other_source = append_event(events, who, "other", "I enjoy maps")
    source = append_event(events, who, "new", "Correction: Italian in 2028")
    client = OpenAIWorkerClient(
        tmp_path / "absent.sock", expected_worker_uid=os.getuid()
    )
    calls = []

    def infer(request, *, correlation_id, operation_id):
        calls.append(operation_id)
        if change == "new_head":
            assert (
                engine.commit_worker_extraction(
                    who,
                    other_source,
                    "memory:other",
                    ExtractionProposal(
                        "new_memory", "candidate_useful_memory", "Enjoys maps", 0.8
                    ),
                ).status
                is CognitionOutcomeStatus.PERSISTED
            )
        else:
            assert (
                DeletionCoordinator(ledger, EventStoreErasureAdapter(events))
                .delete(
                    deletion_context(), deletion_request(content_reference=old_source)
                )
                .disposition.value
                == "applied"
            )
        return ClientResult(
            operation_id,
            correlation_id,
            "completed",
            1,
            None,
            evolution=EvolutionProposal(
                "revise_memory",
                (request.memories[0].handle,),
                "candidate_useful_memory",
                "Italian in 2028",
                0.8,
            ),
        )

    client.infer = infer
    runtime = WorkerConversationRuntime(
        events, gate, client, provider_state=provider, memory_evolution_enabled=True
    )
    runtime._evolve_worker_memory(
        who, source, "Correction: Italian in 2028", "corr", "reply:" + "a" * 64
    )
    assert calls == ["evolution:" + "a" * 64]
    audit = runtime._evolution_audit.read(
        "evolution:" + "a" * 64,
        who.principal_id.value,
        source,
        "Correction: Italian in 2028",
    )
    assert audit.status == "unavailable"
    with events._open() as db:
        assert (
            db.execute(
                "SELECT 1 FROM generic_memory_operations WHERE operation_id=?",
                ("evolution:" + "a" * 64,),
            ).fetchone()
            is None
        )


def test_http_no_change_and_invalid_sdk_output_have_actual_usage_evidence(tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    _auth, events, _ledger, gate, app, browser, _other = _app(tmp_path, socket_path)
    app._conversations = WorkerConversationRuntime(
        events,
        gate,
        OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
        provider_state=ProviderStateStore(EventStore(tmp_path / "ipc.db")),
        memory_evolution_enabled=True,
    )
    parent, child = multiprocessing.Pipe(duplex=False)
    worker = multiprocessing.Process(
        target=_evolution_worker,
        args=(str(socket_path), str(tmp_path / "provider.db"), child, 4),
    )
    worker.start()
    child.close()
    try:
        assert parent.poll(5) and parent.recv() is True
        for message_id, content in (
            ("none", "No change to remember"),
            ("invalid", "Invalid confidence sample"),
        ):
            assert (
                json.loads(browser.submit(message_id, content)[2])["model_state"]
                == "completed"
            )
        assert parent.poll(5) and len(parent.recv()) == 4
        worker.join(5)
        assert worker.exitcode == 0
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        parent.close()
    with events._open() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM generic_memory_operations"
        ).fetchone() == (0,)
    with EventStore(tmp_path / "ipc.db")._open() as db:
        assert db.execute(
            "SELECT status FROM private_extraction_audit ORDER BY status"
        ).fetchall() == [("no_change",), ("rejected",)]
    with EventStore(tmp_path / "provider.db")._open() as db:
        assert db.execute(
            "SELECT outcome,total_tokens FROM provider_usage_attempts ORDER BY outcome"
        ).fetchall() == [
            ("completed", 2),
            ("completed", 2),
            ("completed", 2),
            ("invalid_proposal", 2),
        ]


def _fault_runtime(tmp_path):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, events)
    assert gate.reconcile()
    client = OpenAIWorkerClient(
        tmp_path / "absent.sock", expected_worker_uid=os.getuid()
    )
    runtime = WorkerConversationRuntime(
        events, gate, client, provider_state=provider, memory_evolution_enabled=True
    )
    who = principal("a")
    source = append_event(events, who, "one", "Synthetic input")
    return events, provider, gate, client, runtime, who, source


def test_application_defaults_to_no_evolution_and_selects_one_memory_operation(
    tmp_path,
):
    events, provider, gate, client, _runtime, _who, _source = _fault_runtime(tmp_path)
    default = WorkerConversationRuntime(events, gate, client, provider_state=provider)
    assert default._evolution_audit is None and default._extraction_audit is None
    with pytest.raises(TypeError, match="memory_evolution_enabled must be bool"):
        WorkerConversationRuntime(
            events, gate, client, provider_state=provider, memory_evolution_enabled=1
        )
    selected = WorkerConversationRuntime(
        events,
        gate,
        client,
        provider_state=provider,
        memory_extraction_enabled=True,
        memory_evolution_enabled=True,
    )
    assert selected._evolution_audit is not None and selected._extraction_audit is None


@pytest.mark.parametrize(
    "failure",
    [
        "missing_source",
        "reservation",
        "stop_preflight",
        "missing_preflight",
        "privacy_preflight",
        "invalid_client",
        "stop_after_model",
    ],
)
def test_evolution_admission_and_result_guards_fail_closed(tmp_path, failure):
    events, provider, gate, client, runtime, who, source = _fault_runtime(tmp_path)
    operation = "evolution:" + "a" * 64
    reply = "reply:" + "a" * 64
    if failure == "missing_source":
        runtime._evolve_worker_memory(
            who, "event-missing", "Synthetic input", "corr", reply
        )
        assert (
            runtime._evolution_audit.read(
                operation, who.principal_id.value, source, "Synthetic input"
            )
            is None
        )
        return
    if failure == "reservation":
        provider.reserve_attempt = (
            lambda *_args: AttemptReservationStatus.SPEND_SUSPENDED
        )
    elif failure == "stop_preflight":
        original = provider.reserve_attempt

        def reserve(*args):
            result = original(*args)
            runtime._stop = lambda: True
            return result

        provider.reserve_attempt = reserve
    elif failure == "missing_preflight":
        original = provider.reserve_attempt

        def reserve(*args):
            result = original(*args)
            runtime._worker_cognition._read.retrieve_exact_private_event = (
                lambda *_values: None
            )
            return result

        provider.reserve_attempt = reserve
    elif failure == "privacy_preflight":
        original = gate.authorized
        calls = []

        def authorized(callback):
            calls.append(1)
            if len(calls) == 2:
                raise PrivacyReadBlocked("synthetic privacy change")
            return original(callback)

        gate.authorized = authorized
    elif failure == "invalid_client":
        client.infer = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("synthetic invalid client")
        )
    else:

        def response(request, *, correlation_id, operation_id):
            runtime._stop = lambda: True
            return ClientResult(
                operation_id,
                correlation_id,
                "completed",
                1,
                None,
                evolution=EvolutionProposal(
                    "new_memory", (), "candidate_useful_memory", "Synthetic memory", 0.8
                ),
            )

        client.infer = response
    runtime._evolve_worker_memory(who, source, "Synthetic input", "corr", reply)
    assert runtime._evolution_audit.read(
        operation, who.principal_id.value, source, "Synthetic input"
    ).status in {"unavailable", "rejected"}
    assert GenericMemoryStore(events).current(who) == ()


def test_evolution_commit_rejects_privacy_gate_failure_and_bad_handle(tmp_path):
    events, _provider, gate, _client, runtime, who, source = _fault_runtime(tmp_path)
    operation = "evolution:" + "a" * 64
    runtime._evolution_audit.reserve(operation, who, source, "Synthetic input")
    bad = EvolutionProposal(
        "revise_memory", ("hforged",), "candidate_useful_memory", "Forged", 0.8
    )
    runtime._evolution_audit.receive(
        operation, who.principal_id.value, source, "Synthetic input", bad
    )
    received = runtime._evolution_audit.read(
        operation, who.principal_id.value, source, "Synthetic input"
    )
    runtime._commit_worker_evolution(who, source, operation, received)
    assert (
        runtime._evolution_audit.read(
            operation, who.principal_id.value, source, "Synthetic input"
        ).status
        == "rejected"
    )
    next_op = "evolution:" + "b" * 64
    runtime._evolution_audit.reserve(next_op, who, source, "Synthetic input")
    runtime._evolution_audit.receive(
        next_op,
        who.principal_id.value,
        source,
        "Synthetic input",
        EvolutionProposal(
            "new_memory", (), "candidate_useful_memory", "Synthetic memory", 0.8
        ),
    )
    received = runtime._evolution_audit.read(
        next_op, who.principal_id.value, source, "Synthetic input"
    )
    gate.authorized = lambda _callback: (_ for _ in ()).throw(
        PrivacyReadBlocked("synthetic privacy change")
    )
    runtime._commit_worker_evolution(who, source, next_op, received)
    assert (
        runtime._evolution_audit.read(
            next_op, who.principal_id.value, source, "Synthetic input"
        ).status
        == "unavailable"
    )
    assert GenericMemoryStore(events).current(who) == ()


def test_lost_evolution_response_is_not_retransmitted_after_restart(tmp_path):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, events)
    assert gate.reconcile()
    who = principal("a")
    source = append_event(events, who, "one", "Synthetic memory")
    calls = []
    client = OpenAIWorkerClient(
        tmp_path / "absent.sock", expected_worker_uid=os.getuid()
    )

    def uncertain(_request, *, correlation_id, operation_id):
        calls.append(operation_id)
        return ClientResult(
            operation_id, correlation_id, CLIENT_OUTCOME_UNCERTAIN, 0, None
        )

    client.infer = uncertain
    reply = "reply:" + "a" * 64
    operation = "evolution:" + "a" * 64
    runtime = WorkerConversationRuntime(
        events, gate, client, provider_state=provider, memory_evolution_enabled=True
    )
    runtime._evolve_worker_memory(who, source, "Synthetic memory", "corr", reply)
    reopened = EventStore(events.database_path)
    new_gate = PrivacyReadGate(ledger, reopened)
    assert new_gate.reconcile()
    resumed = WorkerConversationRuntime(
        reopened,
        new_gate,
        client,
        provider_state=provider,
        memory_evolution_enabled=True,
    )
    resumed._evolve_worker_memory(who, source, "Synthetic memory", "corr", reply)
    assert calls == [operation]
    assert (
        resumed._evolution_audit.read(
            operation, who.principal_id.value, source, "Synthetic memory"
        ).status
        == "uncertain"
    )
    assert GenericMemoryStore(reopened).current(who) == ()
    resumed._reconcile_worker_evolution(who, source, "Synthetic memory", reply)


@pytest.mark.parametrize("revocation", ["permission", "stop", "spend"])
def test_worker_rechecks_protected_authority_after_sdk_response(tmp_path, revocation):
    activation_path, _ = activation_file(tmp_path)
    activation = load_activation(activation_path)
    allowance_path = tmp_path / "allowance.db"
    PilotAllowance.initialize(allowance_path, activation)
    stop_path = activation_path.parent / "stop.json"
    stop_path.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": False})
    )
    stop_path.chmod(0o400)
    permission_path = activation_path.parent / "evolution.json"
    permission_path.write_text(
        json.dumps(
            {
                "version": "private-memory-evolution-permission-v1",
                "pilot_id": activation.pilot_id,
                "max_operations": 1,
            }
        )
    )
    permission_path.chmod(0o400)
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    provider_path = tmp_path / "provider.db"
    entered, release = multiprocessing.Event(), multiprocessing.Event()
    parent, child = multiprocessing.Pipe(duplex=False)
    worker = multiprocessing.Process(
        target=_evolution_worker,
        args=(
            str(socket_path),
            str(provider_path),
            child,
            1,
            (activation_path, allowance_path, stop_path, permission_path),
            entered,
            release,
        ),
    )
    worker.start()
    child.close()
    response = {}
    from threading import Thread

    client = OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid())
    thread = Thread(
        target=lambda: response.update(
            value=client.infer(
                EvolutionModelRequest("Synthetic input", ()),
                correlation_id="corr",
                operation_id="evolution:" + "a" * 64,
            )
        )
    )
    try:
        assert parent.poll(5) and parent.recv() is True
        thread.start()
        assert entered.wait(5)
        if revocation == "permission":
            permission_path.chmod(0o600)
            permission_path.write_text("{}")
            permission_path.chmod(0o400)
        elif revocation == "stop":
            stop_path.chmod(0o600)
            stop_path.write_text(
                json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": True})
            )
            stop_path.chmod(0o400)
        else:
            with EventStore(provider_path)._open() as db:
                db.execute(
                    "INSERT INTO provider_spend_suspensions VALUES (?,?,?,?,?)",
                    (
                        "openai",
                        "2030-01-01T00:00:00.000000Z",
                        "project_spend_limit_exceeded",
                        "openai-config-v1",
                        "synthetic-spend",
                    ),
                )
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert response["value"].outcome == "authorization_rejected"
        assert response["value"].evolution is None
        assert parent.poll(5) and len(parent.recv()) == 1
        worker.join(5)
        assert worker.exitcode == 0
        with EventStore(provider_path)._open() as db:
            assert db.execute(
                "SELECT outcome,total_tokens FROM provider_usage_attempts"
            ).fetchall() == [("completed", 2)]
    finally:
        release.set()
        if thread.is_alive():
            thread.join(5)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        parent.close()
