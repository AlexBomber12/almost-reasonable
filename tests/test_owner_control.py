"""Synthetic owner boundary tests; no external effects."""

from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.store import EventStore
from app.core.provider_spend import ProviderStateStore
from app.owner_control.store import OwnerStore
from app.owner_control.wsgi import OwnerApplication
from app.private_web.auth import AuthStore, LoginStatus
from app.private_web.wsgi import PrivateWebConfig


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("owner control attempted external effects or credentials")

    for name in (
        "socket.socket",
        "socket.create_connection",
        "socket.getaddrinfo",
        "app.trusted.openai_transport.OpenAIResponsesTransport.from_environment",
        "app.trusted.openai_credential_files.read_protected_worker_file",
        "app.trusted.openai_credential_files.build_worker_openai_transport",
    ):
        monkeypatch.setattr(name, forbidden)


@pytest.fixture
def boundary(tmp_path):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    store = OwnerStore(tmp_path / "owner", clock=lambda: now[0])
    store.provision("operator", "synthetic-owner-password")
    events = EventStore(tmp_path / "events.sqlite")
    app = OwnerApplication(
        store,
        events,
        PrivateWebConfig(
            frozenset({"owner.test"}),
            frozenset({"https://owner.test"}),
            max_body_bytes=8192,
        ),
        allowed_clients=frozenset({"127.0.0.1", "127.0.0.2"}),
    )
    return store, app, now, events


def request(
    app, path="/status", method="GET", token="", csrf="", payload=None, **overrides
):
    raw = json.dumps(payload).encode()
    env = dict(
        HTTP_HOST="owner.test",
        HTTP_ORIGIN="https://owner.test",
        REQUEST_METHOD=method,
        PATH_INFO=path,
        QUERY_STRING="",
        REMOTE_ADDR="127.0.0.1",
        HTTP_COOKIE=f"ar_owner_session={token}",
        HTTP_X_CSRF_TOKEN=csrf,
        CONTENT_TYPE="application/json",
        CONTENT_LENGTH=str(len(raw)),
    )
    env["wsgi.input"] = BytesIO(raw)
    env.update(overrides)
    responses = []
    body = b"".join(
        app(env, lambda status, headers: responses.append((status, headers)))
    )
    return int(responses[0][0].split()[0]), json.loads(body), responses[0][1], env


def login(store):
    result = store.login("operator", "synthetic-owner-password", "127.0.0.1")
    assert result.status is LoginStatus.SUCCESS
    return result


def test_independent_authentication_expiry_revocation_and_throttling(
    boundary, tmp_path
):
    store, app, now, _ = boundary
    participant = AuthStore(tmp_path / "participant.sqlite")
    participant.provision_account("operator", "synthetic-owner-password", "owner")
    foreign = participant.login("operator", "synthetic-owner-password", "local")
    assert request(app, token=foreign.session_token)[0] == 401
    assert request(app, HTTP_COOKIE=f"ar_session={foreign.session_token}")[0] == 401
    result = login(store)
    assert request(app, token=result.session_token)[0] == 200
    now[0] += timedelta(hours=1)
    assert request(app, token=result.session_token)[0] == 401
    result = login(store)
    store.revoke(result.session_token)
    assert request(app, token=result.session_token)[0] == 401
    for _ in range(5):
        assert store.login("operator", "wrong", "local").status is LoginStatus.INVALID
    assert (
        store.login("operator", "synthetic-owner-password", "local").status
        is LoginStatus.LOCKED
    )


