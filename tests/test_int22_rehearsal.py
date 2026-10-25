"""Actual HTTP, AF_UNIX worker, trusted gateway and fake SDK rehearsal."""

import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
from threading import Thread
from types import SimpleNamespace

import pytest

from acceptance.private_pilot_conversation import run_ship_journey
from acceptance.guards import install
from app.core.openai_worker_client import OpenAIWorkerClient
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.private_web.privacy import ParticipantPrivacyControls
from app.private_web.runtime import WorkerConversationRuntime
from app.privacy.application import PrivacyReadGate
from test_private_web_http import Clock
from app.trusted.openai_transport import OpenAIResponsesTransport
from app.trusted.private_pilot import PilotAllowance, load_activation
from app.trusted import private_pilot_worker
from app.privacy.application import EventStoreErasureAdapter
from app.privacy.deletion import DeletionCoordinator
from test_privacy_deletion import (
    context as deletion_context,
    request as deletion_request,
)
from test_int19_worker_runtime import _app
from test_int22_pilot import activation_file, _rewrite
from test_openai_transport import documented_response


@pytest.fixture(autouse=True)
def offline_network_guard(monkeypatch):
    install(monkeypatch, local_ipc=True)


def _worker(
    socket_path,
    provider_path,
    allowance_path,
    activation_path,
    stop_path,
    ready,
    count,
    raw=None,
    entered=None,
    release=None,
    exit_on_sdk=False,
):
    class Responses:
        calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if exit_on_sdk:
                os._exit(23)
            if entered is not None:
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("synthetic synchronization timed out")
            if isinstance(raw, tuple):
                return raw[len(self.calls) - 1]
            return documented_response() if raw is None else raw

    responses = Responses()
    private_pilot_worker.build_worker_openai_transport = (
        lambda **_kwargs: OpenAIResponsesTransport(SimpleNamespace(responses=responses))
    )
    server = private_pilot_worker.build_worker(
        socket_path=Path(socket_path),
        provider_database=Path(provider_path),
        allowance_database=Path(allowance_path),
        activation_path=Path(activation_path),
        stop_path=Path(stop_path),
    )
    try:
        server.start()
        ready.send(True)
        server.serve(max_connections=count)
        ready.send(responses.calls)
    finally:
        server.close()
        ready.close()


def _start(paths, count, *, raw=None, entered=None, release=None, exit_on_sdk=False):
    if not Path(paths[1]).exists():
        EventStore(Path(paths[1]))
    parent, child = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_worker,
        args=(*paths, child, count, raw, entered, release, exit_on_sdk),
    )
    process.start()
    child.close()
    assert parent.poll(5) and parent.recv() is True
    return process, parent


def _finish(process, pipe):
    assert pipe.poll(10)
    calls = pipe.recv()
    process.join(timeout=5)
    assert process.exitcode == 0
    return calls


def _reply(text):
    payload = documented_response()
    payload["output"][1]["content"][0]["text"] = json.dumps(
        {"proposal": {"decision": "reply", "content": text}}, ensure_ascii=False
    )
    return payload


