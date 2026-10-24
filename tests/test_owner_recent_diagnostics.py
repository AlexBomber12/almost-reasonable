"""Synthetic owner and worker diagnostics never expose source payloads."""

from datetime import UTC, datetime, timedelta
from dataclasses import replace
import json
import os
import socket
import sqlite3
from threading import Event, Thread

import pytest

from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.owner_control.diagnostics import (
    application_replies,
    reference,
    safe_time,
    _sidecars,
)
from app.ops.int26_owner_pilot import build_application
from app.trusted.worker_telemetry import WorkerTelemetryServer, _diagnostics
from app.trusted.openai_gateway import GatewayOutcome
from app.worker_telemetry.protocol import (
    DIAGNOSTICS_VERSION,
    decode_diagnostics,
    MAX_DIAGNOSTICS_RESPONSE,
    encode,
    VERSION,
    REQUEST,
    DIAGNOSTICS_REQUEST,
)
from test_int26_owner_console import call, login, settings
from test_openai_gateway import config, gateway, request
from test_worker_telemetry import attempt, sources
from app.trusted.openai_transport import OpenAITransportError


CANARY = "PRIVATE-CANARY-DO-NOT-SHOW"


def _reply(db, index, outcome):
    when = EventStore._serialize_time(
        datetime(2026, 10, 20, tzinfo=UTC) + timedelta(minutes=index)
    )
    with sqlite3.connect(db) as connection:
        connection.execute(
            """INSERT INTO event_envelopes(event_id,occurred_at,event_type,channel,
               correlation_id,authenticated_source_ref,source_message_id,audience_type,
               audience_principal_id,provenance_type,verification_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"event-{index}",
                when,
                "participant_input",
                "private_chat",
                f"correlation-{index}",
                CANARY,
                f"message-{index}",
                "private_participant",
                CANARY,
                "participant_assertion",
                "unverified",
            ),
        )
        connection.execute(
            """INSERT INTO agent_reply_operations(operation_id,audience_principal_id,
               initiating_event_id,conversation_id,correlation_id,model_identifier,
               prompt_version,core_self_version,source_count)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                f"operation-{index}",
                CANARY,
                f"event-{index}",
                CANARY,
                f"correlation-{index}",
                "model",
                "prompt",
                "core",
                1,
            ),
        )
        if outcome is not None:
            connection.execute(
                "INSERT INTO agent_reply_resolutions(operation_id,outcome,release_id) VALUES (?,?,?)",
                (
                    f"operation-{index}",
                    outcome,
                    f"release-{index}" if outcome == "completed" else None,
                ),
            )


def test_application_reply_diagnostics_are_bounded_authenticated_and_read_only(
    tmp_path,
):
    args = settings(tmp_path)
    for index in range(13):
        _reply(
            args.event_db, index, ("completed", "rejected", "stopped", None)[index % 4]
        )
    app = build_application(args)
    event_store = EventStore(args.event_db)
    before = open(args.event_db, "rb").read()
    assert application_replies(event_store)["available"] is True
    assert open(args.event_db, "rb").read() == before
    assert call(app)[0] == 401
    cookie, _ = login(app)
    code, body, _ = call(app, cookie=cookie)
    assert code == 200
    rows = body["recent"]["application"]["rows"]
    assert len(rows) == 10
    assert rows[0]["time"] > rows[-1]["time"]
    assert rows[0]["outcome"] == "completed"
    assert rows[1]["outcome"] == "unknown"
    assert rows[2]["outcome"] == "stopped"
    assert rows[3]["outcome"] == "rejected"
    assert rows[0]["reference"] == reference("correlation-12")
    assert CANARY not in json.dumps(body)


