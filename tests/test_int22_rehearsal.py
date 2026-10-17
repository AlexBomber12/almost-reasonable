"""Actual HTTP, AF_UNIX worker, trusted gateway and fake SDK rehearsal."""

import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
from threading import Thread
from types import SimpleNamespace

import pytest

from acceptance.guards import install
from app.core.store import EventStore
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
