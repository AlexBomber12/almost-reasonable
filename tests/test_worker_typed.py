"""Typed worker proposals stay private and retain contextual source lineage."""

import json
import multiprocessing
import os
import sqlite3
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from dataclasses import replace

import pytest

from app.cognition.contracts import (
    CognitionCapabilities,
    CognitionOutcome,
    CognitionOutcomeStatus,
    CognitionReason,
)
from app.cognition.pipeline import CognitionEngine
from app.cognition.worker_typed import (
    TypedMemory,
    TypedModelRequest,
    TypedProposal,
    parse_typed_proposal,
)
from app.core.events import VerificationStatus
from app.core.provider_spend import AttemptReservationStatus, ProviderStateStore
from app.core.provider_contract import (
    DeploymentStage,
    default_openai_provider_config,
    build_worker_typed_request,
)
from app.core.openai_worker_client import OpenAIWorkerClient
from app.core.context import ContextLimits
from app.core.self_state import BeliefCandidate, InterestCandidate, StateType
from app.core.self_state_store import SelfStateStore, SelfStateValidationError
from app.core.store import EventStore, FaultPoint, schema_objects
from app.private_web.typed_audit import TypedAuditStore
from app.private_web.runtime import WorkerConversationRuntime
from app.private_web.privacy import ParticipantPrivacyControls
from app.privacy.application import (
    ApplicationPrivacyError,
    EventStoreErasureAdapter,
    EventStoreRestoreAdapter,
    PrivacyReadGate,
    PrivacyReadBlocked,
    _validate_graph,
)
from app.privacy.deletion import DeletionCoordinator
from app.privacy.ledger import PrivacyLedger
from app.privacy.recovery import QuarantinedRestore, create_sqlite_backup
from app.trusted.openai_gateway import GatewayOutcome, TrustedOpenAIGateway
from app.trusted.openai_transport import FakeOpenAITransport, OpenAIResponsesTransport
from app.trusted.openai_worker_server import OpenAIWorkerServer, WorkerServerConfig
from app.trusted import openai_worker_server as server_module
from app.trusted.private_pilot import PilotAllowance, load_activation
from app.trusted.private_pilot_worker import PilotRequestGate, PilotStop
from app.trusted.openai_worker_protocol import (
    InferenceRequest,
    WorkerProtocolError,
    WorkerResponse,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)
from test_openai_gateway import response as provider_response
from shared_schema_helpers import remove_v10
from test_int19_worker_runtime import _app
from test_int22_pilot import activation_file
from test_privacy_deletion import context as deletion_context
from test_privacy_deletion import request as deletion_request
from acceptance.guards import install
from acceptance.private_memory_journey import FIRST, MAX_OPERATIONS, run_robot_journey
from test_cognition_pipeline import NOW, append_event, principal
from test_self_state import interest_change, revision
from test_private_web_http import Clock


@pytest.fixture(autouse=True)
def offline_network_guard(monkeypatch):
    install(monkeypatch, local_ipc=True)


def _proposal(kind, fields, handle=None, role="supporting"):
    return TypedProposal(
        "revise_memory" if handle else "new_memory", handle, kind, fields, role
    )


def test_typed_parser_rejects_authority_and_bad_numbers():
    fields = {
        "description": "Learn Italian in 2027",
        "rationale": "Personal plan",
        "priority": 60,
        "status": "active",
        "constraints": [],
        "review_condition": "When the year changes",
    }
    valid = {
        "decision": "new_memory",
        "handle": None,
        "kind": "goal_or_intention",
        "fields": fields,
        "source_role": "supporting",
    }
    assert parse_typed_proposal(json.dumps(valid)) == _proposal(
        "goal_or_intention", fields
    )
    for invalid in (
        {**valid, "source_event_id": "forged"},
        {**valid, "fields": {**fields, "priority": True}},
        {**valid, "fields": {**fields, "priority": 10**400}},
        {**valid, "fields": {**fields, "status": "verified"}},
        {**valid, "fields": {**fields, "verification": "verified"}},
        {**valid, "verification": "verified"},
        {**valid, "source_role": "counterevidence"},
        {**valid, "handle": "hbad"},
    ):
        with pytest.raises(ValueError, match="invalid typed proposal"):
            parse_typed_proposal(json.dumps(invalid))
    with pytest.raises(ValueError, match="invalid typed proposal"):
        parse_typed_proposal('{"decision":"no_change","decision":"no_change"}')


def test_typed_request_and_field_bounds_are_complete():
    interest = TypedMemory(
        "h0123456789abcdef",
        "durable_interest",
        {
            "topic": "Italian art",
            "strength": 65,
            "status": "active",
            "reason_for_interest": "Personal interest",
        },
    )
    valid = TypedModelRequest("Current input", (interest,))
    assert json.loads(valid.serialize())["memories"][0]["verification"] == "unverified"
    assert json.loads(valid.serialize())["participant_input_can_instruct"] is False
    malformed_requests = (
        TypedModelRequest("", ()),
        TypedModelRequest("x" * 8193, ()),
        TypedModelRequest("Input", [interest]),
        TypedModelRequest("Input", (interest,) * 9),
        TypedModelRequest("Input", ("raw",)),
        TypedModelRequest(
            "Input", (TypedMemory("bad", interest.kind, interest.fields),)
        ),
        TypedModelRequest("Input", (interest, interest)),
        TypedModelRequest(
            "x" * 8192,
            (
                TypedMemory(
                    interest.handle,
                    interest.kind,
                    {
                        **interest.fields,
                        "topic": "y" * 4096,
                        "reason_for_interest": "z" * 4096,
                    },
                ),
            ),
        ),
    )
    for request in malformed_requests:
        with pytest.raises(ValueError, match="invalid typed request"):
            request.serialize()
    interest_data = {
        "decision": "new_memory",
        "handle": None,
        "kind": "durable_interest",
        "fields": interest.fields,
        "source_role": "supporting",
    }
    belief_data = {
        "decision": "new_memory",
        "handle": None,
        "kind": "belief_or_hypothesis",
        "fields": {
            "proposition": "A hypothesis",
            "stance": "uncertain",
            "confidence": 0.3,
            "status": "active",
            "alternatives": [],
        },
        "source_role": "supporting",
    }
    for invalid in (
        {**interest_data, "fields": {**interest.fields, "topic": ""}},
        {**interest_data, "fields": {**interest.fields, "strength": True}},
        {**belief_data, "fields": {**belief_data["fields"], "confidence": 1.1}},
        {**belief_data, "fields": {**belief_data["fields"], "alternatives": [""]}},
        {
            **belief_data,
            "fields": {**belief_data["fields"], "alternatives": ["x"] * 21},
        },
    ):
        with pytest.raises(ValueError, match="invalid typed proposal"):
            parse_typed_proposal(json.dumps(invalid))
    for raw in (None, "x" * 8193):
        with pytest.raises(ValueError, match="invalid typed proposal"):
            parse_typed_proposal(raw)


def test_typed_codec_gateway_usage_and_invalid_sdk_output(tmp_path):
    memory = TypedMemory(
        "h0123456789abcdef",
        "durable_interest",
        {
            "topic": "Italian art",
            "strength": 65,
            "status": "active",
            "reason_for_interest": "I enjoy paintings",
        },
    )
    request = TypedModelRequest("I enjoy Italian art", (memory,))
    wire = encode_request(InferenceRequest("typed:" + "a" * 64, "corr", request))
    assert decode_request(wire).model_request == request
    sdk = build_worker_typed_request(
        request, default_openai_provider_config()
    ).to_sdk_kwargs()
    assert sdk["text"]["format"]["name"] == "private_typed_memory_proposal"
    assert sdk["store"] is False and sdk["tools"] == []
    malformed = json.loads(wire)
    malformed["model_request"]["memories"][0]["item_id"] = "forged"
    with pytest.raises(WorkerProtocolError):
        decode_request(json.dumps(malformed).encode())
    for field, value in (
        ("participant_input_can_instruct", True),
        ("contract_version", "forged"),
    ):
        invalid_request = json.loads(wire)
        invalid_request["model_request"][field] = value
        with pytest.raises(WorkerProtocolError):
            decode_request(json.dumps(invalid_request).encode())
    invalid_memory = json.loads(wire)
    invalid_memory["model_request"]["memories"][0]["verification"] = "verified"
    with pytest.raises(WorkerProtocolError):
        decode_request(json.dumps(invalid_memory).encode())
    no_change = TypedProposal("no_change", None, None, None, None)
    response = WorkerResponse(
        "completed", 1, None, typed=no_change, typed_operation=True
    )
    assert decode_response(encode_response(response)) == response
    for field, value in (
        ("memory_proposal", {"decision": "forged"}),
        ("attempts", -1),
    ):
        invalid_response = json.loads(encode_response(response))
        invalid_response[field] = value
        with pytest.raises(WorkerProtocolError):
            decode_response(json.dumps(invalid_response).encode())
    with pytest.raises(ValueError, match="invalid typed response"):
        encode_response(WorkerResponse("completed", 1, None, typed=no_change))
    with pytest.raises(ValueError, match="invalid typed response"):
        encode_response(
            WorkerResponse(
                "completed",
                1,
                None,
                typed=no_change,
                typed_operation=True,
                evolution_operation=True,
            )
        )
    with pytest.raises(TypeError):
        build_worker_typed_request("wrong", default_openai_provider_config())
    invalid = json.dumps(
        {
            "decision": "new_memory",
            "handle": None,
            "kind": "goal_or_intention",
            "fields": {},
            "source_role": "supporting",
        }
    )
    transport = FakeOpenAITransport([provider_response(invalid)])
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
    with pytest.raises(TypeError, match="typed memory request"):
        gateway.submit_typed("wrong", correlation_id="corr", operation_id="typed:wrong")
    outcome = gateway.submit_typed(
        request, correlation_id="corr", operation_id="typed:invalid"
    )
    assert outcome.outcome is GatewayOutcome.INVALID_PROPOSAL and outcome.attempts == 1
    usage = state.usage_attempt("typed:invalid:1")
    assert usage.total_tokens == 120


