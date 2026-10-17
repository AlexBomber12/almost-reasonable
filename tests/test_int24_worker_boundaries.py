"""Public worker identity, one-use attempt and malformed result regression tests."""

import json
import os
from pathlib import Path
import struct
from threading import Thread

import pytest

import app.behavior.worker_client as client_module
import app.trusted.public_behavior_worker as worker_module
from app.behavior.worker_client import PublicBehaviorClient
from app.core.public_behavior import PublicBehaviorRequest
from app.trusted.public_behavior_worker import PublicSDKAdapter, PublicWorkerServer


REQUEST = {
    "correlation_id": "operation-1",
    "trigger": "autonomous_wakeup",
    "authorized_context": (),
    "permitted_mutable_state": (),
}
GOOD = {"decision": "no_action", "reason_category": "other"}


class SDK:
    def __init__(self, output):
        self.responses = self
        self.output = output
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return type("Response", (), {"output_text": self.output})()


@pytest.mark.parametrize(
    "output",
    [
        None,
        "x" * 4097,
        "{}",
        '{"decision":"no_action","decision":"reply","content":"Привет"}',
    ],
)
def test_sdk_adapter_rejects_malformed_model_output(output):
    adapter = PublicSDKAdapter(SDK(output), model="test-model")
    request = PublicBehaviorRequest(
        "operation-1", "operation-1", "autonomous_wakeup", (), (), 5
    )
    with pytest.raises(ValueError):
        adapter.complete(request)


def test_sdk_adapter_requires_explicit_client_and_model():
    with pytest.raises(ValueError):
        PublicSDKAdapter(object(), model="")


class Socket:
    def __init__(self, uid):
        self.uid = uid

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def settimeout(self, _value):
        return None

    def connect(self, _path):
        return None

    def getsockopt(self, *_args):
        return struct.pack("iII", 1, self.uid, 1)

    def shutdown(self, _direction):
        return None


def test_client_rejects_wrong_worker_uid_and_malformed_results(monkeypatch):
    with pytest.raises(ValueError):
        PublicBehaviorClient(Path("relative"), expected_uid=os.getuid())
    client = PublicBehaviorClient(
        Path("/tmp/nonexistent.sock"), expected_uid=os.getuid()
    )
    monkeypatch.setattr(
        client_module.socket, "socket", lambda *_args: Socket(os.getuid() + 1)
    )
    with pytest.raises(ValueError, match="peer"):
        client.complete(REQUEST, 5)
    monkeypatch.setattr(
        client_module.socket, "socket", lambda *_args: Socket(os.getuid())
    )
    monkeypatch.setattr(client_module, "send_frame", lambda *_args, **_kwargs: None)
    for raw in (b"bad", b"{}", b'{"operation_id":"operation-1","proposal":{}}'):
        monkeypatch.setattr(
            client_module, "receive_frame", lambda *_args, raw=raw, **_kwargs: raw
        )
        with pytest.raises(ValueError):
            client.complete(REQUEST, 5)


def start_server(
    tmp_path, *, attest=lambda _request: True, uid=None, gid=None, output=None
):
    sdk = SDK(json.dumps(GOOD) if output is None else output)
    server = PublicWorkerServer(
        tmp_path / "worker.sock",
        tmp_path / "attempts.sqlite",
        permitted_uid=os.getuid() if uid is None else uid,
        socket_gid=gid,
        attest=attest,
        adapter=PublicSDKAdapter(sdk, model="test-model"),
    )
    server.start()
    return server, sdk


def exchange(server, *, expected_uid=None):
    thread = Thread(target=server.serve_once)
    thread.start()
    client = PublicBehaviorClient(
        server.socket_path,
        expected_uid=os.getuid() if expected_uid is None else expected_uid,
    )
    try:
        return client.complete(REQUEST, 5)
    finally:
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_worker_reserves_once_before_sdk_and_denies_replay(tmp_path):
    server, sdk = start_server(tmp_path)
    try:
        assert exchange(server) == GOOD
        with pytest.raises(Exception):
            exchange(server)
        assert sdk.calls == 1
    finally:
        server.close()


