"""Synthetic end-to-end and fault tests for the observational status boundary."""

from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import runpy
import socket
import sqlite3
import struct
import sys
from types import SimpleNamespace
from threading import Event, Thread
from time import monotonic

import pytest

from app.core.provider_contract import CostEstimateStatus
from app.core.provider_spend import ProviderAttemptRecord, ProviderStateStore
from app.core.store import EventStore
from app.owner_control.worker_telemetry import WorkerTelemetryClient, _read_exact
from app.ops.int26_owner_pilot import build_application
from app.trusted.worker_telemetry import (
    WorkerTelemetryServer,
    _provider,
    _safe_socket_directory,
    _stop,
)
from app.worker_telemetry.protocol import REQUEST, VERSION, decode, encode
from test_int26_owner_console import call, login, settings
from test_openai_gateway import gateway, request, response


def sources(tmp_path):
    worker = tmp_path / "worker"
    worker.mkdir(mode=0o700)
    db = worker / "provider.sqlite"
    EventStore(db)
    stop = worker / "emergency-stop.json"
    write_stop(stop, True)
    ipc = tmp_path / "ipc"
    ipc.mkdir(mode=0o700)
    return db, stop, ipc / "status.sock"


def write_stop(path, active):
    replacement = path.with_suffix(".new")
    replacement.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": active})
    )
    replacement.chmod(0o400)
    replacement.replace(path)


def attempt(
    identifier,
    *,
    input_tokens=10,
    cached_input_tokens=2,
    output_tokens=5,
    total_tokens=15,
):
    return ProviderAttemptRecord(
        attempt_id=identifier,
        correlation_id="synthetic-correlation",
        provider="openai",
        model_identifier="synthetic-model",
        provider_config_version="synthetic-config",
        prompt_version="synthetic-prompt",
        core_self_version="synthetic-self",
        provider_response_id=None,
        started_at=datetime(2026, 10, 19, tzinfo=UTC),
        duration_ms=1,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        outcome="synthetic-operation",
        retry_count=0,
        estimated_cost=None,
        pricing_version=None,
        currency=None,
        cost_estimate_status=(
            CostEstimateStatus.USAGE_INCOMPLETE
            if None in (input_tokens, cached_input_tokens, output_tokens, total_tokens)
            else CostEstimateStatus.PRICING_UNAVAILABLE
        ),
        provider_error_code=None,
    )


def test_gateway_interruption_after_fake_response_preserves_uncertain_reservation(
    tmp_path, monkeypatch
):
    instance, transport, store = gateway(tmp_path, [response()])
    db = tmp_path / "events.sqlite3"

    def interrupted(*_args, **_kwargs):
        raise RuntimeError("synthetic process interruption")

    monkeypatch.setattr(store, "record_attempt", interrupted)
    with pytest.raises(RuntimeError, match="synthetic process interruption"):
        instance.submit(
            request(),
            correlation_id="synthetic-correlation",
            operation_id="synthetic-operation",
        )
    assert len(transport.calls) == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_usage_attempts"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_attempt_reservations"
        ).fetchone() == (1,)
    projection = _provider(db)
    assert projection["available"] is True
    assert projection["attempt_count"] == 0
    assert projection["recorded_tokens"]["total_tokens"] == 0
    assert projection["unknown_usage_attempts"] == 0
    assert projection["unresolved_reservations"] == 1
    assert _provider(db)["unresolved_reservations"] == 1
    assert ProviderStateStore(EventStore(db)).usage_attempt_count() == 0
    _unused_db, stop, path = sources(tmp_path)
    for _restart in range(2):
        server = WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid()}))
        server.start()
        try:
            thread = Thread(target=server.serve_once)
            thread.start()
            observed = WorkerTelemetryClient(path, os.geteuid()).read()
            thread.join()
            assert observed["provider"]["unresolved_reservations"] == 1
            assert observed["provider"]["attempt_count"] == 0
        finally:
            server.close()


