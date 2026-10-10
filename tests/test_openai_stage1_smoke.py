from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import runpy
import sys

import pytest

from app.core.context import ContextRole, ContextTrust, ConversationSegment
from app.core.openai_stage1_contract import (
    STAGE1_CORRELATION_ID,
    STAGE1_MODEL,
    STAGE1_OPERATION_ID,
    STAGE1_PROVIDER_CONFIG_VERSION,
    build_stage1_synthetic_request,
    stage1_model_request_digest,
)
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.trusted.openai_credential_files import TrustedTransportConfigurationError
from app.trusted import openai_stage1_smoke as smoke
from app.trusted.openai_gateway import GatewayOutcome, TrustedOpenAIGateway
from app.trusted.openai_transport import (
    FakeOpenAITransport,
    OpenAITransportError,
)
from app.trusted.openai_stage1_smoke import (
    ACTIVATION_VERSION,
    STOP_VERSION,
    Stage1RequestGate,
    WorkerEmergencyStop,
    load_stage1_activation,
    stage1_pricing,
    stage1_provider_config,
)
from app.trusted.openai_worker_protocol import InferenceRequest


NOW = datetime(2026, 10, 10, 18, 0, tzinfo=UTC)


def protected_json(path: Path, value: object) -> Path:
    if path.exists():
        path.chmod(0o600)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o400)
    return path


def activation_data(**changes):
    request = build_stage1_synthetic_request()
    value = {
        "version": ACTIVATION_VERSION,
        "session_id": "operator-smoke-session-011",
        "operation_id": STAGE1_OPERATION_ID,
        "correlation_id": STAGE1_CORRELATION_ID,
        "model_request_sha256": stage1_model_request_digest(request),
        "provider_config_version": STAGE1_PROVIDER_CONFIG_VERSION,
        "model": STAGE1_MODEL,
        "project_id": "proj_synthetic",
        "organization_id": "org-synthetic",
        "not_before": "2026-10-10T17:55:00Z",
        "expires_at": "2026-10-10T18:05:00Z",
    }
    value.update(changes)
    return value


def stop_file(tmp_path, *, stopped=False, name="stop.json"):
    return protected_json(tmp_path / name, {"version": STOP_VERSION, "stop": stopped})


def test_activation_binds_exact_session_operation_request_model_and_scope(tmp_path):
    path = protected_json(tmp_path / "activation.json", activation_data())

    activation = load_stage1_activation(path, now=NOW)

    assert activation.session_id == "operator-smoke-session-011"
    assert activation.operation_id == STAGE1_OPERATION_ID
    assert activation.correlation_id == STAGE1_CORRELATION_ID
    assert activation.model == STAGE1_MODEL
    assert activation.project_id == "proj_synthetic"
    assert activation.organization_id == "org-synthetic"
    assert "synthetic" not in repr(activation)


@pytest.mark.parametrize(
    "changes",
    [
        {"operation_id": "other-operation"},
        {"correlation_id": "other-correlation"},
        {"model_request_sha256": "0" * 64},
        {"provider_config_version": "other-config"},
        {"model": "gpt-6.1-sol"},
        {"project_id": "other"},
        {"organization_id": "other"},
        {"session_id": "bad value"},
        {"not_before": "2026-10-10T18:01:00Z"},
        {"expires_at": "2026-10-10T17:59:00Z"},
        {"expires_at": "2026-10-10T19:00:01Z"},
        {"expires_at": "not-time"},
    ],
)
def test_invalid_stale_or_mismatched_activation_fails_closed(tmp_path, changes):
    path = protected_json(tmp_path / "activation.json", activation_data(**changes))

    with pytest.raises(TrustedTransportConfigurationError):
        load_stage1_activation(path, now=NOW)


def test_missing_malformed_extra_field_and_naive_clock_fail_closed(tmp_path):
    missing = tmp_path / "missing.json"
    malformed = protected_json(tmp_path / "malformed.json", [])
    extra = activation_data(extra=True)
    extra_path = protected_json(tmp_path / "extra.json", extra)

    for path, now in (
        (missing, NOW),
        (malformed, NOW),
        (extra_path, NOW),
        (
            protected_json(tmp_path / "valid.json", activation_data()),
            NOW.replace(tzinfo=None),
        ),
    ):
        with pytest.raises(TrustedTransportConfigurationError):
            load_stage1_activation(path, now=now)