def test_typed_worker_commit_guard_checks_permission_after_provider(tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    no_change = {
        "decision": "no_change",
        "handle": None,
        "kind": None,
        "fields": None,
        "source_role": None,
    }
    state = ProviderStateStore(EventStore(tmp_path / "provider.db"))
    gateway = TrustedOpenAIGateway(
        store=state,
        transport=FakeOpenAITransport([provider_response(json.dumps(no_change))]),
        config=replace(
            default_openai_provider_config(),
            deployment_stage=DeploymentStage.STAGE_1,
            paid_calls_enabled=True,
            max_attempts=1,
        ),
    )
    config = WorkerServerConfig(socket_path, frozenset({os.getuid()}))
    with pytest.raises(TypeError, match="invalid typed guard"):
        OpenAIWorkerServer(config, gateway, typed_enabled=1)
    with pytest.raises(TypeError, match="invalid typed guard"):
        OpenAIWorkerServer(config, gateway, typed_commit_guard="invalid")
    server = OpenAIWorkerServer(
        config, gateway, typed_enabled=True, typed_commit_guard=lambda: False
    )
    server.start()
    thread = Thread(target=server.serve_once)
    thread.start()
    try:
        client = OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid())
        result = client.infer(
            TypedModelRequest("Synthetic input", ()),
            correlation_id="corr",
            operation_id="typed:" + "a" * 64,
        )
        assert result.outcome == "authorization_rejected" and result.attempts == 1
        assert result.typed is None
        with state._event_store._open() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM provider_usage_attempts"
            ).fetchone() == (1,)
    finally:
        thread.join(5)
        server.close()


def test_typed_worker_permission_is_separate_and_stop_is_current(tmp_path):
    activation_path, _ = activation_file(tmp_path)
    activation = load_activation(activation_path)
    allowance = PilotAllowance.initialize(tmp_path / "allowance.db", activation)
    stop_path = activation_path.parent / "stop.json"
    stop_path.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": False})
    )
    stop_path.chmod(0o400)
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
        "typed:" + "a" * 64, "corr", TypedModelRequest("Synthetic input", ())
    )
    gate = PilotRequestGate(
        activation,
        allowance,
        activation_path=activation_path,
        stop=PilotStop(stop_path),
        evolution_permission_path=evolution_path,
    )
    assert gate.authorize(request).outcome == "authorization_rejected"
    typed_path = activation_path.parent / "typed.json"
    typed_path.write_text(
        json.dumps(
            {
                "version": "private-typed-memory-permission-v1",
                "pilot_id": activation.pilot_id,
                "max_operations": 1,
            }
        )
    )
    typed_path.chmod(0o400)
    gate.typed_permission_path = typed_path
    assert gate.authorize(request) is None
    assert gate.typed_commit_allowed()
    assert (
        gate.authorize(
            InferenceRequest("typed:" + "b" * 64, "corr", request.model_request)
        ).outcome
        == "authorization_rejected"
    )
    stop_path.chmod(0o600)
    stop_path.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": True})
    )
    stop_path.chmod(0o400)
    assert gate.authorize(request).outcome == "emergency_stop"
    assert not gate.typed_commit_allowed()


def test_typed_creation_revision_and_contextual_erasure(tmp_path):
    events = EventStore(tmp_path / "events.db")
    audit = TypedAuditStore(ProviderStateStore(EventStore(tmp_path / "ipc.db")), events)
    who = principal("a")
    engine = CognitionEngine(
        events,
        SelfStateStore(events),
        capabilities=CognitionCapabilities(generic_memory_write=False),
    )
    first = append_event(events, who, "italian-2027", "I plan to learn Italian in 2027")
    initial = audit.reserve(
        "typed:first", who, first, "I plan to learn Italian in 2027"
    )
    fields = {
        "description": "Learn Italian in 2027",
        "rationale": "Personal plan",
        "priority": 60,
        "status": "active",
        "constraints": [],
        "review_condition": "Review next year",
    }
    created = engine.commit_worker_typed(
        who,
        first,
        "typed:first",
        initial.snapshot,
        _proposal("goal_or_intention", fields),
    )
    assert created.status is CognitionOutcomeStatus.PERSISTED
    second = append_event(events, who, "italian-2028", "I moved that plan to 2028")
    selected = audit.reserve("typed:second", who, second, "I moved that plan to 2028")
    assert len(selected.request.memories) == 1
    assert "event-italian-2027" in selected.snapshot.sources
    handle = selected.snapshot.selected[0][0]
    revised = engine.commit_worker_typed(
        who,
        second,
        "typed:second",
        selected.snapshot,
        _proposal(
            "goal_or_intention",
            {**fields, "description": "Learn Italian in 2028"},
            handle,
        ),
    )
    assert revised.status is CognitionOutcomeStatus.PERSISTED
    state = SelfStateStore(EventStore(tmp_path / "events.db"))
    goal = state.retrieve_private_candidates(who)[0]
    assert goal.description == "Learn Italian in 2028" and goal.revision == 2
    evidence = state.inspect_private_revision_evidence(
        who, StateType.GOAL, goal.item_id, 2
    )
    assert {(item.event_id, item.role.value) for item in evidence} == {
        (first, "contextual"),
        (second, "initiating"),
        (second, "supporting"),
    }
    assert all(item.verification is VerificationStatus.UNVERIFIED for item in evidence)


@pytest.mark.parametrize(
    ("kind", "fields", "state_type"),
    [
        (
            "durable_interest",
            {
                "topic": "Italian art",
                "strength": 65,
                "status": "active",
                "reason_for_interest": "I enjoy the paintings",
            },
            StateType.INTEREST,
        ),
        (
            "belief_or_hypothesis",
            {
                "proposition": "Italian study helps memory",
                "stance": "uncertain",
                "confidence": 0.3,
                "status": "active",
                "alternatives": ["Practice may help"],
            },
            StateType.BELIEF,
        ),
    ],
)
def test_interest_and_hypothesis_are_unverified_and_principal_scoped(
    tmp_path, kind, fields, state_type
):
    events = EventStore(tmp_path / "events.db")
    audit = TypedAuditStore(ProviderStateStore(EventStore(tmp_path / "ipc.db")), events)
    a, b = principal("a"), principal("b")
    source = append_event(events, a, kind, "Synthetic participant statement")
    selected = audit.reserve(
        "typed:" + kind, a, source, "Synthetic participant statement"
    )
    engine = CognitionEngine(events, SelfStateStore(events))
    created = engine.commit_worker_typed(
        a, source, "typed:" + kind, selected.snapshot, _proposal(kind, fields)
    )
    assert created.status is CognitionOutcomeStatus.PERSISTED
    state = SelfStateStore(events)
    assert state.retrieve_private_candidates(b) == ()
    item = state.retrieve_private_candidates(a)[0]
    assert item.scope.value == "private_candidate"
    assert (
        state.inspect_private_revision_evidence(a, state_type, item.item_id, 1)[
            0
        ].verification
        is VerificationStatus.UNVERIFIED
    )
    if state_type is StateType.BELIEF:
        assert item.verification is VerificationStatus.UNVERIFIED
    forged = TypedProposal(
        "revise_memory", "h0000000000000000", kind, fields, "supporting"
    )
    assert (
        engine.commit_worker_typed(
            a, source, "typed:forged", selected.snapshot, forged
        ).status
        is not CognitionOutcomeStatus.PERSISTED
    )
    assert (
        engine.commit_worker_typed(
            b, source, "typed:cross", selected.snapshot, _proposal(kind, fields)
        ).status
        is not CognitionOutcomeStatus.PERSISTED
    )


def test_typed_engine_rejects_disabled_noncanonical_and_mismatched_operations(tmp_path):
    events = EventStore(tmp_path / "events.db")
    audit = TypedAuditStore(ProviderStateStore(EventStore(tmp_path / "ipc.db")), events)
    who = principal("a")
    source = append_event(events, who, "first", "I plan to visit Rome")
    reserved = audit.reserve("typed:first", who, source, "I plan to visit Rome")
    fields = {
        "description": "Visit Rome",
        "rationale": "Personal plan",
        "priority": 60,
        "status": "active",
        "constraints": [],
        "review_condition": "Review next month",
    }
    disabled = CognitionEngine(
        events,
        SelfStateStore(events),
        capabilities=CognitionCapabilities(private_state_write=False),
    )
    assert (
        disabled.commit_worker_typed(
            who,
            source,
            "typed:first",
            reserved.snapshot,
            _proposal("goal_or_intention", fields),
        ).reason
        is CognitionReason.CAPABILITY_DISABLED
    )
    engine = CognitionEngine(events, SelfStateStore(events))
    invalid = (
        None,
        _proposal("goal_or_intention", {**fields, "priority": object()}),
        _proposal("goal_or_intention", {**fields, "constraints": ()}),
    )
    for proposal in invalid:
        assert (
            engine.commit_worker_typed(
                who, source, "typed:first", reserved.snapshot, proposal
            ).status
            is not CognitionOutcomeStatus.PERSISTED
        )
    assert (
        engine.commit_worker_typed(
            who,
            source,
            "typed:first",
            reserved.snapshot,
            _proposal("goal_or_intention", fields),
        ).status
        is CognitionOutcomeStatus.PERSISTED
    )
    later = append_event(events, who, "later", "I changed the plan")
    shown = audit.reserve("typed:later", who, later, "I changed the plan")
    assert (
        engine.commit_worker_typed(
            who,
            later,
            "typed:later",
            shown.snapshot,
            _proposal(
                "durable_interest",
                {
                    "topic": "Rome",
                    "strength": 70,
                    "status": "active",
                    "reason_for_interest": "Personal travel",
                },
                shown.snapshot.selected[0][0],
            ),
        ).status
        is not CognitionOutcomeStatus.PERSISTED
    )


def test_contextual_dependency_cannot_relabel_supporting_evidence(tmp_path):
    events = EventStore(tmp_path / "events.db")
    state = SelfStateStore(events)
    who = principal("a")
    source = append_event(events, who, "source", "I enjoy art")
    change = interest_change(source)
    change = replace(
        change,
        revision=replace(change.revision, contextual_event_ids=(source,)),
    )
    with pytest.raises(
        SelfStateValidationError, match="contextual dependency roles conflict"
    ):
        state.create_interest_candidate(who, change)


