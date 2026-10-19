"""Real owner HTTP, kernel credential socket and PilotStop regression tests."""

import json
import os
from pathlib import Path
import runpy
import socket
import sqlite3
from threading import Thread
from datetime import UTC, datetime, timedelta
import time

import pytest

from app.ops.int26_owner_pilot import build_application
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.owner_control.store import OwnerStore
from app.owner_control.worker_stop import WorkerStopClient
from app.trusted.private_pilot_worker import PilotStop
from app.trusted.private_pilot import PilotAuthorityError
from app.trusted.private_pilot import PilotAllowance, load_activation
from app.trusted.worker_stop import WorkerStopServer, assert_stop
from test_int26_owner_console import call, login, settings
from test_int22_pilot import activation_file


def stop_file(tmp_path):
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    path = control / "emergency-stop.json"
    path.write_text('{"version":"private-pilot-emergency-stop-v1","stop":false}')
    path.chmod(0o400)
    return path


def socket_path(tmp_path):
    directory = tmp_path / "socket"
    directory.mkdir(mode=0o700)
    return directory / "stop.sock"


def test_owner_http_reaches_real_worker_stop_and_replays_without_clearing(tmp_path):
    path = stop_file(tmp_path)
    address = socket_path(tmp_path)
    server = WorkerStopServer(address, path, os.geteuid())
    server.start()
    try:
        args = settings(tmp_path)
        args.worker_stop_socket = str(address)
        args.worker_stop_uid = os.geteuid()
        app = build_application(args)
        cookie, csrf = login(app)
        code, app_only, _ = call(app, "/operation", "POST", cookie=cookie, csrf=csrf)
        assert code == 200
        code, _, _ = call(
            app,
            "/emergency-stop",
            "POST",
            cookie=cookie,
            csrf=csrf,
            payload={"operation_id": app_only["operation_id"]},
        )
        assert code == 400
        assert not PilotStop(path).active()
        code, issued, _ = call(
            app, "/worker-stop-operation", "POST", cookie=cookie, csrf=csrf
        )
        assert code == 200
        worker = Thread(target=server.serve_once)
        worker.start()
        code, result, _ = call(
            app,
            "/emergency-stop",
            "POST",
            cookie=cookie,
            csrf=csrf,
            payload={"operation_id": issued["operation_id"]},
        )
        worker.join(timeout=2)
        assert code == 200
        assert result == {"application": "confirmed", "worker": "confirmed"}
        assert app.store.active() and PilotStop(path).active()
        assert json.loads(path.read_text())["stop"] is True
        assert stat_mode(path) == 0o400
        code, repeat, _ = call(
            app,
            "/emergency-stop",
            "POST",
            cookie=cookie,
            csrf=csrf,
            payload={"operation_id": issued["operation_id"]},
        )
        assert code == 200 and repeat == result
    finally:
        server.close()


def test_admitted_operation_finishes_after_expiry_without_losing_worker_stop(
    tmp_path, monkeypatch
):
    path = stop_file(tmp_path)
    address = socket_path(tmp_path)
    server = WorkerStopServer(address, path, os.geteuid())
    server.start()
    try:
        args = settings(tmp_path)
        args.worker_stop_socket = str(address)
        args.worker_stop_uid = os.geteuid()
        app = build_application(args)
        cookie, csrf = login(app)
        now = [datetime.now(UTC)]
        monkeypatch.setattr(app.store, "_clock", lambda: now[0])
        _, issued, _ = call(
            app, "/worker-stop-operation", "POST", cookie=cookie, csrf=csrf
        )
        now[0] += timedelta(seconds=299)
        original = app.store.assert_application_stop

        def cross_expiry(token, operation):
            original(token, operation)
            now[0] += timedelta(seconds=2)

        monkeypatch.setattr(app.store, "assert_application_stop", cross_expiry)
        worker = Thread(target=server.serve_once)
        worker.start()
        code, result, _ = call(
            app,
            "/emergency-stop",
            "POST",
            cookie=cookie,
            csrf=csrf,
            payload={"operation_id": issued["operation_id"]},
        )
        worker.join(timeout=2)
        assert code == 200
        assert result == {"application": "confirmed", "worker": "confirmed"}
        assert PilotStop(path).active()
        assert app.store.latest_worker_stop(cookie.split("=", 1)[1]) == result
        assert (
            call(
                app,
                "/emergency-stop",
                "POST",
                cookie=cookie,
                csrf=csrf,
                payload={"operation_id": issued["operation_id"]},
            )[0]
            == 400
        )
    finally:
        server.close()