def test_worker_rejects_unattested_and_wrong_peer_without_sdk(tmp_path):
    server, sdk = start_server(tmp_path, attest=lambda _request: False)
    try:
        with pytest.raises(Exception):
            exchange(server)
        assert sdk.calls == 0
    finally:
        server.close()
    second = tmp_path / "second"
    second.mkdir(mode=0o700)
    server, sdk = start_server(second, uid=os.getuid() + 1)
    try:
        with pytest.raises(Exception):
            exchange(server)
        assert sdk.calls == 0
    finally:
        server.close()


def test_worker_rejects_oversized_result_and_start_conflicts(tmp_path):
    server, sdk = start_server(
        tmp_path,
        gid=os.getgid(),
        output=json.dumps({"decision": "reply", "content": "х" * 4000}),
    )
    try:
        with pytest.raises(Exception):
            exchange(server)
        assert sdk.calls == 1
        with pytest.raises(ValueError):
            server.start()
    finally:
        server.close()
    assert not server.socket_path.exists()


def test_worker_configuration_and_lifecycle_guards(tmp_path):
    with pytest.raises(ValueError):
        PublicWorkerServer(
            Path("relative"),
            tmp_path / "attempts.sqlite",
            permitted_uid=0,
            attest=lambda _: True,
            adapter=object(),
        )
    with pytest.raises(ValueError):
        PublicWorkerServer(
            tmp_path / "worker.sock",
            tmp_path / "attempts.sqlite",
            permitted_uid=0,
            attest=lambda _: True,
            adapter=object(),
            socket_gid=-1,
        )
    server, _ = start_server(tmp_path)
    try:
        (tmp_path / "worker.sock").unlink()
    finally:
        server.close()
    server, _ = start_server(tmp_path)
    try:
        path = server.socket_path
        path.unlink()
        path.write_text("replacement")
    finally:
        server.close()
    assert path.read_text() == "replacement"
    path.unlink()
    server, _ = start_server(tmp_path)
    server.close()
    with pytest.raises(ValueError):
        server.serve_once()


def test_worker_bounds_serialized_result_and_idempotent_close(tmp_path):
    class HugeAdapter:
        def complete(self, _request):
            return {"decision": "reply", "content": "х" * 4000}

    server = PublicWorkerServer(
        tmp_path / "worker.sock",
        tmp_path / "attempts.sqlite",
        permitted_uid=os.getuid(),
        attest=lambda _: True,
        adapter=HugeAdapter(),
    )
    server.start()
    try:
        with pytest.raises(Exception):
            exchange(server)
    finally:
        server.close()
        server.close()


def test_worker_attempt_database_requires_protected_storage(tmp_path):
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        PublicWorkerServer(
            tmp_path / "worker.sock",
            insecure / "attempts.sqlite",
            permitted_uid=os.getuid(),
            attest=lambda _: True,
            adapter=object(),
        )
    attempts = tmp_path / "attempts.sqlite"
    attempts.write_text("unsafe")
    attempts.chmod(0o644)
    with pytest.raises(ValueError):
        PublicWorkerServer(
            tmp_path / "worker.sock",
            attempts,
            permitted_uid=os.getuid(),
            attest=lambda _: True,
            adapter=object(),
        )


@pytest.mark.parametrize("failing_setup", ["chown", "chmod"])
def test_failed_socket_setup_removes_bound_path_and_allows_restart(
    tmp_path, monkeypatch, failing_setup
):
    server = PublicWorkerServer(
        tmp_path / "worker.sock",
        tmp_path / "attempts.sqlite",
        permitted_uid=os.getuid(),
        socket_gid=os.getgid(),
        attest=lambda _: True,
        adapter=PublicSDKAdapter(SDK(json.dumps(GOOD)), model="test-model"),
    )

    def fail_setup(*_args):
        raise OSError("simulated socket setup failure")

    with monkeypatch.context() as patch:
        patch.setattr(worker_module.os, failing_setup, fail_setup)
        with pytest.raises(OSError, match="setup failure"):
            server.start()
    assert not server.socket_path.exists()
    assert server.listener is None
    server.start()
    server.close()
