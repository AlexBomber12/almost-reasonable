"""Protected ordinary model selection and conservative grant replacement."""

from datetime import timedelta
import json
import os
import sqlite3
from threading import Thread

import pytest

from app.cognition.conversation_context import MemoryModelRequest
from app.core.openai_worker_client import OpenAIWorkerClient
from app.core.provider_contract import build_openai_request
from app.core.store import EventStore
from app.ops import private_pilot_ops
from app.trusted import private_pilot_worker
from app.trusted.openai_transport import FakeOpenAITransport
from app.trusted.private_pilot import (
    PilotAllowance,
    PilotAuthorityError,
    load_activation,
)
from test_b05e_combined import reply
from test_int22_controls import _stop
from test_int22_pilot import activation_file, _rewrite
from test_openai_worker_protocol import model_request
from test_private_operating_grant import NOW, grant


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6.1-sol"])
@pytest.mark.parametrize("intellectual", [False, True])
def test_ordinary_worker_uses_protected_model_and_records_real_request_model(
    tmp_path, monkeypatch, model, intellectual
):
    path, value, _ = grant(tmp_path)
    value["model"] = model
    _rewrite(path, value)
    authority = load_activation(path, now=NOW)
    assert authority.model == model
    assert private_pilot_ops.plan(path, now=NOW)["model"] == model
    db = tmp_path / "allowance.db"
    allowance = PilotAllowance.initialize(db, authority)
    provider = tmp_path / "provider.db"
    EventStore(provider)
    stop = path.parent / "stop.json"
    _stop(stop, False)
    assert (
        private_pilot_ops.inspect(path, db, stop, provider, now=NOW)["model"] == model
    )
    fake = FakeOpenAITransport([reply("Синтетический ответ.")])
    monkeypatch.setattr(
        private_pilot_worker, "build_worker_openai_transport", lambda **_kwargs: fake
    )
    worker = private_pilot_worker.build_worker(
        socket_path=tmp_path / "worker.sock",
        provider_database=provider,
        allowance_database=db,
        activation_path=path,
        stop_path=stop,
        clock=lambda: NOW,
    )
    worker.start()
    thread = Thread(target=worker.serve, kwargs={"max_connections": 1})
    thread.start()
    try:
        result = OpenAIWorkerClient(
            tmp_path / "worker.sock", expected_worker_uid=os.geteuid()
        ).infer(
            MemoryModelRequest(
                model_request(),
                (),
                False,
                intellectual_state={
                    "version": "agent-intellectual-state-v1",
                    "states": [],
                    "sources": [],
                }
                if intellectual
                else None,
            ),
            operation_id="reply:" + "a" * 64,
            correlation_id="synthetic-model-selection",
        )
        assert result.outcome == "completed"
        assert len(fake.calls) == 1
        request = fake.calls[0].request
        payload = request.to_sdk_kwargs()
        assert payload["model"] == model
        assert payload["max_output_tokens"] == (2048 if model == "gpt-6.1-sol" else 512)
        if model == "gpt-6.1-sol":
            assert payload["reasoning"] == {"effort": "low"}
        else:
            assert "reasoning" not in payload
        assert payload["tools"] == [] and payload["store"] is False
        assert payload["text"]["format"]["strict"] is True
        facts = json.loads(
            payload["instructions"]
            .split("Trusted dialogue identity (application-owned):\n", 1)[1]
            .splitlines()[0]
        )
        assert facts["application_name"] == "Almost Reasonable"
        assert facts["configured_model"] == model
        expected_prompt = (
            "agent-proposal-v3-intellectual-v4" if intellectual else "agent-proposal-v3"
        )
        if intellectual:
            assert result.validation.intellectual_prompt == expected_prompt
        assert allowance.remaining() == 39
        with sqlite3.connect(provider) as connection:
            assert connection.execute(
                "SELECT model_identifier, prompt_version FROM provider_usage_attempts"
            ).fetchall() == [(model, expected_prompt)]
    finally:
        thread.join(5)
        worker.close()
        assert not thread.is_alive()


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-sol", "unknown", [], True])
def test_ordinary_unknown_or_malformed_model_is_rejected(tmp_path, model):
    path, value, _ = grant(tmp_path)
    value["model"] = model
    _rewrite(path, value)
    with pytest.raises(PilotAuthorityError):
        load_activation(path, now=NOW)


def test_legacy_pilot_stays_pinned_to_luna(tmp_path):
    path, value = activation_file(tmp_path)
    assert load_activation(path).model == "gpt-6-luna"
    value["model"] = "gpt-6.1-sol"
    _rewrite(path, value)
    with pytest.raises(PilotAuthorityError):
        load_activation(path)


def test_sol_replacement_retains_consumption_and_cannot_reuse_old_reply(tmp_path):
    path, value, original = grant(tmp_path, quota=5)
    db = tmp_path / "allowance.db"
    old = PilotAllowance.initialize(db, original)
    operation = "reply:" + "a" * 64
    assert old.reserve(operation)
    value.update(pilot_id="synthetic-sol-switch", model="gpt-6.1-sol", max_operations=4)
    _rewrite(path, value)
    selected = load_activation(path, now=NOW + timedelta(seconds=1))
    assert selected.expires_at == original.expires_at
    with pytest.raises(PilotAuthorityError):
        PilotAllowance(db, selected)  # Artifact edits cannot select or refill a grant.
    private_pilot_ops.replace(path, db)
    current = PilotAllowance(db, selected)
    assert not old.current(NOW)
    assert old.remaining() == current.remaining() == 4
    assert not current.reserve(operation)
    for index in range(4):
        assert current.reserve("reply:" + f"{index:064x}")
    assert not current.reserve("reply:" + "b" * 64)
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT count(*) FROM operating_consumed"
        ).fetchone() == (5,)
        assert connection.execute(
            "SELECT count(*) FROM operating_grants WHERE retired=1"
        ).fetchone() == (1,)


def test_sol_builder_preserves_closed_contract_for_intellectual_requests():
    from dataclasses import replace
    from test_provider_contract import enabled_config, model_request as plain_request

    request = build_openai_request(plain_request(), enabled_config(model="gpt-6.1-sol"))
    payload = replace(request, intellectual=True).to_sdk_kwargs()
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["text"]["format"]["strict"] is True
    assert payload["tools"] == [] and payload["store"] is False
    assert (
        not {"temperature", "top_p", "top_logprobs", "previous_response_id"}
        & payload.keys()
    )