def test_reservation_before_contact_and_finalization(tmp_path):
    db, _stop, _socket = sources(tmp_path)
    provider = ProviderStateStore(EventStore(db))
    assert _provider(db)["unresolved_reservations"] == 0
    provider.reserve_attempt(
        "synthetic:1", "synthetic-correlation", datetime(2026, 10, 19, tzinfo=UTC)
    )
    observed = _provider(db)
    assert observed["attempt_count"] == 0
    assert observed["unknown_usage_attempts"] == 0
    assert observed["unresolved_reservations"] == 1
    assert observed["recorded_tokens"]["total_tokens"] == 0
    assert ProviderStateStore(EventStore(db)).record_attempt(
        attempt("synthetic:1"), require_reservation=True
    )
    finalized = _provider(db)
    assert finalized["attempt_count"] == 1
    assert finalized["unresolved_reservations"] == 0
    assert finalized["recorded_tokens"]["total_tokens"] == 15


def test_incomplete_usage_and_unresolved_reservation_are_separate(tmp_path):
    db, _stop, _socket = sources(tmp_path)
    provider = ProviderStateStore(EventStore(db))
    provider.record_attempt(attempt("finalized:1", total_tokens=None))
    provider.reserve_attempt(
        "pending:1", "synthetic-correlation", datetime(2026, 10, 19, tzinfo=UTC)
    )
    observed = _provider(db)
    assert observed["attempt_count"] == 1
    assert observed["unknown_usage_attempts"] == 1
    assert observed["unresolved_reservations"] == 1
    assert observed["recorded_tokens"]["total_tokens"] == 0


@pytest.mark.parametrize(
    "timestamp", ["not-a-time", "2026-10-19", "2026-10-19T00:00:00+00:00"]
)
def test_malformed_reservation_timestamp_fails_closed(tmp_path, timestamp):
    db, _stop, _socket = sources(tmp_path)
    ProviderStateStore(EventStore(db)).reserve_attempt(
        "pending:1", "synthetic-correlation", datetime(2026, 10, 19, tzinfo=UTC)
    )
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE provider_attempt_reservations SET reserved_at=?", (timestamp,)
        )
    assert _provider(db)["available"] is False


def test_overlapping_reservation_is_not_counted_twice_and_mismatch_fails_closed(
    tmp_path,
):
    db, _stop, _socket = sources(tmp_path)
    ProviderStateStore(EventStore(db)).record_attempt(attempt("synthetic:1"))
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO provider_attempt_reservations VALUES (?,?,?,?)",
            (
                "synthetic:1",
                "synthetic-correlation",
                "openai",
                "2026-10-19T00:00:00.000000Z",
            ),
        )
    observed = _provider(db)
    assert observed["available"] is True
    assert observed["attempt_count"] == 1
    assert observed["unresolved_reservations"] == 0
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE provider_attempt_reservations SET correlation_id='wrong'"
        )
    assert _provider(db)["available"] is False
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE provider_attempt_reservations SET correlation_id='synthetic-correlation'"
        )
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE provider_attempt_reservations SET provider='other'")
    assert _provider(db)["available"] is False