def test_concurrent_typed_heads_do_not_retarget_and_counterevidence_stays_distinct(
    tmp_path,
):
    events = EventStore(tmp_path / "events.db")
    audit = TypedAuditStore(ProviderStateStore(EventStore(tmp_path / "ipc.db")), events)
    who = principal("a")
    engine = CognitionEngine(events, SelfStateStore(events))
    first = append_event(events, who, "belief-first", "I suspect a pattern")
    initial = audit.reserve("typed:belief-first", who, first, "I suspect a pattern")
    fields = {
        "proposition": "A pattern exists",
        "stance": "uncertain",
        "confidence": 0.3,
        "status": "active",
        "alternatives": ["Coincidence"],
    }
    assert (
        engine.commit_worker_typed(
            who,
            first,
            "typed:belief-first",
            initial.snapshot,
            _proposal("belief_or_hypothesis", fields),
        ).status
        is CognitionOutcomeStatus.PERSISTED
    )
    audit.finish("typed:belief-first", "accepted")
    second = append_event(events, who, "belief-second", "I now doubt that pattern")
    third = append_event(events, who, "belief-third", "I strongly doubt that pattern")
    one = audit.reserve("typed:belief-second", who, second, "I now doubt that pattern")
    two = audit.reserve(
        "typed:belief-third", who, third, "I strongly doubt that pattern"
    )
    handle = one.snapshot.selected[0][0]
    revised_fields = {**fields, "stance": "opposes", "confidence": 0.7}
    assert (
        engine.commit_worker_typed(
            who,
            second,
            "typed:belief-second",
            one.snapshot,
            _proposal(
                "belief_or_hypothesis", revised_fields, handle, "counterevidence"
            ),
        ).status
        is CognitionOutcomeStatus.PERSISTED
    )
    assert (
        engine.commit_worker_typed(
            who,
            third,
            "typed:belief-third",
            two.snapshot,
            _proposal(
                "belief_or_hypothesis",
                revised_fields,
                two.snapshot.selected[0][0],
                "counterevidence",
            ),
        ).status
        is not CognitionOutcomeStatus.PERSISTED
    )
    belief = SelfStateStore(events).retrieve_private_candidates(who)[0]
    assert belief.revision == 2 and belief.conflicting_evidence_count == 1
    evidence = SelfStateStore(events).inspect_private_revision_evidence(
        who, StateType.BELIEF, belief.item_id, 2
    )
    assert (second, "counter") in {(row.event_id, row.role.value) for row in evidence}
    assert (first, "contextual") in {(row.event_id, row.role.value) for row in evidence}


def test_typed_goal_terminal_status_cannot_be_reactivated(tmp_path):
    events = EventStore(tmp_path / "events.db")
    audit = TypedAuditStore(ProviderStateStore(EventStore(tmp_path / "ipc.db")), events)
    who = principal("a")
    engine = CognitionEngine(events, SelfStateStore(events))
    fields = {
        "description": "Learn Italian",
        "rationale": "Personal plan",
        "priority": 70,
        "status": "active",
        "constraints": [],
        "review_condition": "Review next year",
    }
    first = append_event(events, who, "goal-start", "I plan to learn Italian")
    initial = audit.reserve("typed:goal-start", who, first, "I plan to learn Italian")
    assert (
        engine.commit_worker_typed(
            who,
            first,
            "typed:goal-start",
            initial.snapshot,
            _proposal("goal_or_intention", fields),
        ).status
        is CognitionOutcomeStatus.PERSISTED
    )
    audit.finish("typed:goal-start", "accepted")
    second = append_event(events, who, "goal-finish", "I completed my plan")
    complete = audit.reserve("typed:goal-finish", who, second, "I completed my plan")
    assert (
        engine.commit_worker_typed(
            who,
            second,
            "typed:goal-finish",
            complete.snapshot,
            _proposal(
                "goal_or_intention",
                {**fields, "status": "completed"},
                complete.snapshot.selected[0][0],
            ),
        ).status
        is CognitionOutcomeStatus.PERSISTED
    )
    audit.finish("typed:goal-finish", "accepted")
    third = append_event(events, who, "goal-restart", "I changed my mind")
    restart = audit.reserve("typed:goal-restart", who, third, "I changed my mind")
    assert (
        engine.commit_worker_typed(
            who,
            third,
            "typed:goal-restart",
            restart.snapshot,
            _proposal("goal_or_intention", fields, restart.snapshot.selected[0][0]),
        ).status
        is not CognitionOutcomeStatus.PERSISTED
    )


def test_deleting_exposed_typed_context_erases_receipt_and_state(tmp_path):
    events = EventStore(tmp_path / "events.db")
    audit = TypedAuditStore(ProviderStateStore(EventStore(tmp_path / "ipc.db")), events)
    who = principal("a")
    first = append_event(events, who, "base", "I study Italian")
    initial = audit.reserve("typed:base", who, first, "I study Italian")
    fields = {
        "topic": "Italian language",
        "strength": 60,
        "status": "active",
        "reason_for_interest": "Personal study",
    }
    engine = CognitionEngine(events, SelfStateStore(events))
    assert (
        engine.commit_worker_typed(
            who,
            first,
            "typed:base",
            initial.snapshot,
            _proposal("durable_interest", fields),
        ).status
        is CognitionOutcomeStatus.PERSISTED
    )
    audit.finish("typed:base", "accepted")
    second = append_event(events, who, "later", "I also enjoy history")
    exposed = audit.reserve("typed:later", who, second, "I also enjoy history")
    assert first in exposed.snapshot.sources
    with events._open() as db:
        _validate_graph(db)
    report = EventStoreErasureAdapter(events).erase(
        deletion_request(content_reference=first)
    )
    assert report.payload_removed and not report.blockers
    assert SelfStateStore(events).retrieve_private_candidates(who) == ()
    assert (
        audit.read(
            "typed:later", who.principal_id.value, second, "I also enjoy history"
        ).status
        == "unavailable"
    )
    with events._open() as db:
        _validate_graph(db)
        assert db.execute(
            "SELECT COUNT(*) FROM private_extraction_receipts"
        ).fetchone() == (0,)


def test_deleting_unselected_but_exposed_head_blocks_selected_revision(tmp_path):
    events = EventStore(tmp_path / "events.db")
    audit = TypedAuditStore(ProviderStateStore(EventStore(tmp_path / "ipc.db")), events)
    who = principal("a")
    engine = CognitionEngine(events, SelfStateStore(events))
    for label in ("art", "music"):
        source = append_event(events, who, label, "I enjoy " + label)
        reserved = audit.reserve(
            "typed:create-" + label, who, source, "I enjoy " + label
        )
        assert (
            engine.commit_worker_typed(
                who,
                source,
                "typed:create-" + label,
                reserved.snapshot,
                _proposal(
                    "durable_interest",
                    {
                        "topic": label,
                        "strength": 60,
                        "status": "active",
                        "reason_for_interest": "Personal enjoyment",
                    },
                ),
            ).status
            is CognitionOutcomeStatus.PERSISTED
        )
        audit.finish("typed:create-" + label, "accepted")
    update = append_event(events, who, "update", "I enjoy art more")
    exposed = audit.reserve("typed:revise-art", who, update, "I enjoy art more")
    assert len(exposed.snapshot.selected) == 2
    selected = next(
        memory for memory in exposed.request.memories if memory.fields["topic"] == "art"
    )
    assert "event-music" in exposed.snapshot.sources
    report = EventStoreErasureAdapter(events).erase(
        deletion_request(content_reference="event-music")
    )
    assert report.payload_removed and not report.blockers
    assert (
        audit.read(
            "typed:revise-art", who.principal_id.value, update, "I enjoy art more"
        ).status
        == "unavailable"
    )
    outcome = engine.commit_worker_typed(
        who,
        update,
        "typed:revise-art",
        exposed.snapshot,
        _proposal(
            "durable_interest",
            {
                "topic": "art",
                "strength": 80,
                "status": "active",
                "reason_for_interest": "Personal enjoyment",
            },
            selected.handle,
        ),
    )
    assert outcome.status is not CognitionOutcomeStatus.PERSISTED
    assert SelfStateStore(events).retrieve_private_candidates(who)[0].strength == 60


def test_typed_selection_counts_repeated_historical_role_rows(tmp_path):
    events = EventStore(tmp_path / "events.db")
    state = SelfStateStore(events)
    who = principal("a")
    first = append_event(events, who, "initial", "I enjoy Italian art")
    state.create_interest_candidate(who, interest_change(first))
    for number in range(1, 25):
        initiating = append_event(
            events, who, f"revision-{number}", "I still enjoy art"
        )
        change = interest_change(
            first,
            operation=f"revision-{number}",
            expected=number,
            strength=60 + number % 2,
        )
        state.revise_interest_candidate(
            who,
            replace(
                change,
                revision=revision(f"revision-{number}", initiating, expected=number),
            ),
        )
    with events._open() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM protected_state_provenance"
        ).fetchone() == (50,)
    next_source = append_event(events, who, "next", "I changed my art preference")
    audit = TypedAuditStore(ProviderStateStore(EventStore(tmp_path / "ipc.db")), events)
    reserved = audit.reserve(
        "typed:too-many-role-rows", who, next_source, "I changed my art preference"
    )
    assert reserved.request.memories == ()
    assert reserved.snapshot.selected == ()
    with events._open() as db:
        _validate_graph(db)
        sources = sorted(
            {
                row[0]
                for row in db.execute("SELECT event_id FROM protected_state_provenance")
            }
        )
        item = state.retrieve_private_candidates(who)[0]
        raw = db.execute(
            "SELECT proposal FROM private_extraction_receipts WHERE operation_id='typed:too-many-role-rows'"
        ).fetchone()[0]
        value = json.loads(raw)
        value["snapshot"]["selected"] = [
            ["h0123456789abcdef", item.item_id, item.revision, "durable_interest"]
        ]
        value["snapshot"]["sources"] = sources
        db.execute(
            "UPDATE private_extraction_receipts SET proposal=? WHERE operation_id='typed:too-many-role-rows'",
            (json.dumps(value),),
        )
        with pytest.raises(ApplicationPrivacyError, match="invalid_typed_receipt"):
            _validate_graph(db)
    with pytest.raises(ValueError, match="invalid typed receipt"):
        audit.read(
            "typed:too-many-role-rows",
            who.principal_id.value,
            next_source,
            "I changed my art preference",
        )