def test_worker_diagnostics_real_ipc_reservations(tmp_path):
    db, stop, path = sources(tmp_path)
    provider = ProviderStateStore(EventStore(db))
    provider.record_attempt(attempt("synthetic:completed"))
    provider.reserve_attempt(
        "synthetic:pending", "synthetic-correlation", datetime(2026, 10, 20, tzinfo=UTC)
    )
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE provider_usage_attempts SET outcome='invalid_proposal', rejection_reason='invalid_json', provider_error_code=?",
            (CANARY,),
        )
    server = WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid()}))
    server.start()
    try:
        from app.owner_control.worker_telemetry import WorkerTelemetryClient

        client = WorkerTelemetryClient(path, os.geteuid())
        thread = Thread(target=server.serve_once)
        thread.start()
        diagnostics = client.read_diagnostics()
        thread.join()
        assert diagnostics["available"] is True
        assert [row["outcome"] for row in diagnostics["rows"]] == [
            "reservation",
            "rejected",
        ]
        assert (
            diagnostics["rows"][0]["reference"] == diagnostics["rows"][1]["reference"]
        )
        assert CANARY not in json.dumps(diagnostics)
        thread = Thread(target=server.serve_once)
        thread.start()
        assert client.read()["provider"]["available"] is True
        thread.join()
    finally:
        server.close()


def test_legacy_worker_keeps_aggregate_status(tmp_path):
    from app.owner_control.worker_telemetry import WorkerTelemetryClient

    path = tmp_path / "legacy.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    os.chmod(path, 0o600)
    listener.listen(2)
    status = {
        "version": VERSION,
        "available": True,
        "collected_at": datetime.now(UTC).isoformat(),
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

    def serve():
        for _ in range(2):
            connection, _ = listener.accept()
            with connection:
                request = connection.recv(64)
                if request == REQUEST:
                    connection.sendall(encode(status))
                else:
                    assert request == DIAGNOSTICS_REQUEST

    thread = Thread(target=serve)
    thread.start()
    try:
        client = WorkerTelemetryClient(path, os.geteuid())
        assert client.read_diagnostics() == {"available": False, "rows": []}
        assert client.read()["available"] is True
    finally:
        thread.join(timeout=2)
        listener.close()


def test_owner_http_exact_worker_correlation_with_real_ipc(tmp_path):
    from threading import Event

    args = settings(tmp_path)
    _reply(args.event_db, 1, "completed")
    db, stop, path = sources(tmp_path)
    ProviderStateStore(EventStore(db)).record_attempt(
        replace(
            attempt("synthetic:1"), correlation_id="correlation-1", outcome="completed"
        )
    )
    args.worker_telemetry_socket = str(path)
    args.worker_telemetry_uid = os.geteuid()
    app = build_application(args)
    server = WorkerTelemetryServer(path, db, stop, frozenset({os.geteuid()}))
    server.start()
    stopped = Event()
    thread = Thread(target=server.serve, kwargs={"should_stop": stopped.is_set})
    thread.start()
    try:
        cookie, _ = login(app)
        code, body, _ = call(app, cookie=cookie)
        assert code == 200
        assert body["worker"]["available"] is True
        assert body["recent"]["worker"]["available"] is True
        assert (
            body["recent"]["application"]["rows"][0]["reference"]
            == body["recent"]["worker"]["rows"][0]["reference"]
        )
        assert CANARY not in json.dumps(body)
    finally:
        stopped.set()
        thread.join(timeout=2)
        server.close()


def test_gateway_rate_limit_reaches_authenticated_owner_without_spend_suspension(
    tmp_path,
):
    args = settings(tmp_path)
    _, stop, path = sources(tmp_path)
    gateway_instance, transport, provider = gateway(
        tmp_path,
        [
            OpenAITransportError(
                status_code=429,
                error_type="rate_limit_error",
                error_code="rate_limit_exceeded",
            )
        ],
        provider_config=config(max_attempts=1),
    )
    result = gateway_instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )
    assert result.outcome is GatewayOutcome.RATE_LIMIT
    assert result.attempts == 1
    assert len(transport.calls) == 1
    record = provider.usage_attempt("operation-a:1")
    assert record.outcome == GatewayOutcome.RATE_LIMIT.value
    assert provider.suspension() is None

    worker_db = tmp_path / "events.sqlite3"
    projected = _diagnostics(worker_db)
    assert projected["available"] is True
    assert len(projected["rows"]) == 1
    assert (projected["rows"][0]["outcome"], projected["rows"][0]["reason"]) == (
        "rate_limited",
        "rate_limit",
    )
    framed = encode(
        {"version": DIAGNOSTICS_VERSION, **projected}, MAX_DIAGNOSTICS_RESPONSE
    )
    assert decode_diagnostics(framed[4:])["rows"] == projected["rows"]

    args.worker_telemetry_socket = str(path)
    args.worker_telemetry_uid = os.geteuid()
    app = build_application(args)
    stop_before = stop.read_bytes()
    application_stop_before = app.store.active()
    server = WorkerTelemetryServer(path, worker_db, stop, frozenset({os.geteuid()}))
    server.start()
    stopped = Event()
    thread = Thread(target=server.serve, kwargs={"should_stop": stopped.is_set})
    thread.start()
    try:
        cookie, _ = login(app)
        status, body, _ = call(app, cookie=cookie)
        assert status == 200
        assert body["worker"]["provider"]["spend_suspended"] is False
        assert body["worker"]["provider"]["attempt_count"] == 1
        assert body["recent"]["worker"]["rows"] == projected["rows"]
        assert "rate_limit_exceeded" not in json.dumps(body)
        assert CANARY not in json.dumps(body)
    finally:
        stopped.set()
        thread.join(timeout=2)
        server.close()
    assert len(transport.calls) == 1
    assert provider.usage_attempt_count() == 1
    assert provider.suspension() is None
    assert stop.read_bytes() == stop_before
    assert app.store.active() == application_stop_before


