from dataclasses import replace
import os
from pathlib import Path
import socket
import stat
import struct
from threading import Thread
import time

import pytest

from app.core.model_contract import build_model_request
from app.core.provider_contract import DeploymentStage, default_openai_provider_config
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.trusted.openai_gateway import GatewayOutcome, TrustedOpenAIGateway
from app.trusted.openai_transport import (
    FakeOpenAITransport,
    ProviderContent,
    ProviderOutput,
    ProviderResponse,
)
from app.trusted.openai_worker_protocol import (
    IPC_REJECTED,
    MAX_IPC_FRAME_BYTES,
    InferenceRequest,
    WorkerProtocolError,
    decode_response,
    encode_request,
    receive_frame,
)
from app.trusted.openai_worker_server import (
    SOCKET_MODE,
    OpenAIWorkerServer,
    WorkerServerConfig,
    _peer_uid,
    build_offline_worker,
)
from app.trusted import openai_worker_server as server_module
from tests.test_secretless_boundary import model_context, principal


def secure_directory(tmp_path):
    directory = tmp_path / "worker"
    directory.mkdir(mode=0o700)
    return directory


def request(tmp_path):
    store = EventStore(tmp_path / "conversation.sqlite3")
    return build_model_request(model_context(store, principal("a")))


def gateway(
    tmp_path, outcomes=(), *, stage=DeploymentStage.STAGE_0, stop=lambda: False
):
    transport = FakeOpenAITransport(outcomes)
    config = default_openai_provider_config()
    if stage is DeploymentStage.STAGE_1:
        config = replace(config, deployment_stage=stage, paid_calls_enabled=True)
    instance = TrustedOpenAIGateway(
        store=ProviderStateStore(EventStore(tmp_path / "provider.sqlite3")),
        transport=transport,
        config=config,
        emergency_stop=stop,
        sleeper=lambda _delay: None,
        jitter=lambda _attempt, _maximum: 0.0,
    )
    return instance, transport


def response(proposal=None):
    if proposal is None:
        proposal = (
            '{"decision":"no_action","reason_category":"not_warranted",'
            '"content":null,"brief":null,"draft":null}'
        )
    return ProviderResponse(
        response_id="resp_synthetic",
        status="completed",
        output=(
            ProviderOutput("reasoning", ()),
            ProviderOutput("message", (ProviderContent("output_text", text=proposal),)),
        ),
        usage=None,
    )


def run_once(server):
    thread = Thread(target=server.serve_once)
    thread.start()
    return thread


def raw_exchange(server, wire):
    thread = run_once(server)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(server._config.socket_path))
    client.sendall(wire)
    client.shutdown(socket.SHUT_WR)
    try:
        payload = receive_frame(client, deadline=time.monotonic() + 2, io_timeout=1)
    finally:
        client.close()
        thread.join(timeout=2)
    assert thread.is_alive() is False
    return decode_response(payload)


def test_server_accepts_authenticated_peer_and_returns_stage_zero_result(tmp_path):
    directory = secure_directory(tmp_path)
    socket_path = directory / "worker.sock"
    instance, transport = gateway(directory)
    server = OpenAIWorkerServer(
        WorkerServerConfig(socket_path, frozenset((os.getuid(),))), instance
    )
    server.start()
    try:
        payload = encode_request(
            InferenceRequest("operation-a", "correlation-a", request(directory))
        )
        result = raw_exchange(server, struct.pack("!I", len(payload)) + payload)

        assert result.outcome == GatewayOutcome.DISABLED.value
        assert result.attempts == 0
        assert result.validation is None
        assert transport.calls == []
        assert server._listener.family == socket.AF_UNIX
        mode = stat.S_IMODE(socket_path.stat().st_mode)
        assert mode == SOCKET_MODE
        assert stat.S_ISSOCK(socket_path.stat().st_mode)
    finally:
        server.close()
    assert not socket_path.exists()


def test_server_returns_validated_untrusted_proposal_without_executing_it(tmp_path):
    directory = secure_directory(tmp_path)
    instance, transport = gateway(
        directory,
        [
            response(
                '{"decision":"reply","reason_category":null,'
                '"content":"Черновик ответа.","brief":null,"draft":null}'
            )
        ],
        stage=DeploymentStage.STAGE_1,
    )
    server = OpenAIWorkerServer(
        WorkerServerConfig(directory / "worker.sock", frozenset((os.getuid(),))),
        instance,
    )
    server.start()
    try:
        payload = encode_request(
            InferenceRequest("operation-a", "correlation-a", request(directory))
        )
        result = raw_exchange(server, struct.pack("!I", len(payload)) + payload)
    finally:
        server.close()

    assert result.outcome == "completed"
    assert result.validation.valid is True
    assert result.validation.proposal.content == "Черновик ответа."
    assert len(transport.calls) == 1