def test_stop_file_rejects_malformed_substitution_and_is_idempotent(
    tmp_path, monkeypatch
):
    from app.trusted import worker_stop

    path = stop_file(tmp_path)
    path.chmod(0o600)
    path.write_text('{"version":"private-pilot-emergency-stop-v1","stop":0}')
    path.chmod(0o400)
    with pytest.raises(ValueError):
        assert_stop(path)
    path.chmod(0o600)
    path.write_text('{"version":"private-pilot-emergency-stop-v1","stop":false}')
    path.chmod(0o400)
    original = worker_stop.protected_ancestry
    calls = [0]

    def changed(value):
        calls[0] += 1
        identity = original(value)
        return identity if calls[0] == 1 else identity + ((1, 2),)

    monkeypatch.setattr(worker_stop, "protected_ancestry", changed)
    with pytest.raises(ValueError, match="changed"):
        assert_stop(path)
    assert not list(path.parent.glob(".stop-*"))
    monkeypatch.setattr(worker_stop, "protected_ancestry", original)
    assert_stop(path)
    identity = (path.stat().st_dev, path.stat().st_ino)
    assert_stop(path)
    assert identity == (path.stat().st_dev, path.stat().st_ino)
    calls[0] = 0
    monkeypatch.setattr(worker_stop, "protected_ancestry", changed)
    with pytest.raises(ValueError, match="changed"):
        assert_stop(path)
    monkeypatch.setattr(worker_stop, "protected_ancestry", original)
    with pytest.raises(PilotAuthorityError):
        assert_stop(tmp_path / "missing" / "stop.json")


def test_worker_assertion_preserves_authority_allowance_and_accounting(tmp_path):
    activation_path, _ = activation_file(tmp_path)
    stop = activation_path.parent / "stop.json"
    stop.write_text('{"version":"private-pilot-emergency-stop-v1","stop":false}')
    stop.chmod(0o400)
    allowance_path = tmp_path / "allowance.sqlite3"
    authority = load_activation(activation_path)
    allowance = PilotAllowance.initialize(allowance_path, authority)
    events = EventStore(tmp_path / "provider.db")
    provider = ProviderStateStore(events)
    original_activation = activation_path.read_bytes()
    original_allowance = allowance.remaining()
    original_usage = provider.usage_attempt_count()
    original_suspension = provider.suspension()
    assert_stop(stop)
    assert PilotStop(stop).active()
    assert activation_path.read_bytes() == original_activation
    assert allowance.remaining() == original_allowance
    assert provider.usage_attempt_count() == original_usage
    assert provider.suspension() == original_suspension


def test_assertion_preserves_mode_under_restrictive_umask(tmp_path):
    path = stop_file(tmp_path)
    previous = os.umask(0o777)
    try:
        assert_stop(path)
    finally:
        os.umask(previous)
    assert stat_mode(path) == 0o400
    assert PilotStop(path).active()
    from app.trusted.worker_telemetry import _stop

    assert _stop(path) == {"valid": True, "active": True}


def test_socket_configuration_deadline_and_malformed_ack(tmp_path):
    path = stop_file(tmp_path)
    address = socket_path(tmp_path)
    with pytest.raises(ValueError):
        WorkerStopServer(Path("relative"), path, os.geteuid())
    with pytest.raises(ValueError):
        WorkerStopClient(Path("relative"), os.geteuid())
    with pytest.raises(ValueError):
        WorkerStopClient(address, -1)
    with pytest.raises(RuntimeError):
        WorkerStopServer(address, path, os.geteuid()).serve_once()
    args = settings(tmp_path)
    args.worker_stop_socket = str(address)
    with pytest.raises(ValueError, match="configured together"):
        build_application(args)
    args.worker_stop_socket = None
    args.worker_stop_uid = os.geteuid()
    with pytest.raises(ValueError, match="configured together"):
        build_application(args)
    address.write_text("not a socket")
    assert WorkerStopClient(address, os.geteuid()).assert_stop() == "unavailable"
    address.unlink()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(address))
        address.chmod(0o600)
        listener.listen(1)

        def slow():
            with listener.accept()[0] as connection:
                connection.recv(100)
                time.sleep(1.2)

        worker = Thread(target=slow)
        worker.start()
        assert WorkerStopClient(address, os.geteuid()).assert_stop() == "uncertain"
        worker.join(timeout=2)