def test_stop_restart_replay_and_conflict(boundary):
    store, app, _, _ = boundary
    result = login(store)
    token, csrf = result.session_token, result.csrf_token
    challenge = request(app, "/operation", "POST", token, csrf)[1]["operation_id"]
    assert (
        request(
            app,
            "/stop",
            "POST",
            token,
            csrf,
            {"operation_id": challenge, "active": True},
        )[0]
        == 200
    )
    assert store.active()
    assert (
        request(
            app,
            "/stop",
            "POST",
            token,
            csrf,
            {"operation_id": challenge, "active": True},
        )[0]
        == 200
    )
    assert (
        request(
            app,
            "/stop",
            "POST",
            token,
            csrf,
            {"operation_id": challenge, "active": False},
        )[0]
        == 409
    )
    reopened = OwnerStore(store.directory)
    assert reopened.active()
    assert len(reopened.audit()) == 2
    calls = []
    if not reopened.active():
        calls.append("synthetic execution")
    assert calls == []
    challenge = request(app, "/operation", "POST", token, csrf)[1]["operation_id"]
    assert (
        request(
            app,
            "/stop",
            "POST",
            token,
            csrf,
            {"operation_id": challenge, "active": False},
        )[0]
        == 200
    )
    status = request(app, token=token)[1]
    assert not status["stop_active"]
    assert not any(status["live_effects"].values())
    assert status["stop_integration"] == "staged_not_global"


@pytest.mark.parametrize(
    "override,code",
    [
        ({"HTTP_HOST": "evil.test"}, 400),
        ({"HTTP_ORIGIN": "https://evil.test"}, 403),
        ({"HTTP_X_CSRF_TOKEN": ""}, 403),
        ({"HTTP_X_CSRF_TOKEN": "wrong"}, 403),
        ({"CONTENT_LENGTH": "9000"}, 413),
        ({"CONTENT_LENGTH": "bad"}, 400),
        ({"CONTENT_LENGTH": "-1"}, 400),
        ({"CONTENT_LENGTH": ""}, 411),
        ({"CONTENT_LENGTH": "100"}, 400),
        ({"CONTENT_TYPE": "text/plain"}, 415),
        ({"QUERY_STRING": "password=secret"}, 400),
    ],
)
def test_invalid_mutations(boundary, override, code):
    store, app, _, _ = boundary
    result = login(store)
    assert (
        request(
            app,
            "/stop",
            "POST",
            result.session_token,
            result.csrf_token,
            {"operation_id": "spoof", "active": False},
            **override,
        )[0]
        == code
    )
    assert store.active()


def test_unauthorized_spoofing_and_diagnostics(boundary):
    store, app, _, events = boundary
    assert (
        request(
            app, "/stop", "POST", payload={"is_owner": True, "model_output": "resume"}
        )[0]
        == 401
    )
    result = login(store)
    status, body, headers, _ = request(app, token=result.session_token)
    assert status == 200
    assert (
        body["provider"]["attempt_count"]
        == ProviderStateStore(events).usage_attempt_count()
    )
    assert body["provider"]["spend_suspended"] is False
    assert body["privacy"]["reconciliation"] == "unavailable_bounded_public_interface"
    assert "participant" not in json.dumps(body)
    assert ("Cache-Control", "no-store") in headers


def test_concurrent_operation_is_applied_once(boundary):
    store, _, _, _ = boundary
    result = login(store)
    operation = store.issue(result.session_token)
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(
            pool.map(
                lambda _: store.change(result.session_token, operation, False), range(4)
            )
        )
    assert statuses == ["applied"] * 4
    assert len(store.audit()) == 1