def test_usage_and_reservations_share_one_sqlite_snapshot(tmp_path, monkeypatch):
    db, _stop, _socket = sources(tmp_path)
    provider = ProviderStateStore(EventStore(db))
    provider.reserve_attempt(
        "synthetic:1", "synthetic-correlation", datetime(2026, 10, 19, tzinfo=UTC)
    )
    import app.trusted.worker_telemetry as module

    original_connect = sqlite3.connect
    finalized = []

    class FinalizeBetweenReads(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if "FROM provider_attempt_reservations AS r" in sql:
                finalized.append(
                    provider.record_attempt(
                        attempt("synthetic:1"), require_reservation=True
                    )
                )
            return super().execute(sql, parameters)

    def connect(*args, **kwargs):
        if kwargs.get("uri"):
            kwargs["factory"] = FinalizeBetweenReads
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    observed = _provider(db)
    assert finalized == [True]
    assert observed["available"] is True
    assert observed["attempt_count"] == 0
    assert observed["unresolved_reservations"] == 1
    assert _provider(db)["attempt_count"] == 1


def test_real_socket_aggregates_live_wal_and_replaced_stop(tmp_path):
    db, stop, path = sources(tmp_path)
    server = WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid()}))
    server.start()
    try:
        client = WorkerTelemetryClient(path, os.geteuid())
        thread = Thread(target=server.serve_once)
        thread.start()
        first = client.read()
        thread.join()
        assert first["provider"] == {
            "available": True,
            "attempt_count": 0,
            "recorded_tokens": {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
            "unknown_usage_attempts": 0,
            "unresolved_reservations": 0,
            "spend_suspended": False,
        }
        assert first["stop"] == {"valid": True, "active": True}
        with sqlite3.connect(db):
            provider = ProviderStateStore(EventStore(db))
            provider.record_attempt(attempt("chat:1"))
            provider.record_attempt(
                attempt(
                    "memory:1",
                    input_tokens=None,
                    cached_input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                )
            )
            write_stop(stop, False)
            thread = Thread(target=server.serve_once)
            thread.start()
            second = client.read()
            thread.join()
        assert second["stop"] == {"valid": True, "active": False}
        assert second["provider"]["attempt_count"] == 2
        assert second["provider"]["recorded_tokens"] == {
            "input_tokens": 10,
            "cached_input_tokens": 2,
            "output_tokens": 5,
            "total_tokens": 15,
        }
        assert second["provider"]["unknown_usage_attempts"] == 1
        assert "synthetic-correlation" not in json.dumps(second)
        assert "memory:1" not in json.dumps(second)
    finally:
        server.close()


def test_denied_peer_does_not_read_sources(tmp_path, monkeypatch):
    db, stop, path = sources(tmp_path)
    server = WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid() + 1}))
    server.start()
    try:
        monkeypatch.setattr(
            "app.trusted.worker_telemetry._provider",
            lambda _: pytest.fail("read protected state"),
        )
        thread = Thread(target=server.serve_once)
        thread.start()
        assert WorkerTelemetryClient(path, os.geteuid()).read()["available"] is False
        thread.join()
    finally:
        server.close()


def test_owner_authentication_precedes_worker_contact(tmp_path, monkeypatch):
    db, stop, path = sources(tmp_path)
    ProviderStateStore(EventStore(db)).reserve_attempt(
        "pending:1", "synthetic-correlation", datetime(2026, 10, 19, tzinfo=UTC)
    )
    args = settings(tmp_path)
    args.worker_telemetry_socket = str(path)
    args.worker_telemetry_uid = os.geteuid()
    app = build_application(args)
    server = WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid()}))
    server.start()
    try:
        reads = []
        original = app.worker_telemetry.read

        def observed():
            reads.append(True)
            return original()

        monkeypatch.setattr(app.worker_telemetry, "read", observed)
        code, body, _ = call(app)
        assert code == 401 and body == {"error": "Owner authentication required."}
        assert reads == []
        cookie, _ = login(app)
        thread = Thread(target=server.serve_once)
        thread.start()
        code, body, _ = call(app, cookie=cookie)
        thread.join()
        assert code == 200 and reads == [True]
        assert body["worker"]["provider"]["attempt_count"] == 0
        assert body["worker"]["provider"]["unresolved_reservations"] == 1
        assert "provider.sqlite" not in json.dumps(body)
    finally:
        server.close()