def test_tampered_typed_receipt_fails_privacy_graph(tmp_path):
    events = EventStore(tmp_path / "events.db")
    audit = TypedAuditStore(ProviderStateStore(EventStore(tmp_path / "ipc.db")), events)
    who = principal("a")
    source = append_event(events, who, "source", "A synthetic statement")
    audit.reserve("typed:tamper", who, source, "A synthetic statement")
    with events._open() as db:
        db.execute(
            "UPDATE private_extraction_receipts SET proposal=? WHERE operation_id=?",
            (
                '{"version":"private-typed-receipt-v1","snapshot":{"selected":[],"visible":[],"sources":["forged"]},"proposal":null}',
                "typed:tamper",
            ),
        )
        with pytest.raises(ApplicationPrivacyError, match="invalid_typed_receipt"):
            _validate_graph(db)
        db.execute(
            "UPDATE private_extraction_receipts SET proposal=? WHERE operation_id=?",
            (" " * 131_073, "typed:tamper"),
        )
        with pytest.raises(ApplicationPrivacyError, match="invalid_typed_receipt"):
            _validate_graph(db)


def test_old_backup_quarantine_does_not_restore_typed_receipt(tmp_path):
    events = EventStore(tmp_path / "events.db")
    audit = TypedAuditStore(ProviderStateStore(EventStore(tmp_path / "ipc.db")), events)
    who = principal("a")
    first = append_event(events, who, "old", "I like Italian literature")
    fields = {
        "topic": "Italian literature",
        "strength": 70,
        "status": "active",
        "reason_for_interest": "Reading interest",
    }
    initial = audit.reserve("typed:old", who, first, "I like Italian literature")
    engine = CognitionEngine(events, SelfStateStore(events))
    assert (
        engine.commit_worker_typed(
            who,
            first,
            "typed:old",
            initial.snapshot,
            _proposal("durable_interest", fields),
        ).status
        is CognitionOutcomeStatus.PERSISTED
    )
    audit.finish("typed:old", "accepted")
    second = append_event(events, who, "pending", "I now read more")
    audit.reserve("typed:pending", who, second, "I now read more")
    backup = tmp_path / "old-backup.db"
    create_sqlite_backup(events.database_path, backup, 13, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.db")
    deletion = DeletionCoordinator(ledger, EventStoreErasureAdapter(events)).delete(
        deletion_context(), deletion_request(content_reference=first)
    )
    assert deletion.disposition.value == "applied"
    quarantine = tmp_path / "quarantine.db"
    restored = QuarantinedRestore(ledger, 13, EventStoreRestoreAdapter).restore(
        backup, quarantine, NOW
    )
    assert restored.activation_eligible
    with sqlite3.connect(f"file:{quarantine}?immutable=1", uri=True) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM private_extraction_receipts"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM state_revision_payloads"
        ).fetchone() == (0,)


def test_v9_typed_provenance_migration_is_atomic(tmp_path):
    path = tmp_path / "historical.db"
    events = EventStore(path)
    who = principal("a")
    source = append_event(events, who, "migration", "Synthetic historical interest")
    audit = TypedAuditStore(ProviderStateStore(EventStore(tmp_path / "ipc.db")), events)
    selected = audit.reserve(
        "typed:migration", who, source, "Synthetic historical interest"
    )
    fields = {
        "topic": "Historical art",
        "strength": 50,
        "status": "active",
        "reason_for_interest": "Historical example",
    }
    assert (
        CognitionEngine(events, SelfStateStore(events))
        .commit_worker_typed(
            who,
            source,
            "typed:migration",
            selected.snapshot,
            _proposal("durable_interest", fields),
        )
        .status
        is CognitionOutcomeStatus.PERSISTED
    )
    audit.finish("typed:migration", "accepted")
    with sqlite3.connect(path) as db:
        remove_v10(db)
        db.execute("UPDATE schema_metadata SET version=9")
        db.execute("PRAGMA user_version=9")
        before = schema_objects(db)

    def crash(point):
        if point is FaultPoint.BEFORE_V10_MIGRATION_COMMIT:
            raise RuntimeError("synthetic v10 migration crash")

    with pytest.raises(RuntimeError, match="synthetic v10 migration crash"):
        EventStore(path, fault_injector=crash)
    with sqlite3.connect(path) as db:
        assert schema_objects(db) == before
        assert db.execute("PRAGMA user_version").fetchone() == (9,)
    upgraded = EventStore(path)
    assert upgraded.diagnostics().schema_version == 13
    assert (
        SelfStateStore(upgraded).retrieve_private_candidates(who)[0].topic
        == "Historical art"
    )


def _typed_worker(socket_path, provider_path, ready, count):
    class SDK:
        def __init__(self):
            self.requests = []

        def create(self, **kwargs):
            self.requests.append(kwargs)
            projection = json.loads(kwargs["input"])
            if projection.get("task") == "evolve_private_typed_memory":
                user = projection["participant_input"]
                if "Italian sculpture" in user:
                    proposal = {
                        "decision": "revise_memory",
                        "handle": next(
                            item["handle"]
                            for item in projection["memories"]
                            if item["kind"] == "durable_interest"
                        ),
                        "kind": "durable_interest",
                        "source_role": "supporting",
                        "fields": {
                            "topic": "Italian sculpture",
                            "strength": 75,
                            "status": "active",
                            "reason_for_interest": "I enjoy the artwork",
                        },
                    }
                elif "Italian art" in user:
                    proposal = {
                        "decision": "new_memory",
                        "handle": None,
                        "kind": "durable_interest",
                        "source_role": "supporting",
                        "fields": {
                            "topic": "Italian art",
                            "strength": 60,
                            "status": "active",
                            "reason_for_interest": "I enjoy the artwork",
                        },
                    }
                elif "now doubt music" in user:
                    proposal = {
                        "decision": "revise_memory",
                        "handle": next(
                            item["handle"]
                            for item in projection["memories"]
                            if item["kind"] == "belief_or_hypothesis"
                        ),
                        "kind": "belief_or_hypothesis",
                        "source_role": "counterevidence",
                        "fields": {
                            "proposition": "Music improves memory",
                            "stance": "opposes",
                            "confidence": 0.7,
                            "status": "active",
                            "alternatives": ["The earlier pattern may be coincidence"],
                        },
                    }
                elif "suspect music" in user:
                    proposal = {
                        "decision": "new_memory",
                        "handle": None,
                        "kind": "belief_or_hypothesis",
                        "source_role": "supporting",
                        "fields": {
                            "proposition": "Music improves memory",
                            "stance": "uncertain",
                            "confidence": 0.3,
                            "status": "active",
                            "alternatives": ["The pattern may be coincidence"],
                        },
                    }
                elif "2027" in user:
                    proposal = {
                        "decision": "new_memory",
                        "handle": None,
                        "kind": "goal_or_intention",
                        "source_role": "supporting",
                        "fields": {
                            "description": "Learn Italian in 2027",
                            "rationale": "Personal plan",
                            "priority": 60,
                            "status": "active",
                            "constraints": [],
                            "review_condition": "Review next year",
                        },
                    }
                elif "2028" in user:
                    proposal = {
                        "decision": "revise_memory",
                        "handle": projection["memories"][0]["handle"],
                        "kind": "goal_or_intention",
                        "source_role": "supporting",
                        "fields": {
                            "description": "Learn Italian in 2028",
                            "rationale": "Personal plan",
                            "priority": 60,
                            "status": "active",
                            "constraints": [],
                            "review_condition": "Review next year",
                        },
                    }
                else:
                    proposal = {
                        "decision": "no_change",
                        "handle": None,
                        "kind": None,
                        "fields": None,
                        "source_role": None,
                    }
            else:
                proposal = {
                    "proposal": {
                        "decision": "reply",
                        "content": "Да, я помню этот план.",
                    },
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
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            }

    sdk = SDK()
    gateway = TrustedOpenAIGateway(
        store=ProviderStateStore(EventStore(Path(provider_path))),
        transport=OpenAIResponsesTransport(SimpleNamespace(responses=sdk)),
        config=replace(
            default_openai_provider_config(),
            deployment_stage=DeploymentStage.STAGE_1,
            paid_calls_enabled=True,
            max_attempts=1,
        ),
    )
    server = OpenAIWorkerServer(
        WorkerServerConfig(Path(socket_path), frozenset({os.getuid()})),
        gateway,
        typed_enabled=True,
    )
    try:
        server.start()
        ready.send(True)
        server.serve(max_connections=count)
        ready.send(sdk.requests)
    finally:
        server.close()
        ready.close()