def test_http_login_logout_and_cookie_protection(boundary):
    store, app, _, _ = boundary
    _, initial, headers, _ = request(app, "/login")
    cookie = next(value.split(";")[0] for key, value in headers if key == "Set-Cookie")
    payload = {
        "username": "operator",
        "password": "synthetic-owner-password",
        "csrf": initial["csrf"],
    }
    for wrong in [None, "", "wrong"]:
        assert (
            request(
                app,
                "/login",
                "POST",
                payload={**payload, "csrf": wrong},
                HTTP_COOKIE=cookie,
            )[0]
            == 403
        )
    assert request(app, "/login", "POST", payload=payload)[0] == 403
    assert (
        request(
            app,
            "/login",
            "POST",
            payload={**payload, "password": None},
            HTTP_COOKIE=cookie,
        )[0]
        == 401
    )
    _, initial, headers, _ = request(app, "/login")
    cookie = next(value.split(";")[0] for key, value in headers if key == "Set-Cookie")
    payload["csrf"] = initial["csrf"]
    status, body, headers, _ = request(
        app, "/login", "POST", payload=payload, HTTP_COOKIE=cookie
    )
    assert request(app, "/login", "POST", payload=payload, HTTP_COOKIE=cookie)[0] == 403
    assert status == 200
    session_cookie = next(
        value
        for key, value in headers
        if key == "Set-Cookie" and value.startswith("ar_owner_session=")
    )
    assert all(
        flag in session_cookie for flag in ("Secure", "HttpOnly", "SameSite=Strict")
    )
    token = session_cookie.split(";")[0].split("=")[1]
    assert request(app, "/logout", "POST", token, body["csrf"])[0] == 200
    assert store.authenticate(token) is None
    for _ in range(4):
        _, initial, headers, _ = request(app, "/login")
        cookie = next(
            value.split(";")[0] for key, value in headers if key == "Set-Cookie"
        )
        payload["csrf"] = initial["csrf"]
        assert (
            request(
                app,
                "/login",
                "POST",
                payload={**payload, "password": "wrong"},
                HTTP_COOKIE=cookie,
            )[0]
            == 401
        )
    _, initial, headers, _ = request(app, "/login")
    cookie = next(value.split(";")[0] for key, value in headers if key == "Set-Cookie")
    payload["csrf"] = initial["csrf"]
    assert request(app, "/login", "POST", payload=payload, HTTP_COOKIE=cookie)[0] == 429


def test_malformed_routes_and_sanitized_failures(boundary, monkeypatch):
    store, app, _, _ = boundary
    result = login(store)
    assert request(app, "/chat")[0] == 404
    assert request(app, "/stop", "GET")[0] == 405
    for raw in [
        b"{",
        b"[]",
        b'{"active":false,"active":true,"operation_id":"x"}',
        b"\xff",
    ]:
        assert (
            request(
                app,
                "/stop",
                "POST",
                result.session_token,
                result.csrf_token,
                CONTENT_LENGTH=str(len(raw)),
                **{"wsgi.input": BytesIO(raw)},
            )[0]
            == 400
        )
    assert (
        request(
            app,
            "/stop",
            "POST",
            result.session_token,
            result.csrf_token,
            {"operation_id": 1, "active": False},
        )[0]
        == 400
    )

    def unavailable(*args):
        raise RuntimeError("synthetic secret must not leak")

    monkeypatch.setattr(store, "audit", unavailable)
    status, body, _, _ = request(app, token=result.session_token)
    assert status == 503
    assert "secret" not in json.dumps(body)
    monkeypatch.setattr(
        store, "issue", lambda token: (_ for _ in ()).throw(PermissionError("revoked"))
    )
    assert (
        request(app, "/operation", "POST", result.session_token, result.csrf_token)[0]
        == 400
    )


def test_owner_config_and_privacy_readiness(boundary, tmp_path):
    from app.privacy.ledger import PrivacyLedger

    store, app, _, events = boundary
    with pytest.raises(ValueError):
        OwnerApplication(
            store,
            events,
            PrivateWebConfig(
                frozenset({"127.0.0.1"}),
                frozenset({"http://127.0.0.1"}),
                secure_cookies=False,
            ),
        )
    app.privacy = PrivacyLedger(tmp_path / "privacy.sqlite")
    result = login(store)
    assert (
        request(app, token=result.session_token)[1]["privacy"]["ledger_ready"] is True
    )
    app.privacy = type("InvalidLedger", (), {"control_generation": lambda self: None})()
    assert (
        request(app, token=result.session_token)[1]["privacy"]["ledger_ready"] is False
    )


