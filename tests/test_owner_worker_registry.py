"""One authenticated owner command reaches each configured real local socket."""

import json
import os
from threading import Thread

import pytest

from app.core.store import EventStore
from app.owner_control.worker_registry import WorkerRegistry
from app.ops.int26_owner_pilot import build_application
from app.trusted.worker_stop import WorkerStopServer
from app.trusted.worker_telemetry import WorkerTelemetryServer
from test_int26_owner_console import call, login, settings


@pytest.fixture
def workers(tmp_path):
    specs, stops, telemetry, controls = [], [], [], []
    versions = (
        "private-pilot-emergency-stop-v1",
        "public-model-stop-v1",
        "public-research-stop-v1",
    )
    for index, version in enumerate(versions):
        folder = tmp_path / str(index)
        folder.mkdir(mode=0o700)
        path = folder / "stop.json"
        path.write_text(json.dumps({"version": version, "stop": False}))
        path.chmod(0o400)
        database = folder / "provider.db"
        EventStore(database)
        stop_socket, status_socket = folder / "s", folder / "t"
        stop = WorkerStopServer(stop_socket, path, os.geteuid(), stop_version=version)
        status = WorkerTelemetryServer(
            status_socket,
            database,
            path,
            frozenset({os.geteuid()}),
            stop_version=version,
        )
        stop.start()
        status.start()
        stops.append(stop)
        telemetry.append(status)
        controls.append(path)
        specs.append(
            {
                "name": ("private-model", "public-model", "hosted-research")[index],
                "telemetry_socket": str(status_socket),
                "stop_socket": str(stop_socket),
                "uid": os.geteuid(),
            }
        )
    config = tmp_path / "workers.json"
    config.write_text(
        json.dumps({"version": "owner-worker-registry-v1", "workers": specs})
    )
    config.chmod(0o400)
    try:
        yield config, specs, stops, telemetry, controls
    finally:
        for server in stops + telemetry:
            server.close()


def serving(servers):
    threads = [Thread(target=server.serve_once) for server in servers]
    for thread in threads:
        thread.start()
    return threads


def finished(threads):
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_one_owner_stop_covers_all_registry_targets_and_is_durable(workers, tmp_path):
    config, _, servers, telemetry, controls = workers
    assert len(WorkerRegistry.from_config(config).names) == 3
    args = settings(tmp_path)
    args.worker_registry = str(config)
    app = build_application(args)
    cookie, csrf = login(app)
    code, issued, _ = call(
        app, "/worker-stop-operation", "POST", cookie=cookie, csrf=csrf
    )
    assert code == 200
    threads = serving(servers)
    code, result, _ = call(
        app,
        "/emergency-stop",
        "POST",
        cookie=cookie,
        csrf=csrf,
        payload={"operation_id": issued["operation_id"]},
    )
    finished(threads)
    assert code == 200 and result["application"] == result["worker"] == "confirmed"
    assert set(result["workers"].values()) == {"confirmed"}
    assert all(json.loads(path.read_text())["stop"] for path in controls)
    app = build_application(args)
    threads = serving(telemetry)
    code, status, _ = call(app, "/status", cookie=cookie)
    finished(threads)
    assert code == 200 and len(status["workers"]) == 3
    assert all(
        row["status"]["stop"] == {"valid": True, "active": True}
        for row in status["workers"]
    ), status["workers"]
    code, replay, _ = call(
        app,
        "/emergency-stop",
        "POST",
        cookie=cookie,
        csrf=csrf,
        payload={"operation_id": issued["operation_id"]},
    )
    assert code == 200 and replay == result


def test_registry_operation_never_silently_changes_target_set(workers, tmp_path):
    config, specs, _, _, controls = workers
    args = settings(tmp_path)
    args.worker_registry = str(config)
    app = build_application(args)
    cookie, csrf = login(app)
    code, issued, _ = call(
        app, "/worker-stop-operation", "POST", cookie=cookie, csrf=csrf
    )
    assert code == 200
    config.chmod(0o600)
    config.write_text(
        json.dumps({"version": "owner-worker-registry-v1", "workers": specs[:2]})
    )
    config.chmod(0o400)
    changed = build_application(args)
    code, _, _ = call(
        changed,
        "/emergency-stop",
        "POST",
        cookie=cookie,
        csrf=csrf,
        payload={"operation_id": issued["operation_id"]},
    )
    assert code == 409
    assert not any(json.loads(path.read_text())["stop"] for path in controls)