def test_connected_http_typed_goal_revision_restarts_with_current_context(tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    auth, events, ledger, gate, app, a, b = _app(tmp_path, socket_path)
    app._conversations = WorkerConversationRuntime(
        events,
        gate,
        OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
        provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
        typed_memory_enabled=True,
        context_limits=ContextLimits(32000, 1),
    )
    parent, child = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_typed_worker,
        args=(str(socket_path), str(tmp_path / "provider.db"), child, 8),
    )
    process.start()
    child.close()
    try:
        assert parent.poll(5) and parent.recv() is True
        for number, content in enumerate(
            (
                "I plan to learn Italian in 2027",
                "I moved that plan to 2028",
                "What is my Italian plan?",
            )
        ):
            if number == 2:
                reopened_events = EventStore(events.database_path)
                reopened_gate = PrivacyReadGate(ledger, reopened_events)
                assert reopened_gate.reconcile()
                app._conversations = WorkerConversationRuntime(
                    reopened_events,
                    reopened_gate,
                    OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
                    provider_state=ProviderStateStore(
                        EventStore(tmp_path / "ipc-state.db")
                    ),
                    typed_memory_enabled=True,
                    context_limits=ContextLimits(32000, 1),
                    clock=Clock(),
                )
                app._privacy = ParticipantPrivacyControls(
                    auth, reopened_events, ledger, reopened_gate, clock=Clock()
                )
            assert (
                json.loads(a.submit(str(number), content)[2])["model_state"]
                == "completed"
            )
        assert (
            json.loads(b.submit("other", "I study French")[2])["model_state"]
            == "completed"
        )
        assert parent.poll(5)
        requests = parent.recv()
        typed = [
            json.loads(call["input"])
            for call in requests
            if json.loads(call["input"]).get("task") == "evolve_private_typed_memory"
        ]
        assert len(typed) == 4
        assert typed[0]["memories"] == []
        assert (
            typed[1]["memories"][0]["fields"]["description"] == "Learn Italian in 2027"
        )
        assert (
            typed[2]["memories"][0]["fields"]["description"] == "Learn Italian in 2028"
        )
        assert typed[3]["memories"] == []
        reply_inputs = [
            call["input"]
            for call in requests
            if json.loads(call["input"]).get("task") != "evolve_private_typed_memory"
        ]
        assert "Learn Italian in 2028" in reply_inputs[-2]
        assert "Learn Italian in 2028" not in reply_inputs[-1]
        assert all(
            "principal_id" not in call["input"] and call["tools"] == []
            for call in requests
        )
        assert (
            SelfStateStore(EventStore(events.database_path))
            .retrieve_private_candidates(principal("a"))[0]
            .description
            == "Learn Italian in 2028"
        )
        with EventStore(tmp_path / "provider.db")._open() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM provider_usage_attempts"
            ).fetchone() == (8,)
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)
        parent.close()


def test_connected_worker_creates_and_revises_interest_and_hypothesis(tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    _auth, events, _ledger, gate, app, a, b = _app(tmp_path, socket_path)
    app._conversations = WorkerConversationRuntime(
        events,
        gate,
        OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
        provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
        typed_memory_enabled=True,
    )
    parent, child = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_typed_worker,
        args=(str(socket_path), str(tmp_path / "provider.db"), child, 8),
    )
    process.start()
    child.close()
    try:
        assert parent.poll(5) and parent.recv() is True
        for number, content in enumerate(
            (
                "I enjoy Italian art",
                "I enjoy Italian sculpture more",
                "I suspect music improves memory",
                "I now doubt music improves memory",
            )
        ):
            assert (
                json.loads(a.submit(str(number), content)[2])["model_state"]
                == "completed"
            )
        assert parent.poll(5)
        requests = parent.recv()
        typed = [
            json.loads(call["input"])
            for call in requests
            if json.loads(call["input"]).get("task") == "evolve_private_typed_memory"
        ]
        assert len(typed) == 4
        assert typed[1]["memories"][0]["fields"]["topic"] == "Italian art"
        assert any(
            item["fields"]["topic"] == "Italian sculpture"
            for item in typed[2]["memories"]
            if item["kind"] == "durable_interest"
        )
        assert any(
            item["fields"]["proposition"] == "Music improves memory"
            for item in typed[3]["memories"]
            if item["kind"] == "belief_or_hypothesis"
        )
        assert all(
            memory["verification"] == "unverified" and memory["can_instruct"] is False
            for request in typed
            for memory in request["memories"]
        )
        state = SelfStateStore(EventStore(events.database_path))
        items = state.retrieve_private_candidates(principal("a"))
        assert {item.scope.value for item in items} == {"private_candidate"}
        assert {item.revision for item in items} == {2}
        interest = next(item for item in items if isinstance(item, InterestCandidate))
        belief = next(item for item in items if isinstance(item, BeliefCandidate))
        assert interest.topic == "Italian sculpture"
        assert belief.verification is VerificationStatus.UNVERIFIED
        assert belief.conflicting_evidence_count == 1
        assert state.retrieve_private_candidates(principal("b")) == ()
        with EventStore(tmp_path / "provider.db")._open() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM provider_usage_attempts"
            ).fetchone() == (8,)
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)
        parent.close()


def test_source_deletion_during_typed_provider_attempt_blocks_commit(tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    _auth, events, ledger, gate, app, a, _b = _app(tmp_path, socket_path)
    app._conversations = WorkerConversationRuntime(
        events,
        gate,
        OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
        provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
        typed_memory_enabled=True,
    )
    started, release = Event(), Event()

    class BlockingSDK:
        def create(self, **kwargs):
            assert json.loads(kwargs["input"])["task"] == "evolve_private_typed_memory"
            started.set()
            assert release.wait(5)
            proposal = {
                "decision": "new_memory",
                "handle": None,
                "kind": "durable_interest",
                "source_role": "supporting",
                "fields": {
                    "topic": "Italian art",
                    "strength": 60,
                    "status": "active",
                    "reason_for_interest": "Reading",
                },
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
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            }

    provider = ProviderStateStore(EventStore(tmp_path / "provider.db"))
    server = OpenAIWorkerServer(
        WorkerServerConfig(socket_path, frozenset({os.getuid()})),
        TrustedOpenAIGateway(
            store=provider,
            transport=OpenAIResponsesTransport(
                SimpleNamespace(responses=BlockingSDK())
            ),
            config=replace(
                default_openai_provider_config(),
                deployment_stage=DeploymentStage.STAGE_1,
                paid_calls_enabled=True,
                max_attempts=1,
            ),
        ),
        typed_enabled=True,
    )
    server.start()
    worker_thread = Thread(target=server.serve_once)
    result = []
    submit_thread = Thread(
        target=lambda: result.append(a.submit("blocked", "I enjoy Italian art"))
    )
    worker_thread.start()
    submit_thread.start()
    try:
        assert started.wait(5)
        with events._open() as db:
            source = db.execute(
                "SELECT event_id FROM event_envelopes WHERE source_message_id='blocked'"
            ).fetchone()[0]
        deletion = DeletionCoordinator(ledger, EventStoreErasureAdapter(events)).delete(
            deletion_context(), deletion_request(content_reference=source)
        )
        assert deletion.disposition.value == "applied"
        release.set()
        submit_thread.join(5)
        worker_thread.join(5)
        assert not submit_thread.is_alive() and not worker_thread.is_alive()
        assert SelfStateStore(events).retrieve_private_candidates(principal("a")) == ()
        with events._open() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM private_extraction_receipts"
            ).fetchone() == (0,)
        with provider._event_store._open() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM provider_usage_attempts"
            ).fetchone() == (1,)
    finally:
        release.set()
        submit_thread.join(5)
        worker_thread.join(5)
        server.close()


@pytest.mark.parametrize(
    "crash_point", ["after_extraction_receipt", "after_memory_store_commit"]
)
@pytest.mark.parametrize("combined", [False, True])
def test_typed_received_reconciles_without_repeating_provider_or_revision(
    tmp_path, crash_point, combined
):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, events)
    assert gate.reconcile()
    who = principal("a")
    source = append_event(events, who, "plan", "I plan to visit Rome")
    calls = []
    fields = {
        "description": "Visit Rome",
        "rationale": "Personal plan",
        "priority": 60,
        "status": "active",
        "constraints": [],
        "review_condition": "Review next month",
    }

    def checkpoint(point):
        if point == crash_point:
            raise RuntimeError("synthetic crash")

    client = OpenAIWorkerClient(
        tmp_path / "absent.sock", expected_worker_uid=os.getuid()
    )

    def infer(request, *, correlation_id, operation_id):
        calls.append(operation_id)
        return SimpleNamespace(
            outcome="completed",
            typed=_proposal("goal_or_intention", fields),
        )

    client.infer = infer
    runtime = WorkerConversationRuntime(
        events,
        gate,
        client,
        provider_state=provider,
        typed_memory_enabled=True,
        memory_evolution_enabled=combined,
        checkpoint=checkpoint,
    )
    reply = "reply:" + "a" * 64
    with pytest.raises(RuntimeError, match="synthetic crash"):
        runtime._evolve_worker_typed(who, source, "I plan to visit Rome", "corr", reply)
    operation = "typed:" + "a" * 64
    assert (
        runtime._typed_audit.read(
            operation, who.principal_id.value, source, "I plan to visit Rome"
        ).status
        == "received"
    )
    resumed = WorkerConversationRuntime(
        events,
        gate,
        client,
        provider_state=provider,
        typed_memory_enabled=True,
        memory_evolution_enabled=combined,
    )
    if crash_point == "after_extraction_receipt":
        resumed._evolve_worker_typed(who, source, "I plan to visit Rome", "corr", reply)
    else:
        resumed._reconcile_worker_typed(who, source, "I plan to visit Rome", reply)
    assert (
        resumed._typed_audit.read(
            operation, who.principal_id.value, source, "I plan to visit Rome"
        ).status
        == "accepted"
    )
    assert calls == [operation]
    assert len(SelfStateStore(events).retrieve_private_candidates(who)) == 1