def test_operation_binding_expiry_limits_and_revocation(boundary):
    store, _, now, _ = boundary
    with pytest.raises(PermissionError):
        store.issue("foreign")
    with pytest.raises(PermissionError):
        store.change("foreign", "a" * 32, False)
    result = login(store)
    token = result.session_token
    for op, active in [(None, False), ("short", False), ("a" * 32, "resume")]:
        with pytest.raises(ValueError):
            store.change(token, op, active)
    assert store.change(token, "a" * 32, False) == "rejected"
    operation = store.issue(token)
    now[0] += timedelta(minutes=5)
    assert store.change(token, operation, False) == "rejected"
    operation = store.issue(token)
    rotated = login(store)
    assert store.change(rotated.session_token, operation, False) == "rejected"
    for _ in range(32):
        store.issue(rotated.session_token)
    with pytest.raises(ValueError):
        store.issue(rotated.session_token)
    now[0] += timedelta(minutes=5)
    assert store.issue(rotated.session_token)
    assert all(len(row["operation_id"]) == 32 for row in store.audit())


def test_provisioning_cli(tmp_path):
    from app.owner_control.cli import main

    values = iter(["synthetic-owner-password"] * 2)
    directory = tmp_path / "cli"
    assert (
        main(
            ["--directory", str(directory), "--username", "operator"],
            password_reader=lambda prompt: next(values),
        )
        == 0
    )
    store = OwnerStore(directory)
    assert (
        store.login("operator", "synthetic-owner-password", "local").status
        is LoginStatus.SUCCESS
    )
    values = iter(["first-password", "different-password"])
    with pytest.raises(SystemExit, match="confirmation"):
        main(
            ["--directory", str(directory), "--username", "operator"],
            password_reader=lambda prompt: next(values),
        )
    with pytest.raises(ValueError):
        store.provision("empty", "")


def test_protected_store_files_and_schema(boundary, tmp_path):
    import os
    import sqlite3

    store, _, _, _ = boundary
    store.directory.chmod(0o750)
    with pytest.raises(PermissionError):
        OwnerStore(store.directory)
    store.directory.chmod(0o700)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE owner_schema SET version=2")
    with pytest.raises(RuntimeError, match="schema"):
        OwnerStore(store.directory)
    store.path.chmod(0o644)
    assert store.active() is True
    store.path.chmod(0o600)
    sidecar = store.path.with_name("owner.sqlite-wal")
    sidecar.write_text("synthetic")
    sidecar.chmod(0o644)
    assert store.active() is True
    sidecar.unlink()
    sidecar.symlink_to(tmp_path / "absent")
    assert store.active() is True
    sidecar.unlink()
    store.path.unlink()
    assert store.active() is True
    participant_dir = tmp_path / "participant-store"
    participant_dir.mkdir(mode=0o700)
    AuthStore(participant_dir / "owner.sqlite")
    with pytest.raises(RuntimeError, match="unavailable"):
        OwnerStore(participant_dir)
    assert os.stat(participant_dir).st_mode & 0o777 == 0o700


def test_safe_sidecar_and_inode_replacement(boundary, monkeypatch):
    from pathlib import Path
    import os

    store, _, _, _ = boundary
    sidecar = Path(str(store.path) + "-journal")
    sidecar.touch(mode=0o600)
    assert store.active()
    sidecar.unlink()
    original = Path.lstat
    reads = []

    def replaced(path):
        result = original(path)
        if path == store.path:
            reads.append(path)
            if len(reads) > 1:
                values = list(result)
                values[1] += 1
                return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "lstat", replaced)
    assert store.active()


def test_login_challenges_bounded_expiring_and_single_use(boundary):
    store, _, now, _ = boundary
    client = "127.0.0.1"
    challenge = store.login_challenge(client)
    for invalid in [
        None,
        "x" * 257,
        "malformed",
        "invalid.nonce.client.signature",
        challenge[:-1] + "x",
    ]:
        assert not store.consume_login_challenge(invalid, client)
    assert not store.consume_login_challenge(challenge, "127.0.0.2")
    assert store.consume_login_challenge(challenge, client)
    assert not store.consume_login_challenge(challenge, client)
    simultaneous = store.login_challenge(client)
    with ThreadPoolExecutor(max_workers=4) as pool:
        consumed = list(
            pool.map(
                lambda _: store.consume_login_challenge(simultaneous, client), range(4)
            )
        )
    assert sorted(consumed) == [False, False, False, True]
    for _ in range(30):
        assert store.consume_login_challenge(store.login_challenge(client), client)
    assert not store.consume_login_challenge(store.login_challenge(client), client)
    other = "127.0.0.2"
    assert store.consume_login_challenge(store.login_challenge(other), other)
    expired = store.login_challenge(client)
    now[0] += timedelta(minutes=5)
    assert not store.consume_login_challenge(expired, client)
    assert store.consume_login_challenge(store.login_challenge(client), client)