def test_invalid_provider_proposal_returns_sanitized_outcome_without_validation(
    tmp_path,
):
    directory = secure_directory(tmp_path)
    instance, transport = gateway(
        directory,
        [
            response(
                '{"decision":"reply","reason_category":null,'
                '"content":7,"brief":null,"draft":null}'
            )
        ],
        stage=DeploymentStage.STAGE_1,
    )
    server = OpenAIWorkerServer(
        WorkerServerConfig(directory / "worker.sock", frozenset((os.getuid(),))),
        instance,
    )
    server.start()
    try:
        payload = encode_request(
            InferenceRequest("operation-a", "correlation-a", request(directory))
        )
        result = raw_exchange(server, struct.pack("!I", len(payload)) + payload)
    finally:
        server.close()

    assert result.outcome == "invalid_proposal"
    assert result.validation is None
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(protocol_version="v2"),
        lambda data: data.update(operation_type="shell"),
        lambda data: data.update(headers={"Authorization": "CANARY"}),
        lambda data: data["model_request"]["core_self"].update(identity="forged"),
    ],
)
def test_server_rejects_protocol_privilege_and_model_forgery_without_provider_call(
    tmp_path, mutate
):
    directory = secure_directory(tmp_path)
    instance, transport = gateway(
        directory, [response()], stage=DeploymentStage.STAGE_1
    )
    server = OpenAIWorkerServer(
        WorkerServerConfig(directory / "worker.sock", frozenset((os.getuid(),))),
        instance,
    )
    server.start()
    try:
        data = __import__("json").loads(
            encode_request(
                InferenceRequest("operation-a", "correlation-a", request(directory))
            )
        )
        mutate(data)
        payload = __import__("json").dumps(data).encode()
        result = raw_exchange(server, struct.pack("!I", len(payload)) + payload)
    finally:
        server.close()

    assert result.outcome == IPC_REJECTED
    assert "CANARY" not in (result.safe_russian_message or "")
    assert transport.calls == []


@pytest.mark.parametrize(
    "wire",
    [
        struct.pack("!I", 1) + b"\xff",
        struct.pack("!I", 1) + b"{",
        struct.pack("!I", 10) + b"{}",
        struct.pack("!I", MAX_IPC_FRAME_BYTES + 1),
    ],
)
def test_server_rejects_malformed_utf8_json_truncation_and_oversize(tmp_path, wire):
    directory = secure_directory(tmp_path)
    instance, transport = gateway(
        directory, [response()], stage=DeploymentStage.STAGE_1
    )
    server = OpenAIWorkerServer(
        WorkerServerConfig(directory / "worker.sock", frozenset((os.getuid(),))),
        instance,
    )
    server.start()
    try:
        result = raw_exchange(server, wire)
    finally:
        server.close()
    assert result.outcome == IPC_REJECTED
    assert transport.calls == []


def test_unauthorized_peer_is_closed_before_request_processing(tmp_path, monkeypatch):
    directory = secure_directory(tmp_path)
    instance, transport = gateway(
        directory, [response()], stage=DeploymentStage.STAGE_1
    )
    server = OpenAIWorkerServer(
        WorkerServerConfig(directory / "worker.sock", frozenset((os.getuid(),))),
        instance,
    )
    monkeypatch.setattr(
        "app.trusted.openai_worker_server._peer_uid",
        lambda _connection: os.getuid() + 1,
    )
    server.start()
    try:
        thread = run_once(server)
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.connect(str(directory / "worker.sock"))
        peer.sendall(struct.pack("!I", 2) + b"{}")
        peer.shutdown(socket.SHUT_WR)
        try:
            closed = peer.recv(1)
        except ConnectionResetError:
            closed = b""
        assert closed == b""
        peer.close()
        thread.join(timeout=2)
    finally:
        server.close()
    assert transport.calls == []


class BrokenPeerSocket:
    def getsockopt(self, *_args):
        raise OSError("private detail CANARY")