def test_malformed_worker_diagnostics_are_unavailable(tmp_path):
    db, _, _ = sources(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO provider_attempt_reservations VALUES (?,?,?,?)",
            ("bad", "bad time", "openai", "malformed"),
        )
    assert _diagnostics(db) == {"available": False, "rows": []}
    assert decode_diagnostics(
        b'{"version":"worker-diagnostics-v1","available":false,"rows":[]}'
    ) == {"available": False, "rows": []}


def test_malformed_application_time_is_unavailable(tmp_path):
    args = settings(tmp_path)
    _reply(args.event_db, 1, "completed")
    with sqlite3.connect(args.event_db) as connection:
        connection.execute("UPDATE event_envelopes SET occurred_at='malformed'")
    assert application_replies(EventStore(args.event_db)) == {
        "available": False,
        "rows": [],
    }


@pytest.mark.parametrize("value", [None, "", "x" * 129])
def test_correlation_reference_rejects_invalid_identities(value):
    with pytest.raises(ValueError):
        reference(value)


@pytest.mark.parametrize(
    "value", [None, "x" * 41, "2026-10-20T00:00:00", "2026-10-20T00:00:00+00:00"]
)
def test_diagnostic_time_requires_canonical_zoned_value(value):
    with pytest.raises(ValueError):
        safe_time(value)


def test_application_source_replacement_and_unsafe_sidecar_are_unavailable(
    tmp_path, monkeypatch
):
    import app.owner_control.diagnostics as module

    args = settings(tmp_path)
    store = EventStore(args.event_db)
    real = module._sidecars
    calls = [0]

    def changed(path):
        calls[0] += 1
        return ((1, 1), None) if calls[0] == 1 else ((1, 2), None)

    monkeypatch.setattr(module, "_sidecars", changed)
    assert application_replies(store)["available"] is False
    monkeypatch.setattr(module, "_sidecars", real)
    sidecar = args.event_db + "-wal"
    with open(sidecar, "w") as handle:
        handle.write(CANARY)
    os.chmod(sidecar, 0o644)
    try:
        with pytest.raises(ValueError):
            _sidecars(store.database_path)
        assert application_replies(store)["available"] is False
    finally:
        os.unlink(sidecar)


def test_application_rejects_replaced_symlink(tmp_path):
    args = settings(tmp_path)
    store = EventStore(args.event_db)
    moved = tmp_path / "original.db"
    os.rename(args.event_db, moved)
    os.symlink(moved, args.event_db)
    assert application_replies(store) == {"available": False, "rows": []}