def test_owner_telemetry_is_optional(tmp_path):
    args = settings(tmp_path)
    app = build_application(args)
    cookie, _ = login(app)
    code, body, _ = call(app, cookie=cookie)
    assert code == 200 and body["worker"]["available"] is False
    args.worker_telemetry_socket = "/tmp/status.sock"
    with pytest.raises(ValueError, match="together"):
        build_application(args)


def test_telemetry_never_uses_credential_provider_or_allowance(
    tmp_path, monkeypatch, capsys
):
    db, stop, path = sources(tmp_path)
    from app.trusted import private_pilot
    from app.trusted.openai_gateway import TrustedOpenAIGateway

    original_reader = private_pilot.read_protected_worker_file
    reads = []

    def read_control(control_path, *, maximum_bytes):
        reads.append(control_path)
        assert control_path == stop
        return original_reader(control_path, maximum_bytes=maximum_bytes)

    def forbidden(*_args, **_kwargs):
        pytest.fail("telemetry invoked a mutating or provider operation")

    monkeypatch.setattr(private_pilot, "read_protected_worker_file", read_control)
    monkeypatch.setattr(private_pilot.PilotAllowance, "reserve", forbidden)
    monkeypatch.setattr(TrustedOpenAIGateway, "submit", forbidden)
    monkeypatch.setattr(TrustedOpenAIGateway, "submit_context", forbidden)
    monkeypatch.setattr(TrustedOpenAIGateway, "submit_extraction", forbidden)
    original_stop = stop.read_bytes()
    server = WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid()}))
    server.start()
    try:
        thread = Thread(target=server.serve_once)
        thread.start()
        assert (
            WorkerTelemetryClient(path, os.geteuid()).read()["provider"][
                "attempt_count"
            ]
            == 0
        )
        thread.join()
    finally:
        server.close()
    assert reads == [stop]
    assert stop.read_bytes() == original_stop
    assert _provider(db)["attempt_count"] == 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "change",
    [
        "missing_db",
        "unsafe_db",
        "schema",
        "malformed_usage",
        "missing_stop",
        "malformed_stop",
        "unsafe_stop",
    ],
)
def test_bad_sources_are_explicitly_unavailable(tmp_path, change):
    db, stop, _ = sources(tmp_path)
    if change == "missing_db":
        db.unlink()
    elif change == "unsafe_db":
        db.chmod(0o644)
    elif change == "schema":
        with sqlite3.connect(db) as connection:
            connection.execute("PRAGMA user_version=4")
    elif change == "malformed_usage":
        ProviderStateStore(EventStore(db)).record_attempt(attempt("operation:1"))
        with sqlite3.connect(db) as connection:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute("UPDATE provider_usage_attempts SET input_tokens=-1")
    elif change == "missing_stop":
        stop.unlink()
    elif change == "malformed_stop":
        stop.chmod(0o600)
        stop.write_text('{"version":"private-pilot-emergency-stop-v1","stop":0}')
        stop.chmod(0o400)
    elif change == "unsafe_stop":
        stop.chmod(0o600)
    provider = _provider(db)
    observed_stop = _stop(stop)
    if change.endswith("stop"):
        assert observed_stop == {"valid": False, "active": None}
        assert provider["available"] is True
    else:
        assert provider["available"] is False
        assert observed_stop == {"valid": True, "active": True}


def test_bounded_protocol_rejects_sensitive_or_bad_frames(tmp_path):
    db, stop, path = sources(tmp_path)
    server = WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid()}))
    server.start()
    try:
        for request in (b"OTHER\n", REQUEST + b"/etc/passwd"):
            thread = Thread(target=server.serve_once)
            thread.start()
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(1)
                connection.connect(str(path))
                connection.sendall(request)
                try:
                    assert connection.recv(4) == b""
                except ConnectionResetError:
                    pass
            thread.join()
        assert len(encode({"version": VERSION, "available": True})) < 2048
        with pytest.raises(ValueError):
            decode(b'{"version":"worker-status-v1","available":true,"secret":"x"}')
        with pytest.raises(ValueError):
            encode({"secret": "x" * 3000})
    finally:
        server.close()


