"""Synthetic owner console and INT-22 private runtime integration."""

from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
import multiprocessing
import runpy
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

from acceptance.guards import install
from app.core.provider_contract import CostEstimateStatus
from app.core.provider_spend import ProviderAttemptRecord, ProviderStateStore
from app.core.store import EventStore
from app.ops.int26_owner_pilot import build_application, main
from app.owner_control.store import OwnerStore
from app.owner_control.wsgi import OwnerApplication
from app.private_web.cli import IPv6WSGIServer
from app.private_web.wsgi import PrivateWebConfig
from app.privacy.ledger import PrivacyLedger
from app.trusted.private_pilot import PilotAllowance, load_activation
from test_int19_worker_runtime import _app
from test_int22_pilot import activation_file
from test_int22_rehearsal import _finish, _start


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    install(monkeypatch, local_ipc=True)


def settings(tmp_path):
    owner = OwnerStore(tmp_path / "owner")
    owner.provision("operator", "synthetic-owner-password")
    EventStore(tmp_path / "events.db")
    EventStore(tmp_path / "ipc-state.db")
    PrivacyLedger(tmp_path / "ledger.db")
    return SimpleNamespace(
        owner_directory=str(tmp_path / "owner"),
        event_db=str(tmp_path / "events.db"),
        ipc_state_db=str(tmp_path / "ipc-state.db"),
        privacy_ledger=str(tmp_path / "ledger.db"),
        listen_host="127.0.0.1",
        port=18026,
        allowed_host="owner.test",
        allowed_origin="https://owner.test",
        participant_origin="https://chat.test",
        allowed_client=["127.0.0.1"],
        trusted_proxy=[],
    )


def call(
    app, path="/status", method="GET", *, cookie="", csrf="", payload=None, **changes
):
    raw = json.dumps(payload).encode() if payload is not None else b""
    env = {
        "PATH_INFO": path,
        "REQUEST_METHOD": method,
        "QUERY_STRING": "",
        "HTTP_HOST": "owner.test",
        "HTTP_ORIGIN": "https://owner.test",
        "REMOTE_ADDR": "127.0.0.1",
        "HTTP_COOKIE": cookie,
        "HTTP_X_CSRF_TOKEN": csrf,
        "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": BytesIO(raw),
    }
    env.update(changes)
    response = []
    body = b"".join(
        app(env, lambda status, headers: response.append((status, headers)))
    )
    code = int(response[0][0].split()[0])
    if path in {"/", "/owner.js", "/owner.css"} and code == 200:
        return code, body.decode(), response[0][1]
    return code, json.loads(body), response[0][1]


def login(app):
    _, challenge, headers = call(app, "/login")
    login_cookie = next(
        value.split(";")[0] for key, value in headers if key == "Set-Cookie"
    )
    status, result, headers = call(
        app,
        "/login",
        "POST",
        cookie=login_cookie,
        payload={
            "username": "operator",
            "password": "synthetic-owner-password",
            "csrf": challenge["csrf"],
        },
    )
    assert status == 200
    session_cookie = next(
        value.split(";")[0]
        for key, value in headers
        if key == "Set-Cookie" and value.startswith("ar_owner_session=")
    )
    return session_cookie, result["csrf"]


def change(app, cookie, csrf, active):
    code, operation, _ = call(app, "/operation", "POST", cookie=cookie, csrf=csrf)
    assert code == 200
    return call(
        app,
        "/stop",
        "POST",
        cookie=cookie,
        csrf=csrf,
        payload={"operation_id": operation["operation_id"], "active": active},
    )


def test_pilot_console_admission_status_assets_and_stop(tmp_path):
    args = settings(tmp_path)
    ipc = ProviderStateStore(EventStore(args.ipc_state_db))
    ipc.record_attempt(
        ProviderAttemptRecord(
            attempt_id="synthetic-ipc-attempt",
            correlation_id="synthetic-correlation",
            provider="openai",
            model_identifier="synthetic-model",
            provider_config_version="synthetic-config",
            prompt_version="synthetic-prompt",
            core_self_version="synthetic-self",
            provider_response_id=None,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            duration_ms=1,
            input_tokens=None,
            cached_input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            outcome="confirmed_spend_limit",
            retry_count=0,
            estimated_cost=None,
            pricing_version=None,
            currency=None,
            cost_estimate_status=CostEstimateStatus.USAGE_UNAVAILABLE,
            provider_error_code="project_spend_limit_exceeded",
        ),
        confirmed_spend_limit_code="project_spend_limit_exceeded",
    )
    app = build_application(args)
    assert call(app, "/")[0] == 200
    assert "Частный пилот" in call(app, "/")[1]
    assert "fetch(" in call(app, "/owner.js")[1]
    assert "font-family" in call(app, "/owner.css")[1]
    assert call(app, "/", "POST")[0] == 405
    assert call(app, "/", HTTP_HOST="chat.test")[0] == 400
    assert call(app, "/login", REMOTE_ADDR="127.0.0.2")[0] == 403
    assert call(app)[0] == 401
    cookie, csrf = login(app)
    status = call(app, cookie=cookie)[1]
    assert call(app, "/status", "POST", cookie=cookie)[0] == 200
    assert status["scope"] == "private_pilot_application"
    assert status["application_stop"] == {"active": True, "scope": "application_only"}
    assert status["application_provider"]["scope"] == "application_ipc_state"
    assert status["worker"] == {"stop": "unavailable", "accounting": "unavailable"}
    assert status["application_provider"]["attempt_count"] == 1
    assert status["application_provider"]["spend_suspended"] is True
    assert status["application_provider"]["suspension_usage"]["total_tokens"] is None
    assert "synthetic-ipc-attempt" not in json.dumps(status)
    assert status["audit"] == []
    assert call(app, "/operation", "POST", cookie=cookie, csrf="bad")[0] == 403
    assert change(app, cookie, csrf, False)[0] == 200
    assert not OwnerStore(args.owner_directory).active()
    assert change(app, cookie, csrf, True)[0] == 200
    assert OwnerStore(args.owner_directory).active()
    audit = call(app, cookie=cookie)[1]["audit"]
    assert len(audit) == 3
    assert {item["action_type"] for item in audit} == {
        "stop",
        "resume",
        "session_control",
    }
    script = call(app, "/owner.js")[1]
    assert "stop: 'включение Stop'" in script
    assert "resume: 'снятие Stop'" in script
    assert call(app, "/logout", "POST", cookie=cookie, csrf=csrf)[0] == 200
    assert call(app, cookie=cookie)[0] == 401