@pytest.mark.parametrize(
    "outcome,expected,reason",
    [
        ("completed", "completed", None),
        ("emergency_stop", "stopped", None),
        ("invalid_proposal", "rejected", "invalid_proposal"),
        ("refused", "rejected", "refused"),
        ("invalid_response", "rejected", "invalid_response"),
        ("timeout", "uncertain", "timeout"),
        ("connection_error", "uncertain", "unknown"),
        (GatewayOutcome.AUTHENTICATION_ERROR.value, "rejected", "authorization"),
        (GatewayOutcome.PERMISSION_ERROR.value, "rejected", "authorization"),
        (GatewayOutcome.SERVER_ERROR.value, "uncertain", "provider_error"),
        ("spend_suspended", "stopped", "spend_limit"),
        (GatewayOutcome.SPEND_LIMIT_CONFIRMED.value, "stopped", "spend_limit"),
        (GatewayOutcome.BILLING_QUOTA.value, "stopped", "spend_limit"),
        ("unrecognized", "unknown", "unknown"),
    ],
)
def test_worker_outcome_categories(tmp_path, outcome, expected, reason):
    db, _, _ = sources(tmp_path)
    ProviderStateStore(EventStore(db)).record_attempt(attempt("synthetic:1"))
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE provider_usage_attempts SET outcome=?, rejection_reason=NULL",
            (outcome,),
        )
    row = _diagnostics(db)["rows"][0]
    assert (row["outcome"], row["reason"]) == (expected, reason)
    assert row["tokens"] == "15"


def test_worker_incomplete_usage_and_invalid_fields(tmp_path):
    db, _, _ = sources(tmp_path)
    ProviderStateStore(EventStore(db)).record_attempt(attempt("synthetic:1"))
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE provider_usage_attempts SET outcome='completed', total_tokens=NULL, cost_estimate_status='usage_incomplete'"
        )
    assert _diagnostics(db)["rows"][0]["reason"] == "incomplete_usage"
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE provider_usage_attempts SET total_tokens='bad'")
    assert _diagnostics(db)["available"] is False
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE provider_usage_attempts SET total_tokens=NULL, cost_estimate_status='bad'"
        )
    assert _diagnostics(db)["available"] is False


def test_worker_recent_rows_are_bounded_and_newest_first(tmp_path):
    db, _, _ = sources(tmp_path)
    provider = ProviderStateStore(EventStore(db))
    for index in range(13):
        provider.reserve_attempt(
            f"synthetic:{index}",
            f"correlation-{index}",
            datetime(2026, 10, 20, tzinfo=UTC) + timedelta(minutes=index),
        )
    rows = _diagnostics(db)["rows"]
    assert len(rows) == 10
    assert [row["time"] for row in rows] == sorted(
        (row["time"] for row in rows), reverse=True
    )
    assert rows[0]["reference"] == reference("correlation-12")
    assert rows[-1]["reference"] == reference("correlation-3")


def test_worker_replaced_source_is_unavailable(tmp_path, monkeypatch):
    import app.trusted.worker_telemetry as module

    db, _, _ = sources(tmp_path)
    real = module.protected_state_identity
    calls = [0]

    def changed(path):
        calls[0] += 1
        return real(path) if calls[0] == 1 else (0, 0)

    monkeypatch.setattr(module, "protected_state_identity", changed)
    assert _diagnostics(db)["available"] is False


@pytest.mark.parametrize(
    "payload",
    [
        b"x" * (MAX_DIAGNOSTICS_RESPONSE + 1),
        b'{"version":"worker-diagnostics-v1","available":false,"rows":[1]}',
        b'{"version":"worker-diagnostics-v1","available":true,"rows":[{}]}',
        b'{"version":"worker-diagnostics-v1","available":true,"rows":[],"rows":[]}',
    ],
)
def test_diagnostics_protocol_rejects_malformed_payloads(payload):
    with pytest.raises((ValueError, TypeError)):
        decode_diagnostics(payload)


def test_diagnostics_protocol_rejects_private_text_in_time():
    row = {
        "time": CANARY,
        "family": "provider_attempt",
        "outcome": "completed",
        "reason": None,
        "tokens": "1",
        "reference": None,
    }
    with pytest.raises(ValueError):
        decode_diagnostics(
            json.dumps(
                {"version": "worker-diagnostics-v1", "available": True, "rows": [row]}
            ).encode()
        )