def test_peer_credential_failure_is_sanitized():
    with pytest.raises(WorkerProtocolError) as captured:
        _peer_uid(BrokenPeerSocket())
    assert "CANARY" not in str(captured.value)


@pytest.mark.parametrize(
    "uids",
    [
        frozenset(),
        frozenset((-1,)),
        frozenset((2**32 - 1,)),
        frozenset((True,)),
        {os.getuid()},
    ],
)
def test_malformed_trusted_peer_configuration_is_rejected(tmp_path, uids):
    with pytest.raises(ValueError):
        WorkerServerConfig(tmp_path / "worker.sock", uids)


@pytest.mark.parametrize(
    "changes",
    [
        {"socket_path": Path("relative.sock")},
        {"socket_path": "not-a-path"},
        {"accept_timeout_seconds": 0},
        {"accept_timeout_seconds": 11},
        {"accept_timeout_seconds": True},
    ],
)
def test_server_configuration_rejects_invalid_paths_and_accept_timeouts(
    tmp_path, changes
):
    values = {
        "socket_path": tmp_path / "worker.sock",
        "permitted_peer_uids": frozenset((os.getuid(),)),
    }
    values.update(changes)
    with pytest.raises(ValueError):
        WorkerServerConfig(**values)


def test_server_requires_peer_credential_facility(tmp_path, monkeypatch):
    directory = secure_directory(tmp_path)
    instance, _transport = gateway(directory)
    monkeypatch.delattr(socket, "SO_PEERCRED")
    with pytest.raises(RuntimeError, match="unavailable"):
        OpenAIWorkerServer(
            WorkerServerConfig(directory / "worker.sock", frozenset((os.getuid(),))),
            instance,
        )


@pytest.mark.parametrize("kind", ["file", "socket", "symlink"])
def test_preexisting_socket_path_is_never_silently_deleted(tmp_path, kind):
    directory = secure_directory(tmp_path)
    path = directory / "worker.sock"
    peer = None
    if kind == "file":
        path.touch()
    elif kind == "socket":
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.bind(str(path))
    else:
        path.symlink_to(directory / "missing")
    instance, _transport = gateway(directory)
    server = OpenAIWorkerServer(
        WorkerServerConfig(path, frozenset((os.getuid(),))), instance
    )
    with pytest.raises(ValueError, match="already exists"):
        server.start()
    assert path.is_symlink() or path.exists()
    if peer is not None:
        peer.close()


def test_unsafe_or_symlinked_socket_directory_fails_closed(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    target = secure_directory(tmp_path)
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)

    for path in (unsafe / "worker.sock", link / "worker.sock"):
        instance, _transport = gateway(target)
        server = OpenAIWorkerServer(
            WorkerServerConfig(path, frozenset((os.getuid(),))), instance
        )
        with pytest.raises(ValueError):
            server.start()


def test_close_does_not_remove_substituted_or_already_missing_path(tmp_path):
    directory = secure_directory(tmp_path)
    path = directory / "worker.sock"
    instance, _transport = gateway(directory)
    server = OpenAIWorkerServer(
        WorkerServerConfig(path, frozenset((os.getuid(),))), instance
    )
    server.start()
    path.unlink()
    path.touch()
    server.close()
    assert path.is_file()

    path.unlink()
    server.close()
    assert not path.exists()

    missing_server = OpenAIWorkerServer(
        WorkerServerConfig(path, frozenset((os.getuid(),))), instance
    )
    missing_server.start()
    path.unlink()
    missing_server.close()
    assert not path.exists()


def test_server_lifecycle_and_type_validation(tmp_path):
    directory = secure_directory(tmp_path)
    instance, _transport = gateway(directory)
    config = WorkerServerConfig(
        directory / "worker.sock",
        frozenset((os.getuid(),)),
        accept_timeout_seconds=0.01,
    )
    with pytest.raises(TypeError):
        OpenAIWorkerServer(object(), instance)
    with pytest.raises(TypeError):
        OpenAIWorkerServer(config, object())
    server = OpenAIWorkerServer(config, instance)
    with pytest.raises(RuntimeError, match="not started"):
        server.serve_once()
    server.start()
    try:
        with pytest.raises(RuntimeError, match="already"):
            server.start()
        assert server.serve_once() is False
        with pytest.raises(ValueError):
            server.serve(max_connections=0)
    finally:
        server.close()