def test_fragmented_fixed_request_and_trailing_bytes(tmp_path):
    db, stop, path = sources(tmp_path)
    server = WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid()}))
    server.start()
    try:
        thread = Thread(target=server.serve_once)
        thread.start()
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(1)
            connection.connect(str(path))
            connection.sendall(REQUEST[:5])
            connection.sendall(REQUEST[5:])
            connection.shutdown(socket.SHUT_WR)
            deadline = monotonic() + 1.0
            size = struct.unpack("!I", _read_exact(connection, 4, deadline))[0]
            assert (
                decode(_read_exact(connection, size, deadline))["provider"][
                    "attempt_count"
                ]
                == 0
            )
        thread.join()
        thread = Thread(target=server.serve_once)
        thread.start()
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(1)
            connection.connect(str(path))
            connection.sendall(REQUEST)
            connection.sendall(b"extra")
            connection.shutdown(socket.SHUT_WR)
            try:
                assert connection.recv(4) == b""
            except ConnectionResetError:
                pass
        thread.join()
    finally:
        server.close()


def test_unavailable_endpoint_and_invalid_config(tmp_path):
    db, stop, path = sources(tmp_path)
    assert WorkerTelemetryClient(path, os.geteuid()).read()["available"] is False
    with pytest.raises(ValueError):
        WorkerTelemetryClient(Path("relative"), os.geteuid())
    with pytest.raises(ValueError):
        WorkerTelemetryServer(path, db, stop, frozenset())
    path.touch()
    with pytest.raises(ValueError):
        WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid()})).start()


def test_client_rejects_oversized_disconnected_and_stale_responses(tmp_path):
    db, stop, path = sources(tmp_path)
    server = WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid()}))
    server.start()
    client = WorkerTelemetryClient(path, os.geteuid())
    original = server.serve_once
    try:
        for payload in (
            struct.pack("!I", 2049),
            b"",
            encode(
                {
                    "version": VERSION,
                    "available": True,
                    "collected_at": "2020-01-01T00:00:00+00:00",
                    "stop": {"valid": False, "active": None},
                    "provider": {
                        "available": False,
                        "attempt_count": None,
                        "recorded_tokens": None,
                        "unknown_usage_attempts": None,
                        "unresolved_reservations": None,
                        "spend_suspended": None,
                    },
                }
            ),
        ):

            def serve_bad():
                conn, _ = server.listener.accept()
                with conn:
                    conn.recv(len(REQUEST))
                    if payload:
                        conn.sendall(payload)

            thread = Thread(target=serve_bad)
            thread.start()
            assert client.read()["available"] is False
            thread.join()
    finally:
        server.serve_once = original
        server.close()


def test_frame_read_has_one_deadline_across_trickled_bytes(monkeypatch):
    import app.owner_control.worker_telemetry as module

    elapsed = [0.0]
    timeouts = []

    class Trickle:
        def settimeout(self, timeout):
            timeouts.append(timeout)

        def recv(self, _size):
            elapsed[0] += 0.4
            return b"x"

    monkeypatch.setattr(module, "monotonic", lambda: elapsed[0], raising=False)
    with pytest.raises(TimeoutError):
        _read_exact(Trickle(), 4, 1.0)
    assert timeouts == pytest.approx([1.0, 0.6, 0.2])


def test_database_contention_and_unsafe_sidecars(tmp_path):
    db, _stop_path, _path = sources(tmp_path)
    with sqlite3.connect(db, timeout=0.2) as blocker:
        blocker.execute("PRAGMA locking_mode=EXCLUSIVE")
        blocker.execute("BEGIN EXCLUSIVE")
        assert _provider(db)["available"] is False
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db}{suffix}")
        sidecar.write_text("synthetic")
        sidecar.chmod(0o644)
        assert _provider(db)["available"] is False
        sidecar.unlink()