def test_client_peer_identity_extra_frame_and_expired_deadline(tmp_path, monkeypatch):
    from app.owner_control import worker_stop as client_module
    from types import SimpleNamespace

    address = socket_path(tmp_path)
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(address))
        address.chmod(0o600)
        listener.listen(1)
        expected = os.geteuid() + 1
        original_lstat = Path.lstat

        def forged_entry(path):
            result = original_lstat(path)
            if path == address:
                return SimpleNamespace(st_mode=result.st_mode, st_uid=expected)
            return result

        monkeypatch.setattr(Path, "lstat", forged_entry)

        def peer_check():
            with listener.accept()[0] as connection:
                assert connection.recv(1) == b""

        worker = Thread(target=peer_check)
        worker.start()
        assert WorkerStopClient(address, expected).assert_stop() == "unavailable"
        worker.join(timeout=2)
        monkeypatch.setattr(Path, "lstat", original_lstat)

        def extra_ack():
            with listener.accept()[0] as connection:
                connection.recv(100)
                connection.sendall(b"STOP-CONFIRMED/1\nX")

        worker = Thread(target=extra_ack)
        worker.start()
        assert WorkerStopClient(address, os.geteuid()).assert_stop() == "uncertain"
        worker.join(timeout=2)

        original_monotonic = client_module.monotonic
        times = iter((0.0, 0.0, 2.0))
        monkeypatch.setattr(client_module, "monotonic", lambda: next(times))

        def no_ack():
            with listener.accept()[0] as connection:
                connection.recv(100)

        worker = Thread(target=no_ack)
        worker.start()
        assert WorkerStopClient(address, os.geteuid()).assert_stop() == "uncertain"
        worker.join(timeout=2)
        monkeypatch.setattr(client_module, "monotonic", original_monotonic)
    address.unlink()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(address))
        address.chmod(0o600)
        listener.listen(1)

        def malformed():
            with listener.accept()[0] as connection:
                connection.recv(100)
                connection.sendall(b"FALSE\n")

        worker = Thread(target=malformed)
        worker.start()
        assert WorkerStopClient(address, os.geteuid()).assert_stop() == "uncertain"
        worker.join(timeout=2)


def test_server_lifecycle_and_cli(tmp_path, monkeypatch):
    from app.trusted import worker_stop

    path = stop_file(tmp_path)
    address = socket_path(tmp_path)
    server = WorkerStopServer(address, path, os.geteuid())
    server.start()
    with pytest.raises(ValueError):
        WorkerStopServer(address, path, os.geteuid()).start()
    assert server.serve_once() is False
    server.serve(should_stop=lambda: True)
    checks = [False, True]
    server.serve(should_stop=lambda: checks.pop(0))
    server.close()
    server.close()
    assert not address.exists()
    seen = []

    class FakeServer:
        def __init__(self, *args, **kwargs):
            seen.append((args, kwargs))

        def start(self):
            seen.append("start")

        def serve(self, *, should_stop):
            seen.append("serve")
            assert not should_stop()

        def close(self):
            seen.append("close")

    monkeypatch.setattr(worker_stop, "WorkerStopServer", FakeServer)
    assert (
        worker_stop.main(
            [
                "--socket",
                str(address),
                "--stop-file",
                str(path),
                "--allow-uid",
                str(os.geteuid()),
            ]
        )
        == 0
    )
    assert seen[1:] == ["start", "serve", "close"]
    handlers = {}
    monkeypatch.setattr(
        worker_stop.signal,
        "signal",
        lambda number, handler: handlers.setdefault(number, handler),
    )

    class SignalledServer(FakeServer):
        def serve(self, *, should_stop):
            assert not should_stop()
            handlers[worker_stop.signal.SIGTERM](None, None)
            assert should_stop()

    monkeypatch.setattr(worker_stop, "WorkerStopServer", SignalledServer)
    assert (
        worker_stop.main(
            [
                "--socket",
                str(address),
                "--stop-file",
                str(path),
                "--allow-uid",
                str(os.geteuid()),
            ]
        )
        == 0
    )
    monkeypatch.setattr("sys.argv", ["worker_stop"])
    with pytest.raises(SystemExit) as error:
        runpy.run_module("app.trusted.worker_stop", run_name="__main__")
    assert error.value.code == 2


