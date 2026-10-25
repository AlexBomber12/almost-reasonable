"""Credential-free B03E controls and six-turn technical assertions."""

from datetime import UTC, datetime, timedelta
import json

import pytest

from acceptance.private_combined_journey import (
    MESSAGES,
    MAX_OPERATIONS,
    _assert_projection,
    run_combined_journey,
)
from app.cognition.conversation_context import MemoryModelRequest
from app.cognition.worker_evolution import EvolutionMemory, EvolutionModelRequest
from app.cognition.worker_narrative import (
    NarrativeModelRequest,
    NarrativeValidationError,
    parse_narrative_provider_response,
)
from app.cognition.worker_typed import TypedMemory, TypedModelRequest
from app.core.context import ContextRole, ContextTrust, ConversationSegment
from app.core.store import EventStore
from app.private_web.cli import _narrative_permission_current
from app.trusted.openai_transport import FakeOpenAITransport
from app.trusted.openai_worker_protocol import InferenceRequest
from app.trusted.private_pilot import PilotAllowance, load_activation
from app.trusted import private_pilot_worker
from app.trusted.private_pilot_worker import PilotRequestGate, PilotStop
from test_int22_pilot import activation_file, _rewrite
from test_openai_worker_protocol import model_request


def _permission(path, version, pilot_id, limit):
    if path.exists():
        path.chmod(0o600)
    path.write_text(
        json.dumps({"version": version, "pilot_id": pilot_id, "max_operations": limit})
    )
    path.chmod(0o400)
    return path


def test_combined_permission_and_real_gate_consume_exactly_twenty(tmp_path):
    path, value = activation_file(tmp_path)
    value.update(
        version="private-pilot-activation-b03e-v1",
        task="synthetic-private-combined-memory-b03e",
        max_operations=20,
    )
    value["expires_at"] = (datetime.now(UTC) + timedelta(minutes=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _rewrite(path, value)
    with pytest.raises(Exception):
        load_activation(path)
    value["max_operations"] = 27
    _rewrite(path, value)
    with pytest.raises(Exception):
        load_activation(path, allow_memory_acceptance=True)
    value["max_operations"] = 20
    _rewrite(path, value)
    activation = load_activation(path, allow_memory_acceptance=True)
    assert activation.combined_acceptance
    stop = _permission(
        path.parent / "stop.json",
        "private-pilot-emergency-stop-v1",
        activation.pilot_id,
        20,
    )
    stop.chmod(0o600)
    stop.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": False})
    )
    stop.chmod(0o400)
    evolution = _permission(
        path.parent / "evolution.json",
        "private-memory-evolution-permission-v1",
        activation.pilot_id,
        20,
    )
    typed = _permission(
        path.parent / "typed.json",
        "private-typed-memory-permission-v1",
        activation.pilot_id,
        20,
    )
    narrative = _permission(
        path.parent / "narrative.json",
        "private-narrative-permission-v2",
        activation.pilot_id,
        20,
    )
    assert _narrative_permission_current(narrative, activation.pilot_id)
    assert not _narrative_permission_current(narrative, "wrong-scope")
    allowance = PilotAllowance.initialize(tmp_path / "allowance.db", activation)
    gate = PilotRequestGate(
        activation,
        allowance,
        activation_path=path,
        stop=PilotStop(stop),
        evolution_permission_path=evolution,
        typed_permission_path=typed,
        narrative_permission_path=narrative,
    )
    _permission(narrative, "private-narrative-permission-v2", "wrong-scope", 20)
    assert gate.narrative_limit() is None
    _permission(narrative, "private-narrative-permission-v2", activation.pilot_id, 20)
    for index in range(6):
        suffix = f"{index:064x}"
        for family, request in (
            ("evolution", EvolutionModelRequest(MESSAGES[index], ())),
            ("typed", TypedModelRequest(MESSAGES[index], ())),
        ):
            assert (
                gate.authorize(InferenceRequest(family + ":" + suffix, "corr", request))
                is None
            )
        if index in {1, 2}:
            narrative_request = NarrativeModelRequest(
                (
                    ConversationSegment(
                        ContextRole.PARTICIPANT,
                        ContextTrust.UNTRUSTED_CONVERSATIONAL_DATA,
                        MESSAGES[index],
                    ),
                ),
                (),
            )
            assert (
                gate.authorize(
                    InferenceRequest("narrative:" + suffix, "corr", narrative_request)
                )
                is None
            )
        if index == 5:
            assert allowance.remaining() == 1
            shortage = NarrativeModelRequest(
                (
                    ConversationSegment(
                        ContextRole.PARTICIPANT,
                        ContextTrust.UNTRUSTED_CONVERSATIONAL_DATA,
                        MESSAGES[2],
                    ),
                ),
                (),
            )
            assert (
                gate.authorize(
                    InferenceRequest("narrative:" + "f" * 64, "corr", shortage)
                ).outcome
                == "authorization_rejected"
            )
        assert (
            gate.authorize(
                InferenceRequest(
                    "reply:" + suffix,
                    "corr",
                    MemoryModelRequest(model_request(), (), False),
                )
            )
            is None
        )
    assert allowance.remaining() == 0
    assert (
        gate.authorize(
            InferenceRequest(
                "reply:" + "f" * 64,
                "corr",
                MemoryModelRequest(model_request(), (), False),
            )
        ).outcome
        == "authorization_rejected"
    )
    _permission(narrative, "private-narrative-permission-v1", activation.pilot_id, 20)
    assert not _narrative_permission_current(narrative, activation.pilot_id)
    assert gate.narrative_limit() is None
    _permission(narrative, "private-narrative-permission-v2", activation.pilot_id, 21)
    assert not _narrative_permission_current(narrative, activation.pilot_id)
    assert gate.narrative_limit() is None
    stop.chmod(0o600)
    stop.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": True})
    )
    stop.chmod(0o400)
    assert (
        gate.authorize(
            InferenceRequest(
                "reply:" + "e" * 64,
                "corr",
                MemoryModelRequest(model_request(), (), False),
            )
        ).outcome
        == "emergency_stop"
    )