def test_explicit_incomplete_usage_status_is_counted(tmp_path):
    db, _stop_path, _path = sources(tmp_path)
    ProviderStateStore(EventStore(db)).record_attempt(attempt("synthetic:1"))
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE provider_usage_attempts SET cost_estimate_status='usage_incomplete'"
        )
    provider = _provider(db)
    assert provider["attempt_count"] == 1
    assert provider["recorded_tokens"]["total_tokens"] == 15
    assert provider["unknown_usage_attempts"] == 1


def test_unknown_cost_status_rejects_provider_evidence(tmp_path):
    db, _stop_path, _path = sources(tmp_path)
    ProviderStateStore(EventStore(db)).record_attempt(attempt("synthetic:1"))
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE provider_usage_attempts SET cost_estimate_status='synthetic_invalid'"
        )
    provider = _provider(db)
    assert provider["available"] is False
    assert provider["attempt_count"] is None
    assert provider["recorded_tokens"] is None


def test_non_openai_usage_row_rejects_provider_evidence(tmp_path):
    db, _stop_path, _path = sources(tmp_path)
    ProviderStateStore(EventStore(db)).record_attempt(attempt("synthetic:1"))
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE provider_usage_attempts SET provider='synthetic_other'"
        )
    provider = _provider(db)
    assert provider["available"] is False
    assert provider["attempt_count"] is None
    assert provider["recorded_tokens"] is None


def test_spend_suspension_requires_valid_record(tmp_path):
    db, _stop_path, _path = sources(tmp_path)
    code = "project_spend_limit_exceeded"
    record = replace(
        attempt("synthetic:1"),
        outcome="confirmed_spend_limit",
        provider_error_code=code,
    )
    ProviderStateStore(EventStore(db)).record_attempt(
        record, confirmed_spend_limit_code=code
    )
    assert _provider(db)["spend_suspended"] is True
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE provider_spend_suspensions SET confirmed_at='not-a-time'"
        )
    assert _provider(db)["available"] is False
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE provider_spend_suspensions SET confirmed_at='2026-10-19T00:00:00.000000Z', provider='other'"
        )
    assert _provider(db)["available"] is False


def test_replaced_database_and_sidecar_are_rejected(tmp_path, monkeypatch):
    db, _stop_path, _path = sources(tmp_path)
    import app.trusted.worker_telemetry as module

    original = module.protected_state_identity
    calls = []

    def changed(path):
        calls.append(True)
        value = original(path)
        return value if len(calls) == 1 else (value[0], value[1], value[2] + 1)

    monkeypatch.setattr(module, "protected_state_identity", changed)
    assert _provider(db)["available"] is False
    monkeypatch.setattr(module, "protected_state_identity", original)
    original_sidecars = module._sidecar_identities
    calls.clear()

    def changed_sidecar(path):
        calls.append(True)
        value = original_sidecars(path)
        return value if len(calls) == 1 else ((1, 2), (3, 4))

    monkeypatch.setattr(module, "_sidecar_identities", changed_sidecar)
    assert _provider(db)["available"] is False


def test_socket_location_and_lifecycle_errors(tmp_path, monkeypatch):
    db, stop, path = sources(tmp_path)
    with pytest.raises(ValueError, match="path"):
        _safe_socket_directory(Path("relative.sock"), None)
    with pytest.raises(ValueError, match="unavailable"):
        _safe_socket_directory(tmp_path / "missing" / "status.sock", None)
    path.parent.chmod(0o755)
    with pytest.raises(ValueError, match="directory"):
        _safe_socket_directory(path, None)
    path.parent.chmod(0o700)
    server = WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid()}))
    with pytest.raises(RuntimeError, match="not started"):
        server.serve_once()
    server.start()
    with pytest.raises(RuntimeError, match="already started"):
        server.start()
    assert server.serve_once() is False
    socket_entry = path.lstat()
    server.close()
    assert not path.exists()
    assert socket_entry.st_ino != 0
    server.close()