@pytest.mark.parametrize("blocked", ["stop", "spend", "session"])
def test_typed_admission_and_commit_recheck_revocation(tmp_path, blocked):
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
        events, gate, client, provider_state=provider, typed_memory_enabled=True
    )
    who = principal("a")
    source = append_event(events, who, "plan", "I plan to visit Rome")
    operation = "typed:" + "a" * 64
    reply = "reply:" + "a" * 64
    if blocked == "stop":
        runtime._stop = lambda: True
    elif blocked == "spend":
        runtime._provider.is_spend_suspended = lambda: True
    else:
        runtime._current_request.set(lambda: False)
    runtime._evolve_worker_typed(who, source, "I plan to visit Rome", "corr", reply)
    assert (
        runtime._typed_audit.read(
            operation, who.principal_id.value, source, "I plan to visit Rome"
        )
        is None
    )
    runtime._typed_audit.reserve(operation, who, source, "I plan to visit Rome")
    runtime._typed_audit.receive(
        operation,
        who.principal_id.value,
        source,
        "I plan to visit Rome",
        _proposal(
            "goal_or_intention",
            {
                "description": "Visit Rome",
                "rationale": "Personal plan",
                "priority": 60,
                "status": "active",
                "constraints": [],
                "review_condition": "Review next month",
            },
        ),
    )
    received = runtime._typed_audit.read(
        operation, who.principal_id.value, source, "I plan to visit Rome"
    )
    runtime._commit_worker_typed(who, source, operation, received)
    assert (
        runtime._typed_audit.read(
            operation, who.principal_id.value, source, "I plan to visit Rome"
        ).status
        == "unavailable"
    )
    assert SelfStateStore(events).retrieve_private_candidates(who) == ()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("admit_privacy", None),
        ("admit_invalid", None),
        ("duplicate_attempt", "unavailable"),
        ("preflight_stop", "unavailable"),
        ("preflight_missing_source", "unavailable"),
        ("preflight_stale", "unavailable"),
        ("preflight_privacy", "unavailable"),
        ("invalid_client", "rejected"),
        ("communication", "unavailable"),
        ("invalid_response", "rejected"),
        ("receive_revoked", "unavailable"),
        ("receive_privacy", "unavailable"),
        ("receive_invalid", "unavailable"),
    ],
)
def test_typed_runtime_failures_leave_no_state(
    tmp_path, monkeypatch, failure, expected
):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    gate = PrivacyReadGate(PrivacyLedger(tmp_path / "ledger.db"), events)
    assert gate.reconcile()
    client = OpenAIWorkerClient(
        tmp_path / "absent.sock", expected_worker_uid=os.getuid()
    )
    runtime = WorkerConversationRuntime(
        events, gate, client, provider_state=provider, typed_memory_enabled=True
    )
    who = principal("a")
    source = append_event(events, who, "plan", "I plan to visit Rome")
    operation = "typed:" + "a" * 64
    proposal = _proposal(
        "goal_or_intention",
        {
            "description": "Visit Rome",
            "rationale": "Personal plan",
            "priority": 60,
            "status": "active",
            "constraints": [],
            "review_condition": "Review next month",
        },
    )
    original_authorized = gate.authorized
    calls = 0

    def authorized(effect):
        nonlocal calls
        calls += 1
        if (
            (failure == "admit_privacy" and calls == 1)
            or (failure == "preflight_privacy" and calls == 2)
            or (failure == "receive_privacy" and calls == 3)
        ):
            raise PrivacyReadBlocked("synthetic privacy revocation")
        return original_authorized(effect)

    monkeypatch.setattr(gate, "authorized", authorized)
    if failure == "admit_invalid":
        monkeypatch.setattr(
            runtime._typed_audit,
            "reserve",
            lambda *_: (_ for _ in ()).throw(ValueError()),
        )
    elif failure == "duplicate_attempt":
        monkeypatch.setattr(
            provider, "reserve_attempt", lambda *_: AttemptReservationStatus.DUPLICATE
        )
    elif failure == "preflight_stop":
        original_reserve = provider.reserve_attempt

        def reserve(*args):
            result = original_reserve(*args)
            runtime._stop = lambda: True
            return result

        monkeypatch.setattr(provider, "reserve_attempt", reserve)
    elif failure == "preflight_missing_source":
        monkeypatch.setattr(
            runtime._worker_cognition._read,
            "retrieve_exact_private_event",
            lambda *_: None,
        )
    elif failure == "preflight_stale":
        monkeypatch.setattr(
            runtime._worker_cognition._read,
            "retrieve_all_private_candidates",
            lambda *_: (SimpleNamespace(item_id="other", revision=1),),
        )
    elif failure == "receive_invalid":
        monkeypatch.setattr(
            runtime._typed_audit,
            "receive",
            lambda *_: (_ for _ in ()).throw(ValueError()),
        )

    def infer(*_args, **_kwargs):
        if failure == "invalid_client":
            raise ValueError("invalid client result")
        if failure == "receive_revoked":
            runtime._current_request.set(lambda: False)
        if failure in {"communication", "invalid_response"}:
            return SimpleNamespace(
                outcome="communication_failure"
                if failure == "communication"
                else "invalid_response",
                typed=None,
            )
        return SimpleNamespace(outcome="completed", typed=proposal)

    client.infer = infer
    runtime._evolve_worker_typed(
        who, source, "I plan to visit Rome", "corr", "reply:" + "a" * 64
    )
    result = runtime._typed_audit.read(
        operation, who.principal_id.value, source, "I plan to visit Rome"
    )
    assert (None if result is None else result.status) == expected
    assert SelfStateStore(events).retrieve_private_candidates(who) == ()


@pytest.mark.parametrize("failure", ["stale", "privacy"])
def test_typed_commit_reconciles_stale_or_revoked_read(tmp_path, monkeypatch, failure):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    gate = PrivacyReadGate(PrivacyLedger(tmp_path / "ledger.db"), events)
    assert gate.reconcile()
    client = OpenAIWorkerClient(
        tmp_path / "absent.sock", expected_worker_uid=os.getuid()
    )
    runtime = WorkerConversationRuntime(
        events, gate, client, provider_state=provider, typed_memory_enabled=True
    )
    who = principal("a")
    source = append_event(events, who, "plan", "I plan to visit Rome")
    operation = "typed:" + "a" * 64
    runtime._typed_audit.reserve(operation, who, source, "I plan to visit Rome")
    runtime._typed_audit.receive(
        operation,
        who.principal_id.value,
        source,
        "I plan to visit Rome",
        _proposal(
            "goal_or_intention",
            {
                "description": "Visit Rome",
                "rationale": "Personal plan",
                "priority": 60,
                "status": "active",
                "constraints": [],
                "review_condition": "Review next month",
            },
        ),
    )
    received = runtime._typed_audit.read(
        operation, who.principal_id.value, source, "I plan to visit Rome"
    )
    if failure == "stale":
        monkeypatch.setattr(
            runtime._worker_cognition,
            "commit_worker_typed",
            lambda *_: CognitionOutcome(
                CognitionOutcomeStatus.REJECTED, CognitionReason.STALE_REVISION
            ),
        )
    else:
        monkeypatch.setattr(
            gate,
            "authorized",
            lambda *_: (_ for _ in ()).throw(PrivacyReadBlocked("revoked")),
        )
    runtime._commit_worker_typed(who, source, operation, received)
    assert (
        runtime._typed_audit.read(
            operation, who.principal_id.value, source, "I plan to visit Rome"
        ).status
        == "unavailable"
    )
    assert SelfStateStore(events).retrieve_private_candidates(who) == ()


def test_typed_reserved_operation_is_not_retransmitted(tmp_path):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    gate = PrivacyReadGate(PrivacyLedger(tmp_path / "ledger.db"), events)
    assert gate.reconcile()
    client = OpenAIWorkerClient(
        tmp_path / "absent.sock", expected_worker_uid=os.getuid()
    )
    client.infer = lambda *_args, **_kwargs: pytest.fail(
        "reserved operation retransmitted"
    )
    runtime = WorkerConversationRuntime(
        events, gate, client, provider_state=provider, typed_memory_enabled=True
    )
    who = principal("a")
    source = append_event(events, who, "plan", "I plan to visit Rome")
    runtime._typed_audit.reserve(
        "typed:" + "a" * 64, who, source, "I plan to visit Rome"
    )
    runtime._evolve_worker_typed(
        who, source, "I plan to visit Rome", "corr", "reply:" + "a" * 64
    )
    assert (
        runtime._typed_audit.read(
            "typed:" + "a" * 64, who.principal_id.value, source, "I plan to visit Rome"
        ).status
        == "reserved"
    )


def test_typed_preflight_requires_every_exposed_source(tmp_path, monkeypatch):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    gate = PrivacyReadGate(PrivacyLedger(tmp_path / "ledger.db"), events)
    assert gate.reconcile()
    client = OpenAIWorkerClient(
        tmp_path / "absent.sock", expected_worker_uid=os.getuid()
    )
    client.infer = lambda *_args, **_kwargs: pytest.fail(
        "provider contacted with missing source"
    )
    runtime = WorkerConversationRuntime(
        events, gate, client, provider_state=provider, typed_memory_enabled=True
    )
    who = principal("a")
    old = append_event(events, who, "old", "I enjoy Italian art")
    initial = runtime._typed_audit.reserve(
        "typed:initial", who, old, "I enjoy Italian art"
    )
    assert (
        runtime._worker_cognition.commit_worker_typed(
            who,
            old,
            "typed:initial",
            initial.snapshot,
            _proposal(
                "durable_interest",
                {
                    "topic": "Italian art",
                    "strength": 60,
                    "status": "active",
                    "reason_for_interest": "Personal enjoyment",
                },
            ),
        ).status
        is CognitionOutcomeStatus.PERSISTED
    )
    source = append_event(events, who, "new", "I also enjoy Italian sculpture")
    original = runtime._worker_cognition._read.retrieve_exact_private_event
    monkeypatch.setattr(
        runtime._worker_cognition._read,
        "retrieve_exact_private_event",
        lambda context, event_id: None
        if event_id == old
        else original(context, event_id),
    )
    runtime._evolve_worker_typed(
        who, source, "I also enjoy Italian sculpture", "corr", "reply:" + "a" * 64
    )
    assert (
        runtime._typed_audit.read(
            "typed:" + "a" * 64,
            who.principal_id.value,
            source,
            "I also enjoy Italian sculpture",
        ).status
        == "unavailable"
    )


def test_typed_runtime_mode_flags_are_explicit(tmp_path):
    events = EventStore(tmp_path / "events.db")
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    gate = PrivacyReadGate(PrivacyLedger(tmp_path / "ledger.db"), events)
    assert gate.reconcile()
    client = OpenAIWorkerClient(
        tmp_path / "absent.sock", expected_worker_uid=os.getuid()
    )
    with pytest.raises(TypeError, match="typed_memory_enabled must be bool"):
        WorkerConversationRuntime(
            events, gate, client, provider_state=provider, typed_memory_enabled=1
        )
    combined = WorkerConversationRuntime(
        events,
        gate,
        client,
        provider_state=provider,
        typed_memory_enabled=True,
        memory_evolution_enabled=True,
    )
    assert combined._typed_audit is not None
    assert combined._evolution_audit is not None
    assert combined._worker_cognition._capabilities.private_state_write
    assert combined._worker_cognition._capabilities.generic_memory_write
    assert combined._worker_cognition._capabilities.private_consolidation
    extracted = WorkerConversationRuntime(
        events,
        gate,
        client,
        provider_state=provider,
        typed_memory_enabled=True,
        memory_extraction_enabled=True,
    )
    assert extracted._extraction_audit is not None
    assert extracted._typed_audit is not None
    combined_with_extraction = WorkerConversationRuntime(
        events,
        gate,
        client,
        provider_state=provider,
        typed_memory_enabled=True,
        memory_evolution_enabled=True,
        memory_extraction_enabled=True,
    )
    assert combined_with_extraction._evolution_audit is not None
    assert combined_with_extraction._extraction_audit is None