def test_positive_ship_journey_keeps_worker_during_application_restart(tmp_path):
    activation, _ = activation_file(tmp_path)
    stop = activation.parent / "stop.json"
    stop.write_text('{"version":"private-pilot-emergency-stop-v1","stop":false}')
    stop.chmod(0o400)
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    allowance = worker_dir / "allowance.sqlite3"
    PilotAllowance.initialize(allowance, load_activation(activation))
    socket = worker_dir / "worker.sock"
    provider = worker_dir / "provider.sqlite3"
    auth, events, ledger, _gate, app, alice, _bob = _app(tmp_path, socket)
    responses = (
        _reply("Искра"),
        _reply("Корабль: Искра; Назначение: Юпитер"),
        _reply("Корабль: Искра; Назначение: Марс"),
    )
    process, pipe = _start(
        (str(socket), str(provider), str(allowance), str(activation), str(stop)),
        3,
        raw=responses,
    )
    calls = []

    def restart_app():
        reopened = EventStore(events.database_path)
        gate = PrivacyReadGate(ledger, reopened)
        assert gate.reconcile()
        app._conversations = WorkerConversationRuntime(
            reopened,
            gate,
            OpenAIWorkerClient(socket, expected_worker_uid=os.getuid()),
            provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
            clock=Clock(),
        )
        app._privacy = ParticipantPrivacyControls(
            auth, reopened, ledger, gate, clock=Clock()
        )

    def send(message_id, content):
        return json.loads(alice.submit(message_id, content)[2])

    def history():
        return json.loads(alice.request("GET", "/api/history")[2])["messages"]

    def inspect_request():
        calls.extend(_finish(process, pipe))
        return json.loads(calls[-1]["input"])

    result = run_ship_journey(send, history, restart_app, inspect_request)
    assert result["ship_name"] == "Искра"
    assert len(calls) == 3
    assert PilotAllowance(allowance, load_activation(activation)).remaining() == 2
    with EventStore(provider)._open() as db:
        assert db.execute(
            "SELECT count(*) FROM provider_usage_attempts"
        ).fetchone() == (3,)


def test_authenticated_http_to_fake_sdk_reply_history_restart_and_quota(tmp_path):
    path, _ = activation_file(tmp_path)
    stop = path.parent / "emergency-stop.json"
    stop.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": False})
    )
    stop.chmod(0o400)
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    allowance_path = worker_dir / "allowance.sqlite3"
    PilotAllowance.initialize(allowance_path, load_activation(path))
    socket_path = worker_dir / "worker.sock"
    provider_path = worker_dir / "provider.sqlite3"
    _auth, _events, _ledger, _gate, _app_instance, alice, bob = _app(
        tmp_path, socket_path
    )
    paths = (
        str(socket_path),
        str(provider_path),
        str(allowance_path),
        str(path),
        str(stop),
    )

    first, first_pipe = _start(paths, 2)
    a = json.loads(alice.submit("a1", "Синтетический вопрос Алисы")[2])
    b = json.loads(bob.submit("b1", "Синтетический вопрос Боба")[2])
    assert a["model_state"] == b["model_state"] == "completed"
    assert a["response"] == b["response"] == "Здравствуйте"
    calls = _finish(first, first_pipe)
    assert len(calls) == 2
    assert all(call["tools"] == [] and call["store"] is False for call in calls)
    assert "Синтетический вопрос Боба" not in calls[0]["input"]
    assert "Синтетический вопрос Алисы" not in calls[1]["input"]

    second, second_pipe = _start(paths, 4)
    duplicate = json.loads(alice.submit("a1", "Синтетический вопрос Алисы")[2])
    assert duplicate["model_state"] == "completed"
    for index in range(2, 5):
        outcome = json.loads(alice.submit(f"a{index}", "Продолжим разговор")[2])
        assert outcome["model_state"] == "completed"
    exhausted = json.loads(alice.submit("a5", "Ещё вопрос")[2])
    assert exhausted["model_state"] != "completed"
    assert len(_finish(second, second_pipe)) == 3
    assert PilotAllowance(allowance_path, load_activation(path)).remaining() == 0
    history = json.loads(alice.request("GET", "/api/history")[2])
    assert sum(item["role"] == "agent" for item in history["messages"]) == 4