def test_socket_group_mode_and_owner_client_peer_check(tmp_path, monkeypatch):
    db, stop, path = sources(tmp_path)
    path.parent.chmod(0o710)
    server = WorkerTelemetryServer(
        path, db, stop, frozenset({os.geteuid()}), socket_group_gid=os.getegid()
    )
    server.start()
    try:
        assert path.stat().st_mode & 0o777 == 0o660
        client = WorkerTelemetryClient(path, os.geteuid())
        real_lstat = Path.lstat

        def apparent_uid(self):
            value = real_lstat(self)
            if self == path:
                return SimpleNamespace(st_mode=value.st_mode, st_uid=os.geteuid() + 1)
            return value

        monkeypatch.setattr(Path, "lstat", apparent_uid)
        assert client.read()["available"] is False
        monkeypatch.setattr(Path, "lstat", real_lstat)
        thread = Thread(target=server.serve_once)
        thread.start()
        assert client.read()["available"] is True
        thread.join()
    finally:
        server.close()


def test_protocol_rejects_invalid_projection():
    from app.worker_telemetry.protocol import MAX_RESPONSE

    good = {
        "version": VERSION,
        "available": True,
        "collected_at": datetime.now(UTC).isoformat(),
        "stop": {"valid": True, "active": False},
        "provider": {
            "available": True,
            "attempt_count": 1,
            "recorded_tokens": {
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "output_tokens": 2,
                "total_tokens": 3,
            },
            "unknown_usage_attempts": 0,
            "unresolved_reservations": 0,
            "spend_suspended": False,
        },
    }
    assert decode(json.dumps(good).encode())["provider"]["attempt_count"] == 1
    bad = []
    for key, value in (("version", "wrong"), ("available", False), ("collected_at", 1)):
        candidate = json.loads(json.dumps(good))
        candidate[key] = value
        bad.append(candidate)
    for value in (
        None,
        {"valid": True, "active": None},
        {"valid": False, "active": False},
    ):
        candidate = json.loads(json.dumps(good))
        candidate["stop"] = value
        bad.append(candidate)
    for value in (
        None,
        {"available": True},
        {**good["provider"], "recorded_tokens": None},
        {**good["provider"], "attempt_count": -1},
        {**good["provider"], "unknown_usage_attempts": 2},
        {**good["provider"], "unresolved_reservations": -1},
        {**good["provider"], "unresolved_reservations": False},
        {
            key: value
            for key, value in good["provider"].items()
            if key != "unresolved_reservations"
        },
        {**good["provider"], "spend_suspended": 0},
        {**good["provider"], "available": False},
    ):
        candidate = json.loads(json.dumps(good))
        candidate["provider"] = value
        bad.append(candidate)
    for item in bad:
        with pytest.raises(ValueError):
            decode(json.dumps(item).encode())
    with pytest.raises(ValueError):
        decode(b" " * (MAX_RESPONSE + 1))


def test_owner_client_rejects_wrong_kernel_peer_uid(tmp_path, monkeypatch):
    db, stop, path = sources(tmp_path)
    server = WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid()}))
    server.start()
    import app.owner_control.worker_telemetry as client_module

    real_socket = socket.socket

    class DifferentPeer:
        def __init__(self, *args):
            self.connection = real_socket(*args)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.connection.close()

        def settimeout(self, value):
            self.connection.settimeout(value)

        def connect(self, value):
            self.connection.connect(value)

        def getsockopt(self, *_args):
            return struct.pack("iII", 1, os.geteuid() + 1, os.getegid())

    monkeypatch.setattr(
        client_module.socket,
        "socket",
        lambda *args, **kwargs: (
            real_socket(*args, **kwargs) if "fileno" in kwargs else DifferentPeer(*args)
        ),
    )
    try:
        thread = Thread(target=server.serve_once)
        thread.start()
        assert WorkerTelemetryClient(path, os.geteuid()).read()["available"] is False
        thread.join()
    finally:
        server.close()