def test_confirmed_spend_limit_inspection_is_sanitized(boundary):
    from app.core.provider_contract import CostEstimateStatus
    from app.core.provider_spend import ProviderAttemptRecord

    store, app, now, events = boundary
    provider = ProviderStateStore(events)
    record = ProviderAttemptRecord(
        attempt_id="synthetic-attempt",
        correlation_id="synthetic-correlation",
        provider="openai",
        model_identifier="synthetic-model",
        provider_config_version="synthetic-config",
        prompt_version="synthetic-prompt",
        core_self_version="synthetic-self",
        provider_response_id=None,
        started_at=now[0],
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
    )
    provider.record_attempt(
        record, confirmed_spend_limit_code="project_spend_limit_exceeded"
    )
    result = login(store)
    status = request(app, token=result.session_token)[1]["provider"]
    assert status["spend_suspended"]
    assert status["attempt_count"] == 1
    assert status["suspension_usage"]["cost_estimate_status"] == "usage_unavailable"
    assert status["suspension_usage"]["total_tokens"] is None
    assert not status["billing_authority"]
    assert "synthetic-correlation" not in json.dumps(status)
    assert "synthetic-attempt" not in json.dumps(status)


def test_entrypoint_and_rejection_audit(boundary, monkeypatch):
    import runpy
    from app.owner_control import cli

    monkeypatch.setattr(cli, "main", lambda: 0)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("app.owner_control", run_name="__main__")
    assert exit_info.value.code == 0
    store, app, _, _ = boundary
    result = login(store)
    for path in ["/stop", "/operation"]:
        assert request(app, path, "POST", result.session_token, "wrong")[0] == 403
    audit = store.audit()
    assert {row["action_type"] for row in audit} == {"stop_control", "session_control"}
    assert all(row["rejection_category"] == "request_admission" for row in audit)
    with pytest.raises(PermissionError):
        store.reject("foreign", "/stop")


def test_authentication_precedes_body_and_diagnostic_access(boundary, monkeypatch):
    store, app, _, events = boundary

    class Unreadable:
        def read(self, length):
            pytest.fail("unauthenticated body was read")

    monkeypatch.setattr(
        events, "diagnostics", lambda: pytest.fail("unauthenticated diagnostics read")
    )
    assert request(app, "/stop", "POST", **{"wsgi.input": Unreadable()})[0] == 401
    assert request(app)[0] == 401
    result = login(store)
    assert (
        request(
            app,
            "/stop",
            "POST",
            result.session_token,
            "wrong",
            **{"wsgi.input": Unreadable()},
        )[0]
        == 403
    )
    assert store.active()


def test_missing_stop_row_fails_closed_without_false_success(boundary):
    import sqlite3

    store, app, _, _ = boundary
    result = login(store)
    operation = store.issue(result.session_token)
    with sqlite3.connect(store.path) as connection:
        connection.execute("DELETE FROM owner_stop")
    assert store.active()
    assert (
        request(
            app,
            "/stop",
            "POST",
            result.session_token,
            result.csrf_token,
            {"operation_id": operation, "active": False},
        )[0]
        == 503
    )
    assert store.audit() == []