@pytest.mark.parametrize(
    "change",
    [
        "activation_ancestor",
        "stop_ancestor",
        "allowance_ancestor",
        "provider_ancestor",
        "provider_swap",
    ],
)
def test_unsafe_or_redirected_worker_path_denies_sdk_contact(tmp_path, change):
    control_root = tmp_path / "controls"
    control_root.mkdir(mode=0o700)
    path, _ = activation_file(control_root)
    stop_root = tmp_path / "stop-control"
    stop_root.mkdir(mode=0o700)
    stop = stop_root / "stop.json"
    stop.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": False})
    )
    stop.chmod(0o400)
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    allowance_dir = worker_dir / "allowance"
    allowance_dir.mkdir(mode=0o700)
    provider_dir = worker_dir / "provider"
    provider_dir.mkdir(mode=0o700)
    allowance = allowance_dir / "allowance.sqlite3"
    PilotAllowance.initialize(allowance, load_activation(path))
    provider = provider_dir / "provider.sqlite3"
    socket = worker_dir / "worker.sock"
    _auth, _events, _ledger, _gate, _app_instance, alice, _bob = _app(tmp_path, socket)
    paths = (str(socket), str(provider), str(allowance), str(path), str(stop))
    process, pipe = _start(paths, 1)
    if change == "activation_ancestor":
        control_root.chmod(0o777)
    elif change == "stop_ancestor":
        stop_root.chmod(0o777)
    elif change == "allowance_ancestor":
        allowance_dir.chmod(0o777)
    elif change == "provider_ancestor":
        provider_dir.chmod(0o777)
    else:
        provider.rename(provider_dir / "saved.sqlite3")
        EventStore(provider)
    result = json.loads(alice.submit("one", "Синтетический вопрос")[2])
    assert result["model_state"] != "completed"
    assert _finish(process, pipe) == []


@pytest.mark.parametrize("failure", ["wrong_peer", "malformed_sdk"])
def test_worker_rejects_wrong_peer_and_malformed_sdk_response(tmp_path, failure):
    path, value = activation_file(tmp_path)
    if failure == "wrong_peer":
        value["application_peer_uid"] = os.getuid() + 1
        _rewrite(path, value)
    stop = path.parent / "stop.json"
    stop.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": False})
    )
    stop.chmod(0o400)
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    allowance = worker_dir / "allowance.sqlite3"
    PilotAllowance.initialize(allowance, load_activation(path))
    socket = worker_dir / "worker.sock"
    _auth, _events, _ledger, _gate, _app_instance, alice, _bob = _app(tmp_path, socket)
    paths = (
        str(socket),
        str(worker_dir / "provider.sqlite3"),
        str(allowance),
        str(path),
        str(stop),
    )
    raw = (
        {"id": "resp_synthetic", "status": "completed", "output": {}}
        if failure == "malformed_sdk"
        else None
    )
    process, pipe = _start(paths, 1, raw=raw)
    result = json.loads(alice.submit("one", "Синтетический вопрос")[2])
    assert result["model_state"] != "completed"
    calls = _finish(process, pipe)
    assert len(calls) == (0 if failure == "wrong_peer" else 1)
    assert PilotAllowance(allowance, load_activation(path)).remaining() == (
        5 if failure == "wrong_peer" else 4
    )


def test_sdk_process_exit_loses_response_but_consumes_allowance(tmp_path):
    path, _ = activation_file(tmp_path)
    stop = path.parent / "stop.json"
    stop.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": False})
    )
    stop.chmod(0o400)
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    allowance = worker_dir / "allowance.sqlite3"
    PilotAllowance.initialize(allowance, load_activation(path))
    socket = worker_dir / "worker.sock"
    _auth, _events, _ledger, _gate, _app_instance, alice, _bob = _app(tmp_path, socket)
    paths = (
        str(socket),
        str(worker_dir / "provider.sqlite3"),
        str(allowance),
        str(path),
        str(stop),
    )
    process, pipe = _start(paths, 1, exit_on_sdk=True)
    lost = json.loads(alice.submit("lost", "Вопрос с потерянным ответом")[2])
    process.join(5)
    pipe.close()
    assert process.exitcode == 23
    assert lost["model_state"] == "uncertain"
    assert PilotAllowance(allowance, load_activation(path)).remaining() == 4
    socket.unlink()
    restarted, restarted_pipe = _start(paths, 1)
    replay = json.loads(alice.submit("lost", "Вопрос с потерянным ответом")[2])
    assert replay["model_state"] == "uncertain"
    assert PilotAllowance(allowance, load_activation(path)).remaining() == 4
    fresh = json.loads(alice.submit("fresh", "Другой синтетический вопрос")[2])
    assert fresh["model_state"] == "completed"
    assert len(_finish(restarted, restarted_pipe)) == 1
    assert PilotAllowance(allowance, load_activation(path)).remaining() == 3