def test_server_disconnect_timeout_and_shutdown(tmp_path):
    db, stop, path = sources(tmp_path)
    server = WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid()}))
    server.start()
    try:
        thread = Thread(target=server.serve_once)
        thread.start()
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(path))
            assert not connection.recv(1)
        thread.join()
        assert path.exists()
    finally:
        server.close()
    server.start()
    flag = Event()
    thread = Thread(target=server.serve, kwargs={"should_stop": flag.is_set})
    thread.start()
    assert WorkerTelemetryClient(path, os.geteuid()).read()["available"] is True
    flag.set()
    thread.join(timeout=2)
    assert not thread.is_alive() and not path.exists()


def test_server_start_failures_and_replaced_socket_cleanup(tmp_path, monkeypatch):
    db, stop, path = sources(tmp_path)
    import app.trusted.worker_telemetry as module

    server = WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid()}))
    real_socket = socket.socket

    class FailedBind:
        def __init__(self, *_args):
            pass

        def bind(self, _path):
            raise OSError("synthetic bind failure")

        def close(self):
            pass

    monkeypatch.setattr(module.socket, "socket", FailedBind)
    with pytest.raises(RuntimeError, match="creation failed"):
        server.start()
    monkeypatch.setattr(module.socket, "socket", real_socket)
    original_chmod = module.os.chmod

    def failed_chmod(*_args, **_kwargs):
        raise OSError("synthetic chmod failure")

    monkeypatch.setattr(module.os, "chmod", failed_chmod)
    with pytest.raises(RuntimeError, match="creation failed"):
        server.start()
    monkeypatch.setattr(module.os, "chmod", original_chmod)
    assert not path.exists()
    path.parent.chmod(0o710)
    server = WorkerTelemetryServer(
        path, db, stop, frozenset({os.geteuid()}), socket_group_gid=os.getegid()
    )

    def unchanged_chmod(*_args, **_kwargs):
        pass

    monkeypatch.setattr(module.os, "chmod", unchanged_chmod)
    with pytest.raises(RuntimeError, match="creation failed"):
        server.start()
    monkeypatch.setattr(module.os, "chmod", original_chmod)
    path.parent.chmod(0o700)
    server = WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid()}))
    server.start()
    path.unlink()
    path.touch()
    server.close()
    assert path.is_file()
    path.unlink()
    server.start()
    path.unlink()
    server.close()


def test_telemetry_cli_starts_and_shuts_down(tmp_path, monkeypatch):
    import app.trusted.worker_telemetry as module

    db, stop, path = sources(tmp_path)
    calls = []

    class Server:
        def __init__(self, *_args, **_kwargs):
            calls.append("init")

        def start(self):
            calls.append("start")

        def serve(self, *, should_stop):
            calls.append("serve")
            handlers[module.signal.SIGTERM](module.signal.SIGTERM, None)
            assert should_stop()

        def close(self):
            calls.append("close")

    handlers = {}

    def signal_handler(number, handler):
        previous = handlers.get(number)
        handlers[number] = handler
        return previous

    monkeypatch.setattr(module, "WorkerTelemetryServer", Server)
    monkeypatch.setattr(module.signal, "signal", signal_handler)
    assert (
        module.main(
            [
                "--socket",
                str(path),
                "--provider-db",
                str(db),
                "--stop-file",
                str(stop),
                "--allow-uid",
                str(os.geteuid()),
            ]
        )
        == 0
    )
    assert calls == ["init", "start", "serve", "close"]
    monkeypatch.setattr(sys, "argv", ["worker_telemetry"])
    with pytest.raises(SystemExit):
        runpy.run_module("app.trusted.worker_telemetry", run_name="__main__")
