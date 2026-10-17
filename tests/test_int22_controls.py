"""Operator controls, worker admission and explicit entry points."""

import json
import runpy
import signal
import sys
from types import SimpleNamespace

import pytest

from app.cognition.conversation_context import MemoryModelRequest
from app.core.store import EventStore
from app.ops import private_pilot_ops
from app.trusted import private_pilot_worker
from app.trusted.openai_worker_protocol import InferenceRequest
from app.trusted.openai_transport import OpenAIResponsesTransport
from app.trusted.private_pilot import (
    PilotAllowance,
    PilotAuthorityError,
    load_activation,
)
from test_int22_pilot import activation_file, _rewrite
from test_openai_worker_protocol import model_request


def _stop(path, active):
    if path.exists():
        path.chmod(0o600)
    path.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": active})
    )
    path.chmod(0o400)


def test_gate_rechecks_stop_activation_typed_context_and_allowance(tmp_path):
    path, value = activation_file(tmp_path)
    stop_path = path.parent / "stop.json"
    _stop(stop_path, True)
    activation = load_activation(path)
    allowance = PilotAllowance.initialize(tmp_path / "allowance.sqlite3", activation)
    gate = private_pilot_worker.PilotRequestGate(
        activation,
        allowance,
        activation_path=path,
        stop=private_pilot_worker.PilotStop(stop_path),
    )
    typed = MemoryModelRequest(model_request(), (), False)
    request = InferenceRequest("reply:" + "a" * 64, "synthetic-correlation", typed)
    assert gate.authorize(request).outcome == "emergency_stop"
    assert gate.paid_transport_blocked()
    _stop(stop_path, False)
    assert (
        gate.authorize(
            InferenceRequest(
                request.operation_id, request.correlation_id, model_request()
            )
        ).outcome
        == "authorization_rejected"
    )
    assert gate.authorize(request) is None
    assert allowance.remaining() == 4
    assert gate.authorize(request).outcome == "authorization_rejected"
    assert not gate.paid_transport_blocked()
    value["pilot_id"] = "changed"
    _rewrite(path, value)
    assert (
        gate.authorize(InferenceRequest("reply:" + "b" * 64, "c", typed)).outcome
        == "authorization_rejected"
    )
    assert gate.paid_transport_blocked()
    path.unlink()
    assert not gate.current()


def test_gate_rejects_invalid_operation_without_consumption(tmp_path):
    path, _ = activation_file(tmp_path)
    stop = path.parent / "stop.json"
    _stop(stop, False)
    activation = load_activation(path)
    allowance = PilotAllowance.initialize(tmp_path / "allowance.sqlite3", activation)
    gate = private_pilot_worker.PilotRequestGate(
        activation,
        allowance,
        activation_path=path,
        stop=private_pilot_worker.PilotStop(stop),
    )
    request = InferenceRequest(
        "bad-operation", "c", MemoryModelRequest(model_request(), (), False)
    )
    assert gate.authorize(request).outcome == "authorization_rejected"
    assert allowance.remaining() == 5