def test_group_socket_and_server_failure_paths(tmp_path, monkeypatch):
    from app.trusted import worker_stop

    path = stop_file(tmp_path)
    address = socket_path(tmp_path)
    address.parent.chmod(0o710)
    group = os.getegid()
    server = WorkerStopServer(address, path, os.geteuid(), socket_group_gid=group)
    server.start()
    assert stat_mode(address) == 0o660
    server.close()
    address.parent.chmod(0o700)

    original_socket = worker_stop.socket.socket

    class FailingSocket:
        def __init__(self, failure):
            self.real = original_socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.failure = failure

        def bind(self, value):
            if self.failure == "bind":
                raise OSError("synthetic bind failure")
            return self.real.bind(value)

        def listen(self, count):
            if self.failure == "listen":
                raise OSError("synthetic listen failure")
            return self.real.listen(count)

        def __getattr__(self, name):
            return getattr(self.real, name)

    for failure in ("bind", "listen"):
        monkeypatch.setattr(
            worker_stop.socket, "socket", lambda *_: FailingSocket(failure)
        )
        with pytest.raises((RuntimeError, OSError)):
            WorkerStopServer(address, path, os.geteuid()).start()
        assert not address.exists() if failure == "listen" else True
    monkeypatch.setattr(worker_stop.socket, "socket", original_socket)

    server = WorkerStopServer(address, path, os.geteuid())
    server.start()
    address.unlink()
    server.close()
    server = WorkerStopServer(address, path, os.geteuid())
    server.start()
    address.unlink()
    with socket.socket(socket.AF_UNIX) as replacement:
        replacement.bind(str(address))
        server.close()
        assert address.exists()
    address.unlink()


def test_server_rejects_oversized_partial_and_unavailable_stop(tmp_path, monkeypatch):
    from app.trusted import worker_stop

    path = stop_file(tmp_path)
    address = socket_path(tmp_path)
    server = WorkerStopServer(address, path, os.geteuid())
    server.start()
    try:
        worker = Thread(target=server.serve_once)
        worker.start()
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(address))
            connection.sendall(b"ASSERT-STOP/1\nX")
            connection.shutdown(socket.SHUT_WR)
        worker.join(timeout=2)
        assert not PilotStop(path).active()

        clock = iter((0.0, 0.0, 1.0))
        monkeypatch.setattr(worker_stop, "monotonic", lambda: next(clock))
        worker = Thread(target=server.serve_once)
        worker.start()
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(address))
            connection.sendall(b"A")
            connection.shutdown(socket.SHUT_WR)
        worker.join(timeout=2)
        monkeypatch.undo()
        assert not PilotStop(path).active()

        path.unlink()
        worker = Thread(target=server.serve_once)
        worker.start()
        assert WorkerStopClient(address, os.geteuid()).assert_stop() == "uncertain"
        worker.join(timeout=2)
    finally:
        server.close()


def stat_mode(path):
    return path.stat().st_mode & 0o777