def test_combined_memory_http_ipc_restart_eviction_and_deletion(tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    _auth, events, ledger, gate, app, a, b = _app(tmp_path, socket_path)
    ipc_path = tmp_path / "ipc-state.db"

    def runtime(current_events, current_gate):
        return WorkerConversationRuntime(
            current_events,
            current_gate,
            OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
            provider_state=ProviderStateStore(EventStore(ipc_path)),
            memory_extraction_enabled=True,
            memory_evolution_enabled=True,
            typed_memory_enabled=True,
            context_limits=ContextLimits(32000, 5),
        )

    app._conversations = runtime(events, gate)

    class SDK:
        def __init__(self):
            self.requests = []

        def create(self, **kwargs):
            self.requests.append(kwargs)
            data = json.loads(kwargs["input"])
            task = data["task"]
            content = data.get("participant_input", "")
            if task == "evolve_private_memory":
                if "renamed" in content:
                    proposal = {
                        "decision": "revise_memory",
                        "handles": [data["memories"][0]["handle"]],
                        "kind": "candidate_useful_memory",
                        "statement": "The fictional robot project is Cobalt",
                        "confidence": 0.8,
                    }
                elif "Amber" in content:
                    proposal = {
                        "decision": "new_memory",
                        "handles": [],
                        "kind": "candidate_useful_memory",
                        "statement": "The fictional robot project is Amber",
                        "confidence": 0.8,
                    }
                else:
                    proposal = {
                        "decision": "no_change",
                        "handles": [],
                        "kind": None,
                        "statement": None,
                        "confidence": None,
                    }
            elif task == "evolve_private_typed_memory":
                if "renamed" in content:
                    decision = "revise_memory"
                    handle = data["memories"][0]["handle"]
                    topic = "silent gears"
                elif "Amber" in content:
                    decision = "new_memory"
                    handle = None
                    topic = "quiet motors"
                else:
                    decision = "no_change"
                    handle = topic = None
                proposal = {
                    "decision": decision,
                    "handle": handle,
                    "kind": "durable_interest" if topic else None,
                    "source_role": "supporting" if topic else None,
                    "fields": {
                        "topic": topic,
                        "strength": 70,
                        "status": "active",
                        "reason_for_interest": "Fictional robot design preference",
                    }
                    if topic
                    else None,
                }
            else:
                proposal = {
                    "proposal": {
                        "decision": "reply",
                        "content": (
                            "OK"
                            if "unrelated garden inventory"
                            in data["conversation"][-1]["content"]
                            else "Текущий проект — Cobalt; мне нравятся silent gears."
                        ),
                    }
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

    sdk = SDK()
    server = OpenAIWorkerServer(
        WorkerServerConfig(socket_path, frozenset({os.getuid()})),
        TrustedOpenAIGateway(
            store=ProviderStateStore(EventStore(tmp_path / "provider.db")),
            transport=OpenAIResponsesTransport(SimpleNamespace(responses=sdk)),
            config=replace(
                default_openai_provider_config(),
                deployment_stage=DeploymentStage.STAGE_1,
                paid_calls_enabled=True,
                max_attempts=1,
            ),
        ),
        evolution_enabled=True,
        typed_enabled=True,
    )
    server.start()
    worker = Thread(
        target=lambda: server.serve(max_connections=MAX_OPERATIONS + 3), daemon=True
    )
    worker.start()
    try:
        reopened = None

        def send(message_id, content):
            return json.loads(a.submit(message_id, content)[2])

        def restart():
            nonlocal reopened
            reopened = EventStore(events.database_path)
            reopened_gate = PrivacyReadGate(ledger, reopened)
            assert reopened_gate.reconcile()
            app._conversations = runtime(reopened, reopened_gate)

        def inspect():
            replies = [
                json.loads(call["input"])
                for call in sdk.requests
                if json.loads(call["input"])["task"] == "private_conversation"
            ]
            return replies[-1]

        def delete():
            assert reopened is not None
            with reopened._open() as db:
                source = db.execute(
                    "SELECT event_id FROM event_envelopes WHERE source_message_id='robot-revision'"
                ).fetchone()[0]
            backup = tmp_path / "before-deletion.db"
            create_sqlite_backup(reopened.database_path, backup, 13, NOW)
            disposition = (
                DeletionCoordinator(ledger, EventStoreErasureAdapter(reopened))
                .delete(deletion_context(), deletion_request(content_reference=source))
                .disposition.value
            )
            assert app._conversations._gate.reconcile()
            quarantine = tmp_path / "quarantine.db"
            assert (
                QuarantinedRestore(ledger, 13, EventStoreRestoreAdapter)
                .restore(backup, quarantine, NOW)
                .activation_eligible
            )
            with sqlite3.connect(f"file:{quarantine}?immutable=1", uri=True) as db:
                assert db.execute(
                    "SELECT COUNT(*) FROM generic_memory_payloads WHERE statement LIKE '%Cobalt%'"
                ).fetchone() == (0,)
                assert db.execute(
                    "SELECT COUNT(*) FROM state_revision_payloads WHERE topic='silent gears'"
                ).fetchone() == (0,)
            return {"status": disposition}

        run_robot_journey(send, restart, inspect, delete)
        assert a.submit("robot-original", FIRST)[0] == "200 OK"
        assert len(sdk.requests) == MAX_OPERATIONS
        assert [json.loads(call["input"])["task"] for call in sdk.requests] == [
            "evolve_private_memory",
            "evolve_private_typed_memory",
            "private_conversation",
        ] * (MAX_OPERATIONS // 3)
        assert all(call["max_output_tokens"] == 512 for call in sdk.requests)
        assert all(
            len(call["input"].encode())
            <= (
                32000
                if json.loads(call["input"])["task"] == "private_conversation"
                else 16384
            )
            for call in sdk.requests
        )
        with EventStore(ipc_path)._open() as db:
            assert db.execute(
                "SELECT status,COUNT(*) FROM private_extraction_audit GROUP BY status"
            ).fetchall() == [("accepted", 4), ("no_change", 14)]
        assert (
            json.loads(b.submit("other", "What is my robot project?")[2])["model_state"]
            == "completed"
        )
        assert "Cobalt" not in sdk.requests[-1]["input"]
        assert len(sdk.requests) == MAX_OPERATIONS + 3
        with EventStore(tmp_path / "provider.db")._open() as db:
            assert db.execute(
                "SELECT COUNT(*),SUM(input_tokens),SUM(output_tokens),MAX(retry_count) "
                "FROM provider_usage_attempts"
            ).fetchone() == (
                MAX_OPERATIONS + 3,
                MAX_OPERATIONS + 3,
                MAX_OPERATIONS + 3,
                0,
            )
    finally:
        worker.join(5)
        server.close()


def test_typed_receipt_rejects_identity_loss_and_duplicate_response(tmp_path):
    events = EventStore(tmp_path / "events.db")
    audit = TypedAuditStore(ProviderStateStore(EventStore(tmp_path / "ipc.db")), events)
    who = principal("a")
    source = append_event(events, who, "plan", "I plan to visit Rome")
    first = audit.reserve("typed:first", who, source, "I plan to visit Rome")
    assert (
        audit.reserve("typed:first", who, source, "I plan to visit Rome").status
        == "reserved"
    )
    with pytest.raises(ValueError, match="identity conflict"):
        audit.read("typed:first", "other", source, "I plan to visit Rome")
    no_change = TypedProposal("no_change", None, None, None, None)
    audit.receive(
        "typed:first", who.principal_id.value, source, "I plan to visit Rome", no_change
    )
    assert (
        audit.read(
            "typed:first", who.principal_id.value, source, "I plan to visit Rome"
        ).status
        == "received"
    )
    with events._open() as db:
        _validate_graph(db)
    with pytest.raises(ValueError, match="receipt conflict"):
        audit.receive(
            "typed:first",
            who.principal_id.value,
            source,
            "I plan to visit Rome",
            no_change,
        )
    audit.finish("typed:first", "no_change")
    assert (
        audit.read(
            "typed:first", who.principal_id.value, source, "I plan to visit Rome"
        ).status
        == "no_change"
    )
    assert first.request.memories == ()
    with pytest.raises(ValueError, match="receipt unavailable"):
        audit.receive(
            "typed:missing",
            who.principal_id.value,
            source,
            "I plan to visit Rome",
            no_change,
        )

    audit.reserve("typed:lost", who, source, "I plan to visit Rome")
    with events._open() as db:
        with db:
            db.execute(
                "DELETE FROM private_extraction_receipts WHERE operation_id='typed:lost'"
            )
    assert (
        audit.read(
            "typed:lost", who.principal_id.value, source, "I plan to visit Rome"
        ).status
        == "unavailable"
    )


def test_typed_receipt_rejects_corrupt_model_visible_snapshot(tmp_path):
    events = EventStore(tmp_path / "events.db")
    audit = TypedAuditStore(ProviderStateStore(EventStore(tmp_path / "ipc.db")), events)
    who = principal("a")
    old = append_event(events, who, "old", "I plan to visit Rome")
    engine = CognitionEngine(events, SelfStateStore(events))
    initial = audit.reserve("typed:initial", who, old, "I plan to visit Rome")
    fields = {
        "description": "Visit Rome",
        "rationale": "Personal plan",
        "priority": 60,
        "status": "active",
        "constraints": [],
        "review_condition": "Review next month",
    }
    assert (
        engine.commit_worker_typed(
            who,
            old,
            "typed:initial",
            initial.snapshot,
            _proposal("goal_or_intention", fields),
        ).status
        is CognitionOutcomeStatus.PERSISTED
    )
    source = append_event(events, who, "new", "I moved the visit")
    audit.reserve("typed:corrupt", who, source, "I moved the visit")
    with events._open() as db:
        original = db.execute(
            "SELECT proposal FROM private_extraction_receipts WHERE operation_id='typed:corrupt'"
        ).fetchone()[0]
    valid = json.loads(original)
    assert len(valid["snapshot"]["selected"]) == 1
    for variant in range(22):
        value = json.loads(original)
        snapshot = value["snapshot"]
        selected = snapshot["selected"]
        visible = snapshot["visible"]
        sources = snapshot["sources"]
        if variant == 0:
            value["version"] = "future"
        elif variant == 1:
            value["snapshot"] = []
        elif variant == 2:
            snapshot["selected"] = selected * 9
        elif variant == 3:
            snapshot["sources"] = sources * 51
        elif variant == 4:
            snapshot["selected"] = selected * 2
        elif variant == 5:
            snapshot["selected"] = [["h" + "0" * 16, *selected[0][1:]], selected[0]]
        elif variant == 6:
            snapshot["visible"] = visible * 2
        elif variant == 7:
            snapshot["sources"] = sources * 2
        elif variant == 8:
            snapshot["visible"] = []
        elif variant == 9:
            selected[0].pop()
        elif variant == 10:
            selected[0][0] = "bad-handle"
        elif variant == 11:
            selected[0][1] = 7
        elif variant == 12:
            selected[0][2] = 0
        elif variant == 13:
            selected[0][3] = "invalid"
        elif variant == 14:
            visible[0].pop()
        elif variant == 15:
            visible[0][0] = 7
        elif variant == 16:
            visible[0][1] = 0
        elif variant == 17:
            sources[0] = 7
        elif variant == 18:
            selected[0][1] = "missing"
            visible[0][0] = "missing"
        elif variant == 19:
            snapshot["sources"] = []
        elif variant == 20:
            value["proposal"] = {"decision": "bad"}
        else:
            value["unexpected"] = True
        with events._open() as db:
            with db:
                db.execute(
                    "UPDATE private_extraction_receipts SET proposal=? WHERE operation_id='typed:corrupt'",
                    (json.dumps(value),),
                )
        with pytest.raises(ValueError, match="invalid typed receipt"):
            audit.read(
                "typed:corrupt", who.principal_id.value, source, "I moved the visit"
            )
        with events._open() as db:
            with pytest.raises(ApplicationPrivacyError, match="invalid_typed_receipt"):
                _validate_graph(db)


def test_typed_receipt_bounds_and_source_roles_are_checked(tmp_path, monkeypatch):
    from app.cognition.read_store import CognitionReadStore
    from app.private_web.typed_audit import _kind_fields

    with pytest.raises(ValueError, match="invalid typed head"):
        _kind_fields(object())
    events = EventStore(tmp_path / "events.db")
    audit = TypedAuditStore(ProviderStateStore(EventStore(tmp_path / "ipc.db")), events)
    who = principal("a")
    old = append_event(events, who, "old", "I plan to visit Rome")
    engine = CognitionEngine(events, SelfStateStore(events))
    first = audit.reserve("typed:first", who, old, "I plan to visit Rome")
    fields = {
        "description": "Visit Rome",
        "rationale": "Personal plan",
        "priority": 60,
        "status": "active",
        "constraints": [],
        "review_condition": "Review next month",
    }
    assert (
        engine.commit_worker_typed(
            who,
            old,
            "typed:first",
            first.snapshot,
            _proposal("goal_or_intention", fields),
        ).status
        is CognitionOutcomeStatus.PERSISTED
    )
    source = append_event(events, who, "new", "I moved the visit")
    audit.reserve("typed:roles", who, source, "I moved the visit")
    with pytest.raises(ValueError, match="receipt conflict"):
        audit._decode("typed:roles", "other", source)
    with events._open() as db:
        proposal = db.execute(
            "SELECT proposal FROM private_extraction_receipts WHERE operation_id='typed:roles'"
        ).fetchone()[0]
        with db:
            db.execute(
                "UPDATE private_extraction_receipts SET proposal=? WHERE operation_id='typed:roles'",
                (" " * 131_073,),
            )
    with pytest.raises(ValueError, match="invalid typed receipt"):
        audit.read("typed:roles", who.principal_id.value, source, "I moved the visit")
    with events._open() as db:
        with pytest.raises(ApplicationPrivacyError, match="invalid_typed_receipt"):
            _validate_graph(db)
    with events._open() as db:
        with db:
            db.execute(
                "UPDATE private_extraction_receipts SET proposal=? WHERE operation_id='typed:roles'",
                (proposal,),
            )
            db.execute(
                "UPDATE protected_state_provenance SET retention_status='invalidated' WHERE event_id=?",
                (old,),
            )
    with pytest.raises(ValueError, match="invalid typed receipt"):
        audit.read("typed:roles", who.principal_id.value, source, "I moved the visit")
    with events._open() as db:
        with pytest.raises(ApplicationPrivacyError, match="invalid_typed_receipt"):
            _validate_graph(db)
    with events._open() as db:
        with db:
            db.execute(
                "UPDATE protected_state_provenance SET retention_status='active' WHERE event_id=?",
                (old,),
            )

    # A response cannot turn a terminal operation into a received operation.
    audit.reserve("typed:terminal", who, source, "I moved the visit")
    with audit._provider._event_store._open() as db:
        with db:
            db.execute(
                "UPDATE private_extraction_audit SET status='rejected' WHERE operation_id='typed:terminal'"
            )
    with pytest.raises(ValueError, match="receipt conflict"):
        audit.receive(
            "typed:terminal",
            who.principal_id.value,
            source,
            "I moved the visit",
            TypedProposal("no_change", None, None, None, None),
        )

    original = CognitionReadStore.retrieve_all_private_candidate_reads
    original_serialize = TypedModelRequest.serialize
    reads = original(CognitionReadStore(events, SelfStateStore(events)), who)
    assert len(reads) == 1

    def stale(self, context, *, _connection=None):
        return reads

    monkeypatch.setattr(
        CognitionReadStore, "retrieve_all_private_candidate_reads", stale
    )
    with events._open() as db:
        with db:
            db.execute(
                "UPDATE protected_state_provenance SET retention_status='invalidated' WHERE event_id=?",
                (old,),
            )
    assert (
        audit.reserve(
            "typed:stale-rows", who, source, "I moved the visit"
        ).request.memories
        == ()
    )
    with events._open() as db:
        with db:
            db.execute(
                "UPDATE protected_state_provenance SET retention_status='active' WHERE event_id=?",
                (old,),
            )

    def bounded(self):
        if self.memories:
            raise ValueError("projection exceeds bound")
        return original_serialize(self)

    monkeypatch.setattr(TypedModelRequest, "serialize", bounded)
    assert (
        audit.reserve(
            "typed:projection-bound", who, source, "I moved the visit"
        ).request.memories
        == ()
    )
    monkeypatch.setattr(TypedModelRequest, "serialize", original_serialize)

    def enormous(self, context, *, _connection=None):
        return original(self, context, _connection=_connection) * 10_000

    monkeypatch.setattr(
        CognitionReadStore, "retrieve_all_private_candidate_reads", enormous
    )
    assert audit.reserve("typed:oversized", who, source, "I moved the visit") is None


@pytest.mark.parametrize("combined", [False, True])
def test_lost_typed_response_is_uncertain_without_retransmission(
    tmp_path, monkeypatch, combined
):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    _auth, events, _ledger, gate, app, a, _b = _app(tmp_path, socket_path)
    app._conversations = WorkerConversationRuntime(
        events,
        gate,
        OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
        provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
        typed_memory_enabled=True,
        memory_evolution_enabled=combined,
    )
    proposal = {
        "decision": "no_change",
        "handle": None,
        "kind": None,
        "fields": None,
        "source_role": None,
    }
    reply = {
        "proposal": {"decision": "reply", "content": "Да, я помню этот интерес."},
    }
    provider = ProviderStateStore(EventStore(tmp_path / "provider.db"))
    server = OpenAIWorkerServer(
        WorkerServerConfig(socket_path, frozenset({os.getuid()})),
        TrustedOpenAIGateway(
            store=provider,
            transport=FakeOpenAITransport(
                (
                    [
                        provider_response(
                            json.dumps(
                                {
                                    "decision": "no_change",
                                    "handles": [],
                                    "kind": None,
                                    "statement": None,
                                    "confidence": None,
                                }
                            )
                        )
                    ]
                    if combined
                    else []
                )
                + [
                    provider_response(json.dumps(proposal)),
                    provider_response(json.dumps(reply)),
                ]
            ),
            config=replace(
                default_openai_provider_config(),
                deployment_stage=DeploymentStage.STAGE_1,
                paid_calls_enabled=True,
                max_attempts=1,
            ),
        ),
        typed_enabled=True,
        evolution_enabled=combined,
    )

    original_send = server_module.send_frame
    sent = 0

    def lost_response(*args, **kwargs):
        nonlocal sent
        sent += 1
        if sent == (2 if combined else 1):
            raise OSError("synthetic response loss")
        return original_send(*args, **kwargs)

    monkeypatch.setattr(server_module, "send_frame", lost_response)
    server.start()
    attempts = 3 if combined else 2
    thread = Thread(target=lambda: server.serve(max_connections=attempts))
    thread.start()
    try:
        a.submit("lost", "I enjoy Italian art")
        thread.join(5)
        assert not thread.is_alive()
        with provider._event_store._open() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM provider_usage_attempts"
            ).fetchone() == (attempts,)
        with EventStore(tmp_path / "ipc-state.db")._open() as db:
            assert db.execute(
                "SELECT status FROM private_extraction_audit WHERE operation_id LIKE 'typed:%'"
            ).fetchone() == ("uncertain",)
        a.submit("lost", "I enjoy Italian art")
        with provider._event_store._open() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM provider_usage_attempts"
            ).fetchone() == (attempts,)
    finally:
        thread.join(5)
        server.close()