def test_unavailable_worker_never_becomes_a_confirmed_global_stop(workers, tmp_path):
    config, _, servers, _, controls = workers
    servers[-1].close()
    args = settings(tmp_path)
    args.worker_registry = str(config)
    app = build_application(args)
    cookie, csrf = login(app)
    _, issued, _ = call(app, "/worker-stop-operation", "POST", cookie=cookie, csrf=csrf)
    threads = serving(servers[:2])
    code, result, _ = call(
        app,
        "/emergency-stop",
        "POST",
        cookie=cookie,
        csrf=csrf,
        payload={"operation_id": issued["operation_id"]},
    )
    finished(threads)
    assert code == 200 and result["worker"] != "confirmed"
    assert result["workers"]["hosted-research"] == "unavailable"
    assert [json.loads(path.read_text())["stop"] for path in controls] == [
        True,
        True,
        False,
    ]


def test_registry_status_and_control_remain_owner_authenticated(workers, tmp_path):
    args = settings(tmp_path)
    args.worker_registry = str(workers[0])
    app = build_application(args)
    assert call(app, "/status")[0] == 401
    cookie, csrf = login(app)
    assert call(app, "/worker-stop-operation", "POST", cookie=cookie)[0] == 403
    assert (
        call(
            app,
            "/emergency-stop",
            "POST",
            cookie=cookie,
            csrf=csrf,
            payload={"operation_id": "forged"},
        )[0]
        == 400
    )


@pytest.mark.parametrize(
    "case", ["empty", "too_many", "fields", "long_name", "duplicate"]
)
def test_registry_has_closed_finite_target_shape(case):
    endpoint = {
        "name": "public",
        "telemetry_socket": "/synthetic/status",
        "stop_socket": "/synthetic/stop",
        "uid": os.geteuid(),
    }
    value = {
        "empty": [],
        "too_many": [endpoint] * 9,
        "fields": [{"name": "public"}],
        "long_name": [endpoint | {"name": "x" * 33}],
        "duplicate": [endpoint, endpoint],
    }[case]
    with pytest.raises(ValueError):
        WorkerRegistry(value)


def test_registry_rejects_unknown_config_and_client_failures_stay_uncertain(
    workers, tmp_path, monkeypatch
):
    from app.owner_control.worker_stop import WorkerStopClient
    from app.owner_control.worker_telemetry import WorkerTelemetryClient

    registry = WorkerRegistry.from_config(workers[0])

    def fail(_self):
        raise RuntimeError("synthetic endpoint failure")

    monkeypatch.setattr(WorkerTelemetryClient, "read", fail)
    assert not any(row["status"]["available"] for row in registry.read())
    monkeypatch.setattr(
        WorkerStopClient, "assert_stop", lambda _self: "unknown-future-result"
    )
    assert set(registry.assert_all({}).values()) == {"uncertain"}
    monkeypatch.setattr(WorkerStopClient, "assert_stop", fail)
    result = registry.assert_all({registry.names[0]: "confirmed"})
    assert (
        result[registry.names[0]] == "confirmed"
        and list(result.values()).count("uncertain") == 2
    )
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"version":"unknown","workers":[]}')
    invalid.chmod(0o400)
    with pytest.raises(ValueError, match="config"):
        WorkerRegistry.from_config(invalid)


def test_stop_protocol_never_accepts_unknown_control_formats(workers, tmp_path):
    from app.trusted.worker_stop import assert_stop, WorkerStop
    from app.trusted.worker_telemetry import _stop

    config, _, servers, telemetry, controls = workers
    with pytest.raises(ValueError):
        assert_stop(controls[0], stop_version="unknown")
    assert _stop(controls[0], "unknown") == {"valid": False, "active": None}
    with pytest.raises(ValueError):
        WorkerStopServer(
            servers[0].socket_path, controls[0], os.geteuid(), stop_version="unknown"
        )
    with pytest.raises(ValueError):
        WorkerTelemetryServer(
            telemetry[0].socket_path,
            telemetry[0].provider_db,
            controls[0],
            frozenset({os.geteuid()}),
            stop_version="unknown",
        )
    with pytest.raises(ValueError):
        WorkerStop(tmp_path / "unused", "unknown")
    assert WorkerStop(tmp_path / "missing", "public-model-stop-v1").active()
    assert config.exists()


def test_owner_registry_results_cannot_invent_target_acknowledgements(
    workers, tmp_path
):
    args = settings(tmp_path)
    args.worker_registry = str(workers[0])
    app = build_application(args)
    cookie, csrf = login(app)
    _, issued, _ = call(app, "/worker-stop-operation", "POST", cookie=cookie, csrf=csrf)
    token = cookie.split("=", 1)[1]
    for value in (
        {"not-configured": "confirmed"},
        {name: "unknown" for name in app.workers.names},
    ):
        with pytest.raises(ValueError, match="outcome"):
            app.store.record_worker_targets(token, issued["operation_id"], value)
    assert (
        app.store.worker_registry_operation(token, issued["operation_id"])["results"]
        == {}
    )