def test_unavailable_service_and_unsafe_files_never_claim_worker_stop(tmp_path):
    path = stop_file(tmp_path)
    address = socket_path(tmp_path)
    args = settings(tmp_path)
    args.worker_stop_socket = str(address)
    args.worker_stop_uid = os.geteuid()
    app = build_application(args)
    cookie, csrf = login(app)
    _, issued, _ = call(app, "/worker-stop-operation", "POST", cookie=cookie, csrf=csrf)
    code, result, _ = call(
        app,
        "/emergency-stop",
        "POST",
        cookie=cookie,
        csrf=csrf,
        payload={"operation_id": issued["operation_id"]},
    )
    assert code == 200
    assert result == {"application": "confirmed", "worker": "unavailable"}
    assert not PilotStop(path).active()
    path.chmod(0o600)
    with pytest.raises(ValueError):
        assert_stop(path)
    path.unlink()
    path.symlink_to(tmp_path / "absent")
    with pytest.raises(ValueError):
        assert_stop(path)


def test_denied_peer_and_malformed_request_do_not_assert_stop(tmp_path):
    path = stop_file(tmp_path)
    address = socket_path(tmp_path)
    server = WorkerStopServer(address, path, os.geteuid() + 1)
    server.start()
    try:
        worker = Thread(target=server.serve_once)
        worker.start()
        assert WorkerStopClient(address, os.geteuid()).assert_stop() == "uncertain"
        worker.join(timeout=2)
        assert not PilotStop(path).active()
    finally:
        server.close()
    server = WorkerStopServer(address, path, os.geteuid())
    server.start()
    try:
        worker = Thread(target=server.serve_once)
        worker.start()
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(address))
            connection.sendall(b"CLEAR-STOP/1\n")
            connection.shutdown(socket.SHUT_WR)
        worker.join(timeout=2)
        assert not PilotStop(path).active()
    finally:
        server.close()


def test_local_failure_still_attempts_worker_and_expired_id_fails(
    tmp_path, monkeypatch
):
    path = stop_file(tmp_path)
    address = socket_path(tmp_path)
    server = WorkerStopServer(address, path, os.geteuid())
    server.start()
    try:
        args = settings(tmp_path)
        args.worker_stop_socket = str(address)
        args.worker_stop_uid = os.geteuid()
        app = build_application(args)
        cookie, csrf = login(app)
        _, issued, _ = call(
            app, "/worker-stop-operation", "POST", cookie=cookie, csrf=csrf
        )
        monkeypatch.setattr(
            app.store,
            "assert_application_stop",
            lambda *_: (_ for _ in ()).throw(RuntimeError("disk")),
        )
        worker = Thread(target=server.serve_once)
        worker.start()
        code, result, _ = call(
            app,
            "/emergency-stop",
            "POST",
            cookie=cookie,
            csrf=csrf,
            payload={"operation_id": issued["operation_id"]},
        )
        worker.join(timeout=2)
        assert code == 200
        assert result == {"application": "unavailable", "worker": "confirmed"}
        assert PilotStop(path).active()
        assert (
            app.store.worker_operation(cookie.split("=", 1)[1], issued["operation_id"])
            == result
        )
        now = app.store._clock()
        monkeypatch.setattr(app.store, "_clock", lambda: now + timedelta(minutes=6))
        code, _, _ = call(
            app,
            "/emergency-stop",
            "POST",
            cookie=cookie,
            csrf=csrf,
            payload={"operation_id": issued["operation_id"]},
        )
        assert code == 400
    finally:
        server.close()


def test_lost_ack_is_uncertain_and_same_operation_can_reconcile(tmp_path):
    path = stop_file(tmp_path)
    address = socket_path(tmp_path)
    args = settings(tmp_path)
    args.worker_stop_socket = str(address)
    args.worker_stop_uid = os.geteuid()
    app = build_application(args)
    cookie, csrf = login(app)
    _, issued, _ = call(app, "/worker-stop-operation", "POST", cookie=cookie, csrf=csrf)
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(address))
        address.chmod(0o600)
        listener.listen(1)

        def lose_ack():
            with listener.accept()[0] as connection:
                assert connection.recv(100) == b"ASSERT-STOP/1\n"
                assert_stop(path)

        worker = Thread(target=lose_ack)
        worker.start()
        code, result, _ = call(
            app,
            "/emergency-stop",
            "POST",
            cookie=cookie,
            csrf=csrf,
            payload={"operation_id": issued["operation_id"]},
        )
        worker.join(timeout=2)
        assert code == 200
        assert result == {"application": "confirmed", "worker": "uncertain"}
        assert PilotStop(path).active()
    address.unlink()
    server = WorkerStopServer(address, path, os.geteuid())
    server.start()
    try:
        worker = Thread(target=server.serve_once)
        worker.start()
        code, result, _ = call(
            app,
            "/emergency-stop",
            "POST",
            cookie=cookie,
            csrf=csrf,
            payload={"operation_id": issued["operation_id"]},
        )
        worker.join(timeout=2)
        assert code == 200
        assert result == {"application": "confirmed", "worker": "confirmed"}
    finally:
        server.close()