def test_documented_proxy_source_admission(tmp_path):
    args = settings(tmp_path)
    args.allowed_client = ["192.0.2.25"]
    args.trusted_proxy = ["127.0.0.1"]
    app = build_application(args)
    assert call(app, "/login", HTTP_X_FORWARDED_FOR="192.0.2.25")[0] == 200
    assert call(app, "/login", HTTP_X_FORWARDED_FOR="127.0.0.2")[0] == 403
    assert call(app, "/login")[0] == 400
    assert (
        call(app, "/login", REMOTE_ADDR="127.0.0.2", HTTP_X_FORWARDED_FOR="192.0.2.25")[
            0
        ]
        == 403
    )


def test_invalid_configuration_does_not_create_stores(tmp_path):
    args = settings(tmp_path)
    args.listen_host = "0.0.0.0"
    with pytest.raises(ValueError, match="loopback"):
        build_application(args)
    args.listen_host = "127.0.0.1"
    args.allowed_origin = args.participant_origin
    with pytest.raises(ValueError, match="separate"):
        build_application(args)
    args.allowed_origin = "http://owner.test"
    with pytest.raises(ValueError, match="separate"):
        build_application(args)
    args.allowed_origin = "https://wrong.test"
    with pytest.raises(ValueError, match="separate"):
        build_application(args)
    args.allowed_origin = "https://owner.test"
    for participant in (
        "http://chat.test",
        "https:",
        "https://chat.test/path",
        "https://chat.test?x=1",
        "https://chat.test#fragment",
        "https://OWNER.test",
        "https://owner.test:443",
        "https://owner.test:0443",
        "https://%6Fwner.test",
    ):
        args.participant_origin = participant
        with pytest.raises(ValueError, match="separate"):
            build_application(args)
    args.participant_origin = "https://chat.test"
    args.participant_origin = "https://owner.test:444"
    assert build_application(args)
    args.participant_origin = "https://chat.test"
    Path(args.ipc_state_db).unlink()
    with pytest.raises(ValueError, match="missing"):
        build_application(args)
    assert not Path(args.ipc_state_db).exists()
    EventStore(args.ipc_state_db)
    Path(args.ipc_state_db).chmod(0o644)
    with pytest.raises(ValueError, match="unsafe"):
        build_application(args)
    Path(args.ipc_state_db).chmod(0o600)
    args.ipc_state_db = args.event_db
    with pytest.raises(ValueError, match="distinct"):
        build_application(args)
    args.ipc_state_db = str(tmp_path / "ipc-state.db")
    empty_event = tmp_path / "empty-event.db"
    empty_event.touch(mode=0o600)
    args.event_db = str(empty_event)
    with pytest.raises(ValueError, match="unavailable or unsafe"):
        build_application(args)
    assert empty_event.stat().st_size == 0
    args.event_db = str(tmp_path / "events.db")
    empty_ledger = tmp_path / "empty-ledger.db"
    empty_ledger.touch(mode=0o600)
    args.privacy_ledger = str(empty_ledger)
    with pytest.raises(ValueError, match="not initialized"):
        build_application(args)
    assert empty_ledger.stat().st_size == 0
    args.privacy_ledger = str(tmp_path / "ledger.db")
    from app.ops import int26_owner_pilot

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            int26_owner_pilot,
            "OwnerStore",
            lambda _path: (_ for _ in ()).throw(
                RuntimeError("synthetic database detail")
            ),
        )
        with pytest.raises(ValueError, match="unavailable or unsafe"):
            build_application(args)