def test_activation_default_clock_and_non_json_control_fail_closed(
    tmp_path, monkeypatch
):
    path = protected_json(tmp_path / "activation.json", activation_data())

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 10, 18, 0, tzinfo=UTC)

    monkeypatch.setattr(smoke, "datetime", Clock)
    assert load_stage1_activation(path).model == STAGE1_MODEL

    broken = tmp_path / "broken.json"
    broken.write_bytes(b"not-json")
    broken.chmod(0o400)
    with pytest.raises(TrustedTransportConfigurationError, match="malformed"):
        load_stage1_activation(broken, now=NOW)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"version":"one","version":"two"}', encoding="utf-8")
    duplicate.chmod(0o400)
    with pytest.raises(TrustedTransportConfigurationError, match="malformed"):
        load_stage1_activation(duplicate, now=NOW)


def test_stop_defaults_active_and_only_exact_false_artifact_runs(tmp_path):
    missing = WorkerEmergencyStop(tmp_path / "missing.json")
    malformed = WorkerEmergencyStop(
        protected_json(tmp_path / "malformed.json", {"stop": False})
    )
    active = WorkerEmergencyStop(stop_file(tmp_path, stopped=True, name="active.json"))
    clear = WorkerEmergencyStop(stop_file(tmp_path, stopped=False, name="clear.json"))

    assert missing.active() is True
    assert malformed.active() is True
    assert active.active() is True
    assert clear.active() is False


def gate(tmp_path):
    activation_path = protected_json(tmp_path / "activation.json", activation_data())
    activation = load_stage1_activation(activation_path, now=NOW)
    stop = WorkerEmergencyStop(stop_file(tmp_path))
    return Stage1RequestGate(
        activation,
        stop=stop,
        activation_path=activation_path,
        utcnow=lambda: NOW,
    ), activation_path


def inference(**changes):
    values = {
        "operation_id": STAGE1_OPERATION_ID,
        "correlation_id": STAGE1_CORRELATION_ID,
        "model_request": build_stage1_synthetic_request(),
    }
    values.update(changes)
    return InferenceRequest(**values)


def test_gate_allows_only_exact_fixed_request(tmp_path):
    request_gate, _path = gate(tmp_path)

    assert request_gate.authorize(inference()) is None

    changed_request = replace(
        build_stage1_synthetic_request(),
        conversation=(
            ConversationSegment(
                ContextRole.PARTICIPANT,
                ContextTrust.UNTRUSTED_CONVERSATIONAL_DATA,
                "Другая синтетическая строка.",
            ),
        ),
    )
    for denied in (
        inference(operation_id="other-operation"),
        inference(correlation_id="other-correlation"),
        inference(model_request=changed_request),
    ):
        result = request_gate.authorize(denied)
        assert result.outcome == "authorization_rejected"
        assert result.attempts == 0


def test_gate_rechecks_stop_activation_and_expiry_before_transport(tmp_path):
    request_gate, activation_path = gate(tmp_path)
    request_gate._stop = WorkerEmergencyStop(stop_file(tmp_path, stopped=True))

    assert request_gate.authorize(inference()).outcome == "emergency_stop"
    assert request_gate.paid_transport_blocked() is True

    request_gate._stop = WorkerEmergencyStop(stop_file(tmp_path, stopped=False))
    protected_json(
        activation_path,
        activation_data(session_id="operator-smoke-session-replacement"),
    )
    assert request_gate.authorize(inference()).outcome == "authorization_rejected"
    assert request_gate.paid_transport_blocked() is True


def test_gate_fails_closed_when_activation_disappears_or_request_is_invalid(tmp_path):
    request_gate, activation_path = gate(tmp_path)
    activation_path.chmod(0o600)
    activation_path.unlink()

    assert request_gate.authorize(inference()).outcome == "authorization_rejected"
    assert request_gate.paid_transport_blocked() is True

    request_gate._activation_path = protected_json(
        tmp_path / "restored.json", activation_data()
    )
    assert (
        request_gate.authorize(
            InferenceRequest(
                STAGE1_OPERATION_ID,
                STAGE1_CORRELATION_ID,
                object(),
            )
        ).outcome
        == "authorization_rejected"
    )


def test_stage1_configuration_is_one_attempt_bounded_and_currently_priced():
    config = stage1_provider_config()
    pricing = stage1_pricing()

    assert config.paid_calls_enabled is True
    assert config.max_attempts == 1
    assert config.max_output_tokens == 512
    assert config.model == STAGE1_MODEL
    assert pricing.model == STAGE1_MODEL
    assert pricing.input_per_million.as_tuple().exponent == -2
    assert pricing.output_per_million.as_tuple().exponent == -2


def test_request_digest_is_canonical_and_content_is_synthetic():
    request = build_stage1_synthetic_request()
    assert stage1_model_request_digest(request) == stage1_model_request_digest(request)
    assert request.conversation[0].content.startswith("Синтетическая")


def test_fake_response_matches_strict_normalizer_shape():
    response = smoke._fake_response()
    assert response.status == "completed"
    assert response.usage.total_tokens == 120
    assert response.output[0].content[0].type == "output_text"