def test_operator_prepare_inspect_and_cli_keep_stop(tmp_path, capsys, monkeypatch):
    path, _ = activation_file(tmp_path)
    allowance = tmp_path / "allowance.sqlite3"
    provider = tmp_path / "provider.sqlite3"
    stop = path.parent / "stop.json"
    _stop(stop, True)
    assert (
        private_pilot_ops.main(
            ["prepare", "--activation", str(path), "--allowance", str(allowance)]
        )
        == 0
    )
    assert "stop unchanged" in capsys.readouterr().out
    with pytest.raises(PilotAuthorityError):
        private_pilot_ops.inspect(path, allowance, stop, provider)
    EventStore(provider)
    state = private_pilot_ops.inspect(path, allowance, stop, provider)
    assert state["remaining_operations"] == 5
    assert state["worker_stopped"] is True
    assert state["ready"] is False
    _stop(stop, False)
    assert private_pilot_ops.inspect(path, allowance, stop, provider)["ready"] is True
    monkeypatch.setattr(
        private_pilot_ops.ProviderStateStore, "is_spend_suspended", lambda _self: True
    )
    assert private_pilot_ops.inspect(path, allowance, stop, provider)["ready"] is False
    assert (
        private_pilot_ops.main(
            [
                "inspect",
                "--activation",
                str(path),
                "--allowance",
                str(allowance),
                "--stop",
                str(stop),
                "--provider-database",
                str(provider),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["worker_spend_suspended"] is True
    with pytest.raises(SystemExit) as error:
        private_pilot_ops.main(
            ["prepare", "--activation", str(path), "--allowance", str(allowance)]
        )
    assert error.value.code == 1


def test_worker_main_installs_signal_handlers_and_closes(monkeypatch, tmp_path):
    events = []
    handlers = {}

    class Server:
        def start(self):
            events.append("start")

        def serve(self, *, should_stop):
            assert not should_stop()
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            assert should_stop()
            events.append("serve")

        def close(self):
            events.append("close")

    monkeypatch.setattr(
        private_pilot_worker, "build_worker", lambda **_kwargs: Server()
    )
    monkeypatch.setattr(
        private_pilot_worker.signal,
        "signal",
        lambda number, callback: handlers.setdefault(number, callback),
    )
    assert (
        private_pilot_worker.main(
            [
                "--socket",
                str(tmp_path / "socket"),
                "--provider-database",
                str(tmp_path / "provider"),
                "--allowance-database",
                str(tmp_path / "allowance"),
                "--socket-group-gid",
                "123",
            ]
        )
        == 0
    )
    assert events == ["start", "serve", "close"]


def test_production_worker_composition_uses_bound_scope_and_one_attempt(
    monkeypatch, tmp_path
):
    path, _ = activation_file(tmp_path)
    allowance = tmp_path / "allowance.sqlite3"
    PilotAllowance.initialize(allowance, load_activation(path))
    seen = []

    def synthetic_sdk_adapter(**scope):
        seen.append(scope)
        return OpenAIResponsesTransport(SimpleNamespace(responses=object()))

    monkeypatch.setattr(
        private_pilot_worker, "build_worker_openai_transport", synthetic_sdk_adapter
    )
    EventStore(tmp_path / "provider.sqlite3")
    worker = private_pilot_worker.build_worker(
        socket_path=tmp_path / "socket",
        provider_database=tmp_path / "provider.sqlite3",
        allowance_database=allowance,
        activation_path=path,
        stop_path=path.parent / "stop.json",
    )
    assert seen == [
        {"project_id": "proj_synthetic", "organization_id": "org-synthetic"}
    ]
    assert worker._gateway._config.max_attempts == 1
    assert worker._config.permitted_peer_uids == frozenset(
        {load_activation(path).application_peer_uid}
    )


def test_worker_provider_spend_state_cannot_disappear_or_be_unsafe(tmp_path):
    database = tmp_path / "provider.sqlite3"
    with pytest.raises(PilotAuthorityError):
        private_pilot_worker._require_provider_database(database)
    EventStore(database)
    private_pilot_worker._require_provider_database(database)
    database.chmod(0o644)
    with pytest.raises(PilotAuthorityError):
        private_pilot_worker._require_provider_database(database)


def test_module_entry_points_are_explicit(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["private_pilot_ops", "--help"])
    with pytest.raises(SystemExit) as left:
        runpy.run_module("app.ops.private_pilot_ops", run_name="__main__")
    monkeypatch.setattr(sys, "argv", ["private_pilot_worker", "--help"])
    with pytest.raises(SystemExit) as right:
        runpy.run_module("app.trusted.private_pilot_worker", run_name="__main__")
    assert left.value.code == right.value.code == 0