def test_worker_operation_scope_and_durable_replay_guards(tmp_path, monkeypatch):
    args = settings(tmp_path)
    app = build_application(args)
    cookie, csrf = login(app)
    token = cookie.split("=", 1)[1]
    assert app.store.latest_worker_stop(token) is None
    with pytest.raises(PermissionError):
        app.store.latest_worker_stop("forged")
    assert (
        call(app, "/worker-stop-operation", "POST", cookie=cookie, csrf=csrf)[0] == 503
    )
    operation = app.store.issue_worker_stop(token)
    assert app.store.worker_operation(token, operation) == {
        "application": "pending",
        "worker": "pending",
    }
    assert OwnerStore(args.owner_directory).latest_worker_stop(token) == {
        "application": "pending",
        "worker": "pending",
    }
    with pytest.raises(ValueError):
        app.store.worker_operation(token, "forged")
    with pytest.raises(ValueError):
        app.store.worker_operation(token, "f" * 32)
    with pytest.raises(ValueError):
        app.store.assert_application_stop(token, "f" * 32)
    with pytest.raises(ValueError):
        app.store.record_worker_outcome(token, "f" * 32, "worker_result", "confirmed")
    with pytest.raises(PermissionError):
        app.store.worker_operation("forged", operation)
    with pytest.raises(PermissionError):
        app.store.issue_worker_stop("forged")
    with pytest.raises(ValueError):
        app.store.record_worker_outcome(
            token, operation, "application_result", "invalid"
        )
    app.store.record_worker_outcome(token, operation, "worker_result", "confirmed")
    app.store.record_worker_outcome(token, operation, "worker_result", "unavailable")
    assert app.store.worker_operation(token, operation)["worker"] == "confirmed"
    assert (
        OwnerStore(args.owner_directory).latest_worker_stop(token)["worker"]
        == "confirmed"
    )
    with sqlite3.connect(app.store.path) as connection:
        connection.execute("DELETE FROM owner_stop")
    with pytest.raises(RuntimeError):
        app.store.assert_application_stop(token, operation)
    with sqlite3.connect(app.store.path) as connection:
        connection.execute("INSERT INTO owner_stop VALUES (1,1)")
    for _ in range(31):
        app.store.issue_worker_stop(token)
    with pytest.raises(ValueError):
        app.store.issue_worker_stop(token)


def test_latest_emergency_result_survives_owner_login_and_restart(tmp_path):
    args = settings(tmp_path)
    address = socket_path(tmp_path)
    args.worker_stop_socket = str(address)
    args.worker_stop_uid = os.geteuid()
    app = build_application(args)
    cookie, csrf = login(app)
    code, issued, _ = call(
        app, "/worker-stop-operation", "POST", cookie=cookie, csrf=csrf
    )
    assert code == 200
    status = call(app, "/status", "POST", cookie=cookie)[1]
    assert status["emergency_operation"] == {
        "application": "pending",
        "worker": "pending",
    }
    code, result, _ = call(
        app,
        "/emergency-stop",
        "POST",
        cookie=cookie,
        csrf=csrf,
        payload={"operation_id": issued["operation_id"]},
    )
    assert code == 200 and result["worker"] == "unavailable"
    restarted = build_application(args)
    fresh_cookie, fresh_csrf = login(restarted)
    status = call(restarted, "/status", "POST", cookie=fresh_cookie)[1]
    assert status["emergency_operation"] == {
        "application": "confirmed",
        "worker": "unavailable",
    }
    assert (
        call(
            restarted,
            "/emergency-stop",
            "POST",
            cookie=fresh_cookie,
            csrf=fresh_csrf,
            payload={"operation_id": issued["operation_id"]},
        )[0]
        == 400
    )