def test_stage1_attempt_is_durable_one_shot_across_worker_restart(tmp_path):
    database = tmp_path / "provider.sqlite3"
    first_transport = FakeOpenAITransport([smoke._fake_response()])
    first = TrustedOpenAIGateway(
        store=ProviderStateStore(EventStore(database)),
        transport=first_transport,
        config=stage1_provider_config(),
        pricing=stage1_pricing(),
    )

    result = first.submit(
        build_stage1_synthetic_request(),
        operation_id=STAGE1_OPERATION_ID,
        correlation_id=STAGE1_CORRELATION_ID,
    )
    assert result.outcome is GatewayOutcome.COMPLETED
    assert result.attempts == 1

    replacement_transport = FakeOpenAITransport([smoke._fake_response()])
    restarted = TrustedOpenAIGateway(
        store=ProviderStateStore(EventStore(database)),
        transport=replacement_transport,
        config=stage1_provider_config(),
        pricing=stage1_pricing(),
    )
    duplicate = restarted.submit(
        build_stage1_synthetic_request(),
        operation_id=STAGE1_OPERATION_ID,
        correlation_id=STAGE1_CORRELATION_ID,
    )

    assert duplicate.outcome is GatewayOutcome.DUPLICATE_OPERATION
    assert duplicate.attempts == 0
    assert replacement_transport.calls == []


def test_stage1_timeout_makes_one_attempt_and_never_creates_replacement(tmp_path):
    transport = FakeOpenAITransport([OpenAITransportError(timeout=True)])
    gateway = TrustedOpenAIGateway(
        store=ProviderStateStore(EventStore(tmp_path / "provider.sqlite3")),
        transport=transport,
        config=stage1_provider_config(),
    )

    result = gateway.submit(
        build_stage1_synthetic_request(),
        operation_id=STAGE1_OPERATION_ID,
        correlation_id=STAGE1_CORRELATION_ID,
    )

    assert result.outcome is GatewayOutcome.TIMEOUT
    assert result.attempts == 1
    assert len(transport.calls) == 1


@pytest.mark.parametrize("fake_transport", [True, False])
def test_stage1_main_builds_dedicated_guarded_worker(
    tmp_path, monkeypatch, fake_transport
):
    activation_path = protected_json(tmp_path / "activation.json", activation_data())
    activation = load_stage1_activation(activation_path, now=NOW)
    stop_path = stop_file(tmp_path)
    constructed = {}

    monkeypatch.setattr(smoke, "load_stage1_activation", lambda: activation)
    monkeypatch.setattr(
        smoke,
        "WorkerEmergencyStop",
        lambda: WorkerEmergencyStop(stop_path),
    )
    live_transport = FakeOpenAITransport([])
    monkeypatch.setattr(
        smoke, "build_worker_openai_transport", lambda **_kwargs: live_transport
    )

    class Server:
        def __init__(self, config, gateway, *, request_guard):
            constructed["config"] = config
            constructed["gateway"] = gateway
            constructed["guard"] = request_guard

        def start(self):
            constructed["started"] = True

        def serve(self, *, max_connections, should_stop):
            constructed["max_connections"] = max_connections
            constructed["stopped"] = should_stop()

        def close(self):
            constructed["closed"] = True

    handlers = []

    def fake_signal(_kind, handler):
        if callable(handler):
            handlers.append(handler)
            handler(0, None)
        return object()

    monkeypatch.setattr(smoke, "OpenAIWorkerServer", Server)
    monkeypatch.setattr(smoke.signal, "signal", fake_signal)
    argv = [
        "--socket",
        str(tmp_path / "worker.sock"),
        "--database",
        str(tmp_path / "provider.sqlite3"),
        "--allow-uid",
        "11002",
        "--socket-group-gid",
        str(__import__("os").getgid()),
        "--log-peer-uids",
    ]
    if fake_transport:
        argv.append("--fake-transport")

    assert smoke.main(argv) == 0
    assert constructed["config"].permitted_peer_uids == frozenset((11002,))
    assert constructed["config"].log_peer_uids is True
    assert constructed["max_connections"] == 2
    assert constructed["stopped"] is True
    assert constructed["closed"] is True
    assert len(handlers) == 2


def test_stage1_worker_module_entrypoint_requires_explicit_configuration(
    monkeypatch, capsys
):
    monkeypatch.setattr(sys, "argv", ["openai_stage1_smoke"])
    with pytest.warns(RuntimeWarning), pytest.raises(SystemExit) as caught:
        runpy.run_module("app.trusted.openai_stage1_smoke", run_name="__main__")

    assert caught.value.code == 2
    assert "--socket" in capsys.readouterr().err