def test_pilot_sessions_csrf_and_operation_replay(tmp_path):
    args = settings(tmp_path)
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    owner = OwnerStore(args.owner_directory, clock=lambda: now[0])
    app = OwnerApplication(
        owner,
        EventStore(args.event_db),
        PrivateWebConfig(
            frozenset({args.allowed_host}), frozenset({args.allowed_origin})
        ),
        privacy_ledger=PrivacyLedger(args.privacy_ledger),
        pilot_provider_events=EventStore(args.ipc_state_db),
    )
    assert call(app, cookie="ar_session=forged")[0] == 401
    cookie, csrf = login(app)
    assert call(app, "/operation", "POST", cookie=cookie, csrf="forged")[0] == 403
    operation = call(app, "/operation", "POST", cookie=cookie, csrf=csrf)[1][
        "operation_id"
    ]
    payload = {"operation_id": operation, "active": False}
    assert (
        call(app, "/stop", "POST", cookie=cookie, csrf=csrf, payload=payload)[0] == 200
    )
    assert (
        call(
            app,
            "/stop",
            "POST",
            cookie=cookie,
            csrf=csrf,
            payload={**payload, "active": True},
        )[0]
        == 409
    )
    assert not owner.active()
    owner.revoke(cookie.split("=", 1)[1])
    assert call(app, "/operation", "POST", cookie=cookie, csrf=csrf)[0] == 401
    cookie, _ = login(app)
    now[0] += timedelta(hours=1)
    assert call(app, cookie=cookie)[0] == 401


def test_launcher_uses_validated_composition(tmp_path, monkeypatch, capsys):
    args = settings(tmp_path)
    values = [
        "--event-db",
        args.event_db,
        "--ipc-state-db",
        args.ipc_state_db,
        "--privacy-ledger",
        args.privacy_ledger,
        "--owner-directory",
        args.owner_directory,
        "--allowed-host",
        args.allowed_host,
        "--allowed-origin",
        args.allowed_origin,
        "--participant-origin",
        args.participant_origin,
        "--allowed-client",
        "127.0.0.1",
        "--trusted-proxy",
        "127.0.0.1",
    ]
    seen = []

    class Server:
        def serve_forever(self):
            seen.append("served")

    def factory(host, port, application, **options):
        seen.append((host, port, isinstance(application, OwnerApplication), options))
        return Server()

    assert main(values, server_factory=factory) == 0
    assert seen == [("127.0.0.1", 18026, True, {}), "served"]
    seen.clear()
    assert main([*values, "--listen-host", "::1"], server_factory=factory) == 0
    assert seen == [("::1", 18026, True, {"server_class": IPv6WSGIServer}), "served"]
    assert "Owner console" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main([*values, "--listen-host", "0.0.0.0"], server_factory=factory)
    monkeypatch.setattr(
        "app.ops.int26_owner_pilot.build_application",
        lambda _args: (_ for _ in ()).throw(ValueError("synthetic")),
    )
    with pytest.raises(SystemExit):
        main(values, server_factory=factory)
    monkeypatch.setattr("sys.argv", ["int26"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("app.ops.int26_owner_pilot", run_name="__main__")
    assert exit_info.value.code == 2


def test_console_stop_controls_int22_wsgi_before_sdk_and_on_release(tmp_path):
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
    _auth, events, ledger, gate, private_app, alice, _bob = _app(tmp_path, socket)
    owner = OwnerStore(tmp_path / "owner")
    owner.provision("operator", "synthetic-owner-password")
    private_app._conversations._stop = owner.active
    owner_app = OwnerApplication(
        owner,
        events,
        PrivateWebConfig(frozenset({"owner.test"}), frozenset({"https://owner.test"})),
        privacy_ledger=ledger,
        pilot_provider_events=EventStore(tmp_path / "ipc-state.db"),
    )
    cookie, csrf = login(owner_app)
    entered, release = multiprocessing.Event(), multiprocessing.Event()
    paths = (
        str(socket),
        str(worker_dir / "provider.sqlite3"),
        str(allowance),
        str(path),
        str(stop),
    )
    process, pipe = _start(paths, 1, entered=entered, release=release)
    assert (
        json.loads(alice.submit("closed", "Синтетический вопрос")[2])["model_state"]
        != "completed"
    )
    assert not entered.is_set()
    assert PilotAllowance(allowance, load_activation(path)).remaining() == 5
    assert change(owner_app, cookie, csrf, False)[0] == 200
    results = []
    thread = Thread(
        target=lambda: results.append(
            alice.submit("flight", "Второй синтетический вопрос")
        )
    )
    thread.start()
    try:
        assert entered.wait(5)
        assert change(owner_app, cookie, csrf, True)[0] == 200
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert (
        results[0][0] != "201 Created"
        or json.loads(results[0][2])["model_state"] != "completed"
    )
    assert len(_finish(process, pipe)) == 1
    assert gate.reconcile()
    history = json.loads(alice.request("GET", "/api/history")[2])
    assert not any(item["role"] == "agent" for item in history["messages"])
    assert (
        ProviderStateStore(EventStore(tmp_path / "ipc-state.db")).usage_attempt_count()
        >= 0
    )
