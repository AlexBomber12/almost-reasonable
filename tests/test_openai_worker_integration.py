from dataclasses import replace
import inspect
import multiprocessing
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import runpy
from threading import Thread
import time

import pytest

from app.core.action_policy import ActionPolicy, TrustedActionContext
from app.core.openai_worker_client import (
    CLIENT_COMMUNICATION_FAILURE,
    CLIENT_OUTCOME_UNCERTAIN,
    ClientResult,
    OpenAIWorkerClient,
)
from app.core.provider_contract import DeploymentStage, default_openai_provider_config
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.trusted.openai_gateway import TrustedOpenAIGateway
from app.trusted.openai_transport import FakeOpenAITransport, OpenAITransportError
from app.trusted.openai_worker_server import (
    OpenAIWorkerServer,
    WorkerServerConfig,
    build_offline_worker,
    main,
)
from tests.test_openai_worker_server import gateway, request, response, secure_directory
from tests.test_secretless_boundary import normal_capabilities, principal


def wait_for_socket(path, process=None):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process is not None and not process.is_alive():
            break
        time.sleep(0.01)
    raise AssertionError("worker socket did not become ready")


def offline_worker_process(socket_path, database_path, ready):
    server = build_offline_worker(
        Path(socket_path), Path(database_path), frozenset((os.getuid(),))
    )
    try:
        server.start()
        ready.send(os.getpid())
        server.serve(max_connections=1)
    finally:
        server.close()
        ready.close()


def crashing_worker_process(socket_path, ready):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(socket_path)
    listener.listen(1)
    ready.send(os.getpid())
    connection, _ = listener.accept()
    connection.recv(4096)
    os._exit(0)


def offline_main_process(socket_path, database_path):
    main(
        [
            "--socket",
            socket_path,
            "--database",
            database_path,
            "--allow-uid",
            str(os.getuid()),
        ]
    )


def test_client_and_worker_run_in_distinct_os_processes_with_real_unix_socket(tmp_path):
    directory = secure_directory(tmp_path)
    socket_path = directory / "worker.sock"
    parent, child = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=offline_worker_process,
        args=(str(socket_path), str(directory / "provider.sqlite3"), child),
    )
    process.start()
    child.close()
    worker_pid = parent.recv()
    wait_for_socket(socket_path, process)

    result = OpenAIWorkerClient(socket_path).infer(
        request(directory), operation_id="operation-a", correlation_id="correlation-a"
    )
    process.join(timeout=5)

    assert worker_pid != os.getpid()
    assert process.exitcode == 0
    assert result.outcome == "disabled"
    assert result.operation_id == "operation-a"
    assert not socket_path.exists()


def test_client_has_no_transport_or_sdk_dependency():
    source = inspect.getsource(sys.modules[OpenAIWorkerClient.__module__])
    assert "openai_transport" not in source
    assert "OPENAI_API_KEY" not in source
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.core.openai_worker_client; "
                "assert 'openai' not in sys.modules; "
                "assert 'app.trusted.openai_transport' not in sys.modules"
            ),
        ],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_client_returns_typed_result_and_policy_still_mediates_proposal(tmp_path):
    directory = secure_directory(tmp_path)
    instance, transport = gateway(
        directory,
        [
            response(
                '{"decision":"reply","reason_category":null,'
                '"content":"Только черновик.","brief":null,"draft":null}'
            )
        ],
        stage=DeploymentStage.STAGE_1,
    )
    server = OpenAIWorkerServer(
        WorkerServerConfig(directory / "worker.sock", frozenset((os.getuid(),))),
        instance,
    )
    server.start()
    worker = Thread(target=server.serve_once)
    worker.start()
    try:
        result = OpenAIWorkerClient(directory / "worker.sock").infer(
            request(directory),
            operation_id="operation-a",
            correlation_id="correlation-a",
        )
    finally:
        worker.join(timeout=2)
        server.close()

    decision = ActionPolicy().evaluate(
        result.validation,
        TrustedActionContext("correlation-a", principal("a")),
        normal_capabilities(private_reply_drafts_enabled=False),
    )
    assert isinstance(result, ClientResult)
    assert result.outcome == "completed"
    assert result.validation.valid is True
    assert decision.dispatchable is False
    assert len(transport.calls) == 1


def test_connect_failure_is_distinct_from_uncertain_post_transmission_failure(tmp_path):
    directory = secure_directory(tmp_path)
    socket_path = directory / "worker.sock"
    model = request(directory)
    unavailable = OpenAIWorkerClient(socket_path, io_timeout_seconds=0.1).infer(
        model, operation_id="operation-a", correlation_id="correlation-a"
    )

    parent, child = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=crashing_worker_process, args=(str(socket_path), child)
    )
    process.start()
    child.close()
    assert parent.recv() != os.getpid()
    uncertain = OpenAIWorkerClient(socket_path, io_timeout_seconds=0.2).infer(
        model, operation_id="operation-b", correlation_id="correlation-b"
    )
    process.join(timeout=5)
    if socket_path.exists():
        socket_path.unlink()

    assert unavailable.outcome == CLIENT_COMMUNICATION_FAILURE
    assert uncertain.outcome == CLIENT_OUTCOME_UNCERTAIN
    assert uncertain.operation_id == "operation-b"
    assert uncertain.correlation_id == "correlation-b"
    assert process.exitcode == 0