def test_worker_control_http_admission_blocks_participant_and_malformed_input(tmp_path):
    path = stop_file(tmp_path)
    address = socket_path(tmp_path)
    args = settings(tmp_path)
    args.worker_stop_socket = str(address)
    args.worker_stop_uid = os.geteuid()
    app = build_application(args)
    payload = {"operation_id": "f" * 32}
    assert (
        call(
            app,
            "/emergency-stop",
            "POST",
            cookie="ar_session=participant",
            csrf="model",
            payload=payload,
        )[0]
        == 401
    )
    cookie, csrf = login(app)
    assert (
        call(
            app,
            "/worker-stop-operation",
            "POST",
            cookie=cookie,
            csrf=csrf,
            HTTP_HOST="participant.test",
        )[0]
        == 400
    )
    assert (
        call(
            app,
            "/worker-stop-operation",
            "POST",
            cookie=cookie,
            csrf=csrf,
            HTTP_ORIGIN="https://participant.test",
        )[0]
        == 403
    )
    assert (
        call(app, "/worker-stop-operation", "POST", cookie=cookie, csrf="model")[0]
        == 403
    )
    _, issued, _ = call(app, "/worker-stop-operation", "POST", cookie=cookie, csrf=csrf)
    assert (
        call(
            app,
            "/emergency-stop",
            "POST",
            cookie=cookie,
            csrf=csrf,
            payload={"operation_id": issued["operation_id"], "path": str(path)},
        )[0]
        == 400
    )
    assert (
        call(
            app,
            "/emergency-stop",
            "POST",
            cookie=cookie,
            csrf=csrf,
            payload={"operation_id": issued["operation_id"], "active": False},
        )[0]
        == 400
    )
    assert not PilotStop(path).active()
    rejected = [
        record for record in app.store.audit() if record["result"] == "rejected"
    ]
    assert len(rejected) >= 4
    assert {record["action_type"] for record in rejected} == {"worker_stop_control"}


def test_outcome_record_failures_remain_uncertain_and_worker_is_attempted(
    tmp_path, monkeypatch
):
    path = stop_file(tmp_path)
    address = socket_path(tmp_path)
    server = WorkerStopServer(address, path, os.geteuid())
    server.start()
    try:
        args = settings(tmp_path)
        args.worker_stop_socket = str(address)
        args.worker_stop_uid = os.geteuid()
        app = build_application(args)
        cookie, csrf = login(app)
        _, issued, _ = call(
            app, "/worker-stop-operation", "POST", cookie=cookie, csrf=csrf
        )
        original = app.store.record_worker_outcome

        def fail_application(token, operation, field, result):
            if field == "application_result":
                raise RuntimeError("synthetic commit failure")
            return original(token, operation, field, result)

        monkeypatch.setattr(app.store, "record_worker_outcome", fail_application)
        worker = Thread(target=server.serve_once)
        worker.start()
        code, result, _ = call(
            app,
            "/emergency-stop",
            "POST",
            cookie=cookie,
            csrf=csrf,
            payload={"operation_id": issued["operation_id"]},
        )
        worker.join(timeout=2)
        assert code == 200 and result == {
            "application": "uncertain",
            "worker": "confirmed",
        }
        assert PilotStop(path).active()
        monkeypatch.setattr(
            app.store,
            "record_worker_outcome",
            lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic commit failure")),
        )
        _, second, _ = call(
            app, "/worker-stop-operation", "POST", cookie=cookie, csrf=csrf
        )
        worker = Thread(target=server.serve_once)
        worker.start()
        code, result, _ = call(
            app,
            "/emergency-stop",
            "POST",
            cookie=cookie,
            csrf=csrf,
            payload={"operation_id": second["operation_id"]},
        )
        worker.join(timeout=2)
        assert code == 200 and result == {
            "application": "uncertain",
            "worker": "uncertain",
        }
    finally:
        server.close()