def test_combined_worker_assembly_accepts_narrative_with_real_controls(
    tmp_path, monkeypatch
):
    path, value = activation_file(tmp_path)
    value.update(
        version="private-pilot-activation-b03e-v1",
        task="synthetic-private-combined-memory-b03e",
        max_operations=20,
    )
    value["expires_at"] = (datetime.now(UTC) + timedelta(minutes=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _rewrite(path, value)
    activation = load_activation(path, allow_memory_acceptance=True)
    stop = path.parent / "stop.json"
    stop.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": False})
    )
    stop.chmod(0o400)
    evolution = _permission(
        path.parent / "evolution.json",
        "private-memory-evolution-permission-v1",
        activation.pilot_id,
        20,
    )
    typed = _permission(
        path.parent / "typed.json",
        "private-typed-memory-permission-v1",
        activation.pilot_id,
        20,
    )
    narrative = _permission(
        path.parent / "narrative.json",
        "private-narrative-permission-v2",
        activation.pilot_id,
        20,
    )
    allowance = tmp_path / "allowance.db"
    PilotAllowance.initialize(allowance, activation)
    provider = tmp_path / "provider.db"
    EventStore(provider)
    monkeypatch.setattr(
        private_pilot_worker,
        "build_worker_openai_transport",
        lambda **_scope: FakeOpenAITransport([]),
    )
    worker = private_pilot_worker.build_worker(
        socket_path=tmp_path / "worker.sock",
        provider_database=provider,
        allowance_database=allowance,
        activation_path=path,
        stop_path=stop,
        evolution_permission_path=evolution,
        typed_permission_path=typed,
        narrative_permission_path=narrative,
        memory_acceptance=True,
    )
    try:
        assert worker._narrative_enabled
        request = NarrativeModelRequest(
            (
                ConversationSegment(
                    ContextRole.PARTICIPANT,
                    ContextTrust.UNTRUSTED_CONVERSATIONAL_DATA,
                    MESSAGES[1],
                ),
            ),
            (),
        )
        assert (
            worker._request_guard(
                InferenceRequest("narrative:" + "a" * 64, "corr", request)
            )
            is None
        )
        assert PilotAllowance(allowance, activation).remaining() == 18
    finally:
        worker.close()


@pytest.mark.parametrize(
    "proposal,code",
    [
        (
            {"decision": "create", "handle": "h0123456789abcdef", "episode": "x"},
            "narrative_invalid_handle",
        ),
        (
            {"decision": "revise", "handle": None, "episode": "x"},
            "narrative_invalid_handle",
        ),
        (
            {"decision": "no_change", "handle": None, "episode": "x"},
            "narrative_invalid_shape",
        ),
        (
            {"decision": [], "handle": None, "episode": None},
            "narrative_invalid_decision",
        ),
        (
            {"decision": "create", "handle": None, "episode": ["x"]},
            "narrative_invalid_episode",
        ),
    ],
)
def test_provider_envelope_rejects_invalid_combinations(proposal, code):
    with pytest.raises(NarrativeValidationError) as failure:
        parse_narrative_provider_response(json.dumps({"proposal": proposal}))
    assert failure.value.code == code


def test_combined_journey_requires_projection_and_revision():
    state = {"turn": 0, "deleted": False}

    def send(_step, _message):
        state["turn"] += 1
        return {"model_state": "completed", "response": "Синтетический ответ."}

    def heads():
        if state["deleted"]:
            return []
        return [("episode", 1 if state["turn"] == 2 else 2)]

    def delete():
        state["deleted"] = True
        return {"status": "applied"}

    def records():
        episode = {
            "text": "Искра открывает Волна",
            "classification": "fictional_autobiography",
            "trust": "untrusted_fiction_data",
            "can_instruct": False,
            "verified_action": False,
            "evidence": False,
        }
        data = {
            "task": "private_conversation",
            "conversation": [],
            "memories": [{"text": "Кобальт Ладога"}],
            "fictional_episodes": [] if state["deleted"] else [episode],
        }
        rows = []
        for index in range(state["turn"]):
            rows.extend(
                {
                    "purpose": purpose,
                    "input": json.dumps({"participant_input": MESSAGES[index]}),
                }
                for purpose in ("memory_evolution", "typed_memory_evolution")
            )
            if index in {1, 2}:
                shown = (
                    []
                    if index == 1
                    else [{"text": "Искра", "trust": "untrusted_fiction_data"}]
                )
                rows.append(
                    {
                        "purpose": "narrative_evolution",
                        "input": json.dumps({"episodes": shown}),
                    }
                )
            rows.append({"purpose": "agent", "input": json.dumps(data)})
        return rows

    result = run_combined_journey(send, lambda: None, records, heads, delete)
    assert state["turn"] == 6
    assert result["technical_journey"] == "passed"
    assert result["human_semantic_review"] == "pending"
    assert len(result["transcript"]) == 4
    assert MAX_OPERATIONS == 20
    assert len(MESSAGES) == 6


@pytest.mark.parametrize("family", ["memory_evolution", "typed_memory_evolution"])
def test_projection_rejects_episode_text_in_factual_writer(family):
    episode_text = "На маяке я нашёл ключ Искра, который открывает дверь Волна."
    reply = {
        "task": "private_conversation",
        "conversation": [],
        "memories": [{"text": "Кобальт Ладога"}],
        "fictional_episodes": [
            {
                "text": episode_text,
                "classification": "fictional_autobiography",
                "trust": "untrusted_fiction_data",
                "can_instruct": False,
                "verified_action": False,
                "evidence": False,
            }
        ],
    }
    if family == "memory_evolution":

        def request(text):
            memories = (
                (EvolutionMemory("memory1", "candidate_useful_memory", text, 0.8),)
                if text
                else ()
            )
            return EvolutionModelRequest(episode_text, memories).serialize()
    else:

        def request(text):
            memories = (
                (
                    TypedMemory(
                        "h0123456789abcdef",
                        "durable_interest",
                        {
                            "topic": text,
                            "strength": 80,
                            "status": "active",
                            "reason_for_interest": "Participant preference",
                        },
                    ),
                )
                if text
                else ()
            )
            return TypedModelRequest(episode_text, memories).serialize()

    rows = [
        {"purpose": family, "input": request(None)},
        {"purpose": "agent", "input": json.dumps(reply, ensure_ascii=False)},
    ]
    assert _assert_projection(rows) == reply
    rows[0]["input"] = request(episode_text)
    with pytest.raises(AssertionError, match="fiction entered a factual memory writer"):
        _assert_projection(rows)