def test_client_does_not_retry_lost_response_under_a_new_operation_id(tmp_path):
    directory = secure_directory(tmp_path)
    socket_path = directory / "worker.sock"
    accepted = []

    def lose_response():
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.listen(2)
        connection, _ = listener.accept()
        accepted.append(connection.recv(4096))
        connection.close()
        listener.settimeout(0.1)
        with pytest.raises(TimeoutError):
            listener.accept()
        listener.close()

    worker = Thread(target=lose_response)
    worker.start()
    wait_for_socket(socket_path)
    result = OpenAIWorkerClient(socket_path, io_timeout_seconds=0.2).infer(
        request(directory), operation_id="operation-a", correlation_id="correlation-a"
    )
    worker.join(timeout=2)
    socket_path.unlink()

    assert result.outcome == CLIENT_OUTCOME_UNCERTAIN
    assert len(accepted) == 1
    assert result.operation_id == "operation-a"


def test_worker_emergency_stop_blocks_fake_provider_invocation(tmp_path):
    directory = secure_directory(tmp_path)
    instance, transport = gateway(
        directory, [response()], stage=DeploymentStage.STAGE_1, stop=lambda: True
    )
    server = OpenAIWorkerServer(
        WorkerServerConfig(directory / "worker.sock", frozenset((os.getuid(),))),
        instance,
    )
    server.start()
    worker = Thread(target=server.serve_once)
    worker.start()
    try:
        result = OpenAIWorkerClient(directory / "worker.sock").infer(
            request(directory),
            operation_id="operation-a",
            correlation_id="correlation-a",
        )
    finally:
        worker.join(timeout=2)
        server.close()
    assert result.outcome == "emergency_stop"
    assert transport.calls == []


def test_provider_transport_failure_returns_only_sanitized_result(tmp_path):
    directory = secure_directory(tmp_path)
    failures = [OpenAITransportError(status_code=503) for _ in range(2)]
    instance, transport = gateway(directory, failures, stage=DeploymentStage.STAGE_1)
    server = OpenAIWorkerServer(
        WorkerServerConfig(directory / "worker.sock", frozenset((os.getuid(),))),
        instance,
    )
    server.start()
    worker = Thread(target=server.serve_once)
    worker.start()
    try:
        result = OpenAIWorkerClient(directory / "worker.sock").infer(
            request(directory),
            operation_id="operation-a",
            correlation_id="correlation-a",
        )
    finally:
        worker.join(timeout=2)
        server.close()
    assert result.outcome == "server_error"
    assert result.attempts == 2
    assert result.validation is None
    assert result.safe_russian_message == (
        "Сервис модели временно недоступен. Попробуйте позже."
    )
    assert len(transport.calls) == 2


@pytest.mark.parametrize("suspension", [False, True])
def test_worker_restart_preserves_duplicate_or_spend_suspension(tmp_path, suspension):
    directory = secure_directory(tmp_path)
    database = directory / "provider.sqlite3"
    config = replace(
        default_openai_provider_config(),
        deployment_stage=DeploymentStage.STAGE_1,
        paid_calls_enabled=True,
    )
    first_transport = FakeOpenAITransport(
        [
            OpenAITransportError(
                status_code=429,
                error_type="insufficient_quota",
                error_code="project_spend_limit_exceeded",
            )
            if suspension
            else response()
        ]
    )
    first = OpenAIWorkerServer(
        WorkerServerConfig(directory / "worker.sock", frozenset((os.getuid(),))),
        TrustedOpenAIGateway(
            store=ProviderStateStore(EventStore(database)),
            transport=first_transport,
            config=config,
        ),
    )
    first.start()
    thread = Thread(target=first.serve_once)
    thread.start()
    initial = OpenAIWorkerClient(directory / "worker.sock").infer(
        request(directory), operation_id="operation-a", correlation_id="correlation-a"
    )
    thread.join(timeout=2)
    first.close()

    second_transport = FakeOpenAITransport([response()])
    second = OpenAIWorkerServer(
        WorkerServerConfig(directory / "worker.sock", frozenset((os.getuid(),))),
        TrustedOpenAIGateway(
            store=ProviderStateStore(EventStore(database)),
            transport=second_transport,
            config=config,
        ),
    )
    second.start()
    thread = Thread(target=second.serve_once)
    thread.start()
    repeated = OpenAIWorkerClient(directory / "worker.sock").infer(
        request(directory),
        operation_id="operation-b" if suspension else "operation-a",
        correlation_id="correlation-b",
    )
    thread.join(timeout=2)
    second.close()

    assert initial.outcome == ("spend_limit_confirmed" if suspension else "completed")
    assert repeated.outcome == (
        "spend_suspended" if suspension else "duplicate_operation"
    )
    assert second_transport.calls == []