def test_serve_loop_counts_only_handled_connections(tmp_path):
    directory = secure_directory(tmp_path)
    instance, _transport = gateway(directory)
    server = OpenAIWorkerServer(
        WorkerServerConfig(
            directory / "worker.sock",
            frozenset((os.getuid(),)),
            accept_timeout_seconds=0.01,
        ),
        instance,
    )
    server.start()
    worker = Thread(target=lambda: server.serve(max_connections=1))
    worker.start()
    time.sleep(0.03)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(directory / "worker.sock"))
    payload = encode_request(
        InferenceRequest("operation-a", "correlation-a", request(directory))
    )
    client.sendall(struct.pack("!I", len(payload)) + payload)
    client.shutdown(socket.SHUT_WR)
    receive_frame(client, deadline=time.monotonic() + 2, io_timeout=1)
    client.close()
    worker.join(timeout=2)
    server.close()
    assert worker.is_alive() is False


def test_socket_bind_and_post_bind_failures_are_sanitized_and_cleaned(
    tmp_path, monkeypatch
):
    directory = secure_directory(tmp_path)
    path = directory / "worker.sock"
    instance, _transport = gateway(directory)
    server = OpenAIWorkerServer(
        WorkerServerConfig(path, frozenset((os.getuid(),))), instance
    )
    path.touch()
    monkeypatch.setattr(server_module, "_validate_socket_location", lambda _path: None)
    with pytest.raises(RuntimeError, match="creation"):
        server.start()
    path.unlink()

    monkeypatch.undo()
    server = OpenAIWorkerServer(
        WorkerServerConfig(path, frozenset((os.getuid(),))), instance
    )
    monkeypatch.setattr(
        os, "chmod", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())
    )
    with pytest.raises(RuntimeError, match="creation"):
        server.start()
    assert not path.exists()


def test_non_socket_post_bind_substitution_fails_closed(tmp_path, monkeypatch):
    directory = secure_directory(tmp_path)
    path = directory / "worker.sock"
    instance, _transport = gateway(directory)

    class FakeListener:
        def bind(self, value):
            Path(value).touch()

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda *_args: FakeListener())
    server = OpenAIWorkerServer(
        WorkerServerConfig(path, frozenset((os.getuid(),))), instance
    )
    with pytest.raises(RuntimeError, match="creation"):
        server.start()
    assert path.exists()


def test_response_write_failure_is_bounded(tmp_path, monkeypatch):
    directory = secure_directory(tmp_path)
    instance, _transport = gateway(directory)
    server = OpenAIWorkerServer(
        WorkerServerConfig(directory / "worker.sock", frozenset((os.getuid(),))),
        instance,
    )
    original = server_module.send_frame
    monkeypatch.setattr(
        server_module,
        "send_frame",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(WorkerProtocolError("failed")),
    )
    server.start()
    thread = run_once(server)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(directory / "worker.sock"))
    payload = encode_request(
        InferenceRequest("operation-a", "correlation-a", request(directory))
    )
    original(
        client,
        payload,
        deadline=time.monotonic() + 2,
        io_timeout=1,
    )
    client.shutdown(socket.SHUT_WR)
    assert client.recv(1) == b""
    client.close()
    thread.join(timeout=2)
    server.close()
    assert thread.is_alive() is False


def test_socket_location_rejects_long_and_missing_directories(tmp_path):
    instance, _transport = gateway(tmp_path)
    long_path = Path("/tmp") / ("x" * 108)
    missing_path = tmp_path / "missing" / "worker.sock"
    for path in (long_path, missing_path):
        server = OpenAIWorkerServer(
            WorkerServerConfig(path, frozenset((os.getuid(),))), instance
        )
        with pytest.raises(ValueError):
            server.start()


def test_offline_worker_factory_owns_separate_database_and_uses_no_credentials(
    tmp_path, monkeypatch
):
    directory = secure_directory(tmp_path)
    canary = "TEST-ONLY-OPENAI-CANARY-WORKER"
    monkeypatch.setenv("OPENAI_API_KEY", canary)
    database = directory / "provider.sqlite3"
    server = build_offline_worker(
        directory / "worker.sock", database, frozenset((os.getuid(),))
    )
    server.start()
    try:
        payload = encode_request(
            InferenceRequest("operation-a", "correlation-a", request(directory))
        )
        result = raw_exchange(server, struct.pack("!I", len(payload)) + payload)
    finally:
        server.close()
    assert result.outcome == "disabled"
    assert database.exists()
    assert canary not in repr(result)