@pytest.mark.parametrize("change", ["session", "source", "stop", "spend"])
def test_inflight_controls_block_release_after_fake_sdk_contact(tmp_path, change):
    path, _ = activation_file(tmp_path)
    stop = path.parent / "stop.json"
    stop.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": False})
    )
    stop.chmod(0o400)
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    allowance = worker_dir / "allowance.sqlite3"
    PilotAllowance.initialize(allowance, load_activation(path))
    socket = worker_dir / "worker.sock"
    auth, events, ledger, gate, app, alice, _bob = _app(tmp_path, socket)
    entered, release = multiprocessing.Event(), multiprocessing.Event()
    paths = (
        str(socket),
        str(worker_dir / "provider.sqlite3"),
        str(allowance),
        str(path),
        str(stop),
    )
    process, pipe = _start(paths, 1, entered=entered, release=release)
    results = []
    thread = Thread(
        target=lambda: results.append(
            alice.submit("one", "Удаляемый синтетический вопрос")
        )
    )
    thread.start()
    try:
        assert entered.wait(5)
        if change == "session":
            assert auth.revoke(alice.cookies["ar_session"])
        elif change == "source":
            principal = auth.authenticate(alice.cookies["ar_session"]).trusted_context
            source = gate.retrieve_private_events(principal)[-1].event_id
            assert (
                DeletionCoordinator(ledger, EventStoreErasureAdapter(events))
                .delete(deletion_context(), deletion_request(content_reference=source))
                .disposition.value
                == "applied"
            )
        elif change == "stop":
            app._conversations._stop = lambda: True
        else:
            with sqlite3.connect(tmp_path / "ipc-state.db") as connection:
                connection.execute(
                    "INSERT INTO provider_spend_suspensions VALUES (?, ?, ?, ?, ?)",
                    (
                        "openai",
                        "2030-01-01T00:00:00.000000Z",
                        "project_spend_limit_exceeded",
                        "openai-private-pilot-v1",
                        "synthetic",
                    ),
                )
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert results
    assert (
        results[0][0] != "201 Created"
        or json.loads(results[0][2])["model_state"] != "completed"
    )
    assert len(_finish(process, pipe)) == 1
    assert PilotAllowance(allowance, load_activation(path)).remaining() == 4
    if change == "session":
        assert alice.login("alice", "synthetic-password-a")[0] == "303 See Other"
    if change == "stop":
        app._conversations._stop = lambda: False
    assert gate.reconcile()
    if change != "spend":
        history = json.loads(alice.request("GET", "/api/history")[2])
        assert not any(item["role"] == "agent" for item in history["messages"])


def test_contradictory_mars_text_does_not_pass_destination_oracle():
    replies = iter(
        (
            "Искра",
            "Корабль: Искра; Назначение: Юпитер",
            "Корабль Искра всё ещё летит к Юпитеру, а не к Марсу. Марс не выбран.",
        )
    )

    def send(_message_id, _content):
        return {"model_state": "completed", "response": next(replies)}

    with pytest.raises(AssertionError, match="Mars"):
        run_ship_journey(send, lambda: [], lambda: None)


@pytest.mark.parametrize("invalid", ["history", "transcript"])
def test_journey_validation_survives_optimized_python(invalid):
    import subprocess
    import sys

    script = f"""
from acceptance.private_pilot_conversation import FIRST, ASK, MARS, run_ship_journey
replies = iter(('Искра', 'Корабль: Искра; Назначение: Юпитер', 'Корабль: Искра; Назначение: Марс'))
def send(_id, _content):
    return {{'model_state': 'completed', 'response': next(replies)}}
def history():
    return []
def inspect():
    return {{'conversation': []}}
try:
    run_ship_journey(send, history, lambda: None, {"inspect" if invalid == "transcript" else "None"})
except AssertionError:
    raise SystemExit(0)
raise SystemExit(1)
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