@pytest.mark.parametrize(
    "path",
    ["", "relative.sock", "x" * 108, "bad\x00path", object()],
)
def test_client_rejects_malformed_socket_paths(path):
    with pytest.raises((TypeError, ValueError)):
        OpenAIWorkerClient(path)


def test_client_rejects_non_utf8_socket_path():
    with pytest.raises(ValueError):
        OpenAIWorkerClient("\ud800")


def test_client_rejects_invalid_metadata_and_model_before_connecting(tmp_path):
    client = OpenAIWorkerClient(tmp_path / "missing.sock")
    with pytest.raises(ValueError):
        client.infer(
            request(tmp_path), operation_id="bad value", correlation_id="correlation"
        )
    with pytest.raises(ValueError):
        client.infer(
            request(tmp_path), operation_id="o" * 127, correlation_id="correlation"
        )
    with pytest.raises(TypeError):
        client.infer(object(), operation_id="operation", correlation_id="correlation")


def test_executable_harness_handles_one_request_and_restores_signal_handlers(tmp_path):
    directory = secure_directory(tmp_path)
    socket_path = directory / "worker.sock"
    observed = []

    def invoke_client():
        wait_for_socket(socket_path)
        observed.append(
            OpenAIWorkerClient(socket_path).infer(
                request(directory),
                operation_id="operation-a",
                correlation_id="correlation-a",
            )
        )

    client_thread = Thread(target=invoke_client)
    client_thread.start()
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    result = main(
        [
            "--socket",
            str(socket_path),
            "--database",
            str(directory / "provider.sqlite3"),
            "--allow-uid",
            str(os.getuid()),
            "--max-connections",
            "1",
        ]
    )
    client_thread.join(timeout=2)

    assert result == 0
    assert observed[0].outcome == "disabled"
    assert signal.getsignal(signal.SIGTERM) is previous_term
    assert signal.getsignal(signal.SIGINT) is previous_int
    assert not socket_path.exists()
    assert stat.S_ISREG((directory / "provider.sqlite3").stat().st_mode)


def test_main_signal_callback_stops_loop_and_always_closes(monkeypatch, tmp_path):
    handlers = {}

    class FakeServer:
        def __init__(self):
            self.closed = False

        def start(self):
            pass

        def serve_once(self):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return False

        def close(self):
            self.closed = True

    fake = FakeServer()
    monkeypatch.setattr(
        "app.trusted.openai_worker_server.build_offline_worker",
        lambda *_args: fake,
    )

    def capture(which, handler):
        previous = handlers.get(which, signal.SIG_DFL)
        handlers[which] = handler
        return previous

    monkeypatch.setattr(signal, "signal", capture)
    assert (
        main(
            [
                "--socket",
                str(tmp_path / "worker.sock"),
                "--database",
                str(tmp_path / "provider.sqlite3"),
                "--allow-uid",
                str(os.getuid()),
            ]
        )
        == 0
    )
    assert fake.closed is True


def test_module_entrypoint_is_executable(monkeypatch, tmp_path):
    directory = secure_directory(tmp_path)
    socket_path = directory / "worker.sock"
    observed = []

    def invoke_client():
        wait_for_socket(socket_path)
        observed.append(
            OpenAIWorkerClient(socket_path).infer(
                request(directory),
                operation_id="operation-a",
                correlation_id="correlation-a",
            )
        )

    thread = Thread(target=invoke_client)
    thread.start()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "openai_worker_server",
            "--socket",
            str(socket_path),
            "--database",
            str(directory / "provider.sqlite3"),
            "--allow-uid",
            str(os.getuid()),
            "--max-connections",
            "1",
        ],
    )
    with pytest.warns(RuntimeWarning), pytest.raises(SystemExit) as captured:
        runpy.run_module("app.trusted.openai_worker_server", run_name="__main__")
    thread.join(timeout=2)
    assert captured.value.code == 0
    assert observed[0].outcome == "disabled"


def test_sigterm_cleans_up_only_the_worker_owned_socket(tmp_path):
    directory = secure_directory(tmp_path)
    socket_path = directory / "worker.sock"
    unrelated = directory / "keep.txt"
    unrelated.touch()
    process = multiprocessing.Process(
        target=offline_main_process,
        args=(str(socket_path), str(directory / "provider.sqlite3")),
    )
    process.start()
    wait_for_socket(socket_path, process)
    process.terminate()
    process.join(timeout=5)

    assert process.exitcode == 0
    assert not socket_path.exists()
    assert unrelated.exists()