def test_anonymous_challenge_flood_does_not_block_owner_login(boundary):
    import sqlite3

    store, app, _, _ = boundary
    # Reviewer interleaving: owner obtains a challenge BEFORE the anonymous flood.
    status, challenge, headers, _ = request(app, "/login")
    assert status == 200
    cookie = next(value.split(";")[0] for key, value in headers if key == "Set-Cookie")
    for _ in range(256):
        assert request(app, "/login", REMOTE_ADDR="127.0.0.2")[0] == 200
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM owner_login_replays"
        ).fetchone() == (0,)
    payload = {
        "username": "operator",
        "password": "synthetic-owner-password",
        "csrf": challenge["csrf"],
    }
    status, body, _, _ = request(
        app, "/login", "POST", payload=payload, HTTP_COOKIE=cookie
    )
    assert status == 200
    assert body["authenticated"] is True
    assert request(app, "/login", "POST", payload=payload, HTTP_COOKIE=cookie)[0] == 403
    reopened = OwnerStore(store.directory, clock=store._clock)
    assert not reopened.consume_login_challenge(challenge["csrf"], "127.0.0.1")
    fresh = store.login_challenge("127.0.0.1")
    assert reopened.consume_login_challenge(fresh, "127.0.0.1")


@pytest.mark.parametrize(
    "password",
    ["x" * 1011, "x" * 1024, "é" * 512],
    ids=["ascii-1011", "ascii-1024", "utf8-1024"],
)
def test_full_password_byte_range_is_preserved(boundary, password):
    from app.private_web.auth import ProvisioningError

    store, _, _, _ = boundary
    store.provision("long-password-owner", password)
    assert (
        store.login("long-password-owner", password, "local").status
        is LoginStatus.SUCCESS
    )
    assert (
        store.login("long-password-owner", password + "x", "local").status
        is LoginStatus.INVALID
    )
    with pytest.raises(ProvisioningError):
        store.provision("oversized-owner", "x" * 1025)


def test_unlisted_sources_cannot_allocate_authentication_state(boundary):
    import sqlite3

    store, app, _, _ = boundary
    _, challenge, headers, _ = request(app, "/login")
    cookie = next(value.split(";")[0] for key, value in headers if key == "Set-Cookie")
    payload = {"username": "operator", "password": "wrong", "csrf": challenge["csrf"]}
    for index in range(128):
        address = f"2001:db8::{index + 1:x}"
        assert request(app, "/login", REMOTE_ADDR=address)[0] == 403
        assert (
            request(
                app,
                "/login",
                "POST",
                payload=payload,
                HTTP_COOKIE=cookie,
                REMOTE_ADDR=address,
            )[0]
            == 403
        )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM owner_login_replays"
        ).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM login_attempts").fetchone() == (
            0,
        )
    payload["password"] = "synthetic-owner-password"
    assert request(app, "/login", "POST", payload=payload, HTTP_COOKIE=cookie)[0] == 200


@pytest.mark.parametrize(
    "clients",
    [
        [],
        frozenset(),
        frozenset({None}),
        frozenset({"127.0.0.01"}),
        frozenset(f"2001:db8::{index:x}" for index in range(17)),
    ],
)
def test_owner_admission_configuration_is_finite_and_exact(boundary, clients):
    store, app, _, events = boundary
    with pytest.raises(ValueError, match="admission"):
        OwnerApplication(store, events, app.config, allowed_clients=clients)


def test_owner_admission_defaults_and_proxy_source_binding(boundary):
    store, app, _, events = boundary
    local = OwnerApplication(store, events, app.config)
    assert request(local, "/login", REMOTE_ADDR="::1")[0] == 200
    assert request(local, "/login", REMOTE_ADDR="127.0.0.2")[0] == 403
    proxy_config = PrivateWebConfig(
        frozenset({"owner.test"}),
        frozenset({"https://owner.test"}),
        trusted_proxy_addresses=frozenset({"127.0.0.1"}),
    )
    proxied = OwnerApplication(
        store, events, proxy_config, allowed_clients=frozenset({"127.0.0.2"})
    )
    assert request(proxied, "/login", HTTP_X_FORWARDED_FOR="127.0.0.2")[0] == 200
    assert request(proxied, "/login", HTTP_X_FORWARDED_FOR="2001:db8::1")[0] == 403
    assert (
        request(proxied, "/login", HTTP_X_FORWARDED_FOR="127.0.0.2,127.0.0.1")[0] == 400
    )
    # Source admission does not authorize private reads or administrative controls.
    assert request(proxied, HTTP_X_FORWARDED_FOR="127.0.0.2")[0] == 401
