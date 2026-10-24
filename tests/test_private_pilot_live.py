"""Credential-free checks of the live operator flow and durable accounting."""

from datetime import UTC, datetime, timedelta
import hashlib
from http.cookiejar import Cookie
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import stat
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from acceptance import private_pilot_live as live
from acceptance.private_pilot_conversation import ASK, FIRST, MARS, run_ship_journey
from acceptance.private_pilot_live import (
    MODEL,
    TASK,
    reserve_aggregate,
    validate_authorization,
)


def _response(body):
    return BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())


def _cookie(jar, name, value):
    jar.set_cookie(
        Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain="chat.test",
            domain_specified=False,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
    )


def test_https_participant_decodes_session_and_api_but_not_login(monkeypatch):
    responses = iter(
        [
            b"<html>login</html>",
            {"authenticated": True, "username": "synthetic-user"},
            {"messages": []},
        ]
    )
    participant = live.HTTPSParticipant("https://chat.test", "synthetic-user")
    requests = []

    def open_request(request, *, timeout):
        requests.append((request.full_url, request.get_method(), timeout))
        return _response(next(responses))

    participant.opener = SimpleNamespace(open=open_request)
    assert participant.request("GET", "/login") is None
    assert participant.request("GET", "/session") == {
        "authenticated": True,
        "username": "synthetic-user",
    }
    assert participant.request("GET", "/api/history") == {"messages": []}
    assert requests == [
        ("https://chat.test/login", "GET", 55),
        ("https://chat.test/session", "GET", 55),
        ("https://chat.test/api/history", "GET", 55),
    ]


@pytest.mark.parametrize("body", [b"<html>wrong route</html>", b"{broken"])
def test_https_participant_rejects_non_json_session(body):
    participant = live.HTTPSParticipant("https://chat.test", "synthetic-user")
    participant.opener = SimpleNamespace(open=lambda *_args, **_kwargs: _response(body))
    with pytest.raises(json.JSONDecodeError):
        participant.request("GET", "/session")


def test_worker_readiness_probes_short_socket_inside_running_app(tmp_path, monkeypatch):
    host_path = (
        tmp_path
        / ("long-state-directory-" * 5)
        / "runs"
        / ("a" * 32)
        / "ipc"
        / "worker.sock"
    )
    assert len(os.fsencode(host_path)) > 107
    calls = []

    def app_python(_env, code, *, running, timeout):
        calls.append((code, running, timeout))

    monkeypatch.setattr(live, "_app_python", app_python)
    live._wait_worker_ready({})
    assert len(calls) == 1
    assert "/run/pilot-ipc/worker.sock" in calls[0][0]
    assert str(host_path) not in calls[0][0]
    assert calls[0][1] is True
    assert 0 < calls[0][2] <= 2

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("private worker detail must not enter report")

    monkeypatch.setattr(live, "_app_python", unavailable)
    clock = iter((0, 0, 16))
    monkeypatch.setattr(live.time, "monotonic", lambda: next(clock))
    with pytest.raises(live.BackendReadinessError, match="worker socket"):
        live._wait_worker_ready({})


def test_app_python_uses_exec_only_for_running_fixed_ip_app(monkeypatch):
    calls = []

    def compose(_env, *args, **kwargs):
        calls.append((args, kwargs))
        return ""

    monkeypatch.setattr(live, "_compose", compose)
    live._app_python({}, "print('before')", running=False)
    live._app_python({}, "print('during')", "secret\n", running=True)
    live._app_python({}, "print('after')", running=False)
    assert calls[0][0][:5] == ("run", "--rm", "--no-deps", "-i", "private-app")
    assert calls[1][0][:3] == ("exec", "-T", "private-app")
    assert calls[1][1]["input_text"] == "secret\n"
    assert calls[2][0][:5] == calls[0][0][:5]


def test_owner_change_passes_password_by_stdin_for_running_app(monkeypatch):
    calls = []

    def app_python(_env, code, input_text=None, *, running, timeout):
        calls.append((code, input_text, running, timeout))

    monkeypatch.setattr(live, "_app_python", app_python)
    live._owner_change({}, "synthetic-password", False, running=True)
    assert len(calls) == 1
    assert calls[0][1:] == ("synthetic-password\n", True, 120)
    assert "False" in calls[0][0]


def test_effective_compose_keeps_static_backend_on_internal_network(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose unavailable")
    env = os.environ.copy()
    env.update(
        {
            "PILOT_SOCKET_DIR": str(tmp_path / "ipc"),
            "PILOT_WORKER_STATE_DIR": str(tmp_path / "worker"),
            "PILOT_CONTROL_DIR": str(tmp_path / "control"),
            "PILOT_SECRET_DIR": str(tmp_path / "secrets"),
            "PILOT_APP_STATE_DIR": str(tmp_path / "app"),
            "PILOT_ALLOWED_HOST": "acceptance.test",
            "PILOT_ALLOWED_ORIGIN": "https://acceptance.test",
            "PILOT_APP_SUBNET": "172.30.252.0/29",
            "PILOT_APP_IP": "172.30.252.3",
        }
    )
    result = subprocess.run(
        [*live.COMPOSE, "config", "--format", "json"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    config = json.loads(result.stdout)
    app = config["services"]["private-app"]
    assert "ports" not in app
    assert app["networks"]["app-local"]["ipv4_address"] == "172.30.252.3"
    assert set(app["networks"]) == {"app-local"}
    network = config["networks"]["app-local"]
    assert network["internal"] is True
    assert network["ipam"]["config"] == [{"subnet": "172.30.252.0/29"}]


def test_backend_conflict_check_rejects_docker_and_host_overlap(monkeypatch):
    subnet, address = live._backend_network("172.30.252.0/29", "172.30.252.3")
    assert str(address) == "172.30.252.3"
    for invalid in ("172.30.252.1", "172.30.252.8", "127.0.0.1"):
        with pytest.raises(ValueError, match="invalid acceptance backend"):
            live._backend_network("172.30.252.0/29", invalid)
    with pytest.raises(ValueError, match="invalid acceptance backend"):
        live._backend_network("not-a-subnet", "172.30.252.3")

    def commands(docker_subnet, route_subnet):
        def command(parts, _env):
            if parts[:3] == ("docker", "network", "ls"):
                return "network-id"
            if parts[:3] == ("docker", "network", "inspect"):
                return json.dumps([{"IPAM": {"Config": [{"Subnet": docker_subnet}]}}])
            return json.dumps([{"dst": route_subnet}])

        return command

    monkeypatch.setattr(live, "_command", commands("172.30.252.0/24", "10.0.0.0/8"))
    with pytest.raises(ValueError, match="Docker network"):
        live._check_backend_conflicts(subnet, {})
    monkeypatch.setattr(live, "_command", commands("172.20.0.0/16", "172.30.252.0/24"))
    with pytest.raises(ValueError, match="host route"):
        live._check_backend_conflicts(subnet, {})
    monkeypatch.setattr(live, "_command", commands("172.20.0.0/16", "10.0.0.0/8"))
    live._check_backend_conflicts(subnet, {})


NOW = datetime(2030, 1, 1, tzinfo=UTC)


def approval():
    return {
        "task": TASK,
        "commit": "synthetic-commit",
        "model": MODEL,
        "project_id": "proj_synthetic",
        "organization_id": "org-synthetic",
        "valid_from": (NOW - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valid_until": (NOW + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "billing_budget_usd": "1.00",
        "max_aggregate_operations": 6,
        "max_aggregate_input_tokens": 10000,
        "max_aggregate_output_tokens": 3072,
        "max_requests_per_run": 3,
    }


def activation():
    return {
        "model": MODEL,
        "project_id": "proj_synthetic",
        "organization_id": "org-synthetic",
        "not_before": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (NOW + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "max_attempts_per_operation": 1,
        "max_operations": 5,
    }


def test_live_authorization_requires_task_scope_limits_and_current_commit():
    validate_authorization(approval(), activation(), "synthetic-commit", NOW)
    changes = [
        ("task", "another-task"),
        ("commit", "another-commit"),
        ("model", "another-model"),
        ("billing_budget_usd", "0"),
        ("max_aggregate_operations", 0),
        ("max_aggregate_output_tokens", True),
        ("max_requests_per_run", 2),
        ("project_id", "proj_other"),
    ]
    for key, value in changes:
        changed = approval()
        changed[key] = value
        with pytest.raises(ValueError):
            validate_authorization(changed, activation(), "synthetic-commit", NOW)
    with pytest.raises(ValueError):
        validate_authorization(
            approval(), activation(), "synthetic-commit", NOW + timedelta(hours=1)
        )
    changed_activation = activation()
    changed_activation["max_attempts_per_operation"] = 2
    with pytest.raises(ValueError):
        validate_authorization(approval(), changed_activation, "synthetic-commit", NOW)
    changed_activation = activation()
    changed_activation["expires_at"] = (NOW + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with pytest.raises(ValueError, match="beyond"):
        validate_authorization(approval(), changed_activation, "synthetic-commit", NOW)


def test_aggregate_allowance_is_durable_across_run_ids_and_commit_changes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(live, "_protected_parents", lambda *_: None)
    ledger = tmp_path / "aggregate.sqlite3"
    approved = approval()
    assert reserve_aggregate(ledger, approved, "first-run") == 3
    assert reserve_aggregate(ledger, approved, "second-run") == 0
    with pytest.raises(ValueError, match="exhausted"):
        reserve_aggregate(ledger, approved, "third-run")
    altered = json.loads(json.dumps(approved))
    altered["commit"] = "new-commit"
    with pytest.raises(ValueError, match="policy changed"):
        reserve_aggregate(ledger, altered, "new-commit-run")


@pytest.fixture
def zero_call_state(tmp_path, monkeypatch):
    old_run = "a" * 32
    approved = approval()
    approved.update(
        commit="a" * 40,
        max_aggregate_operations=3,
        max_aggregate_input_tokens=6000,
        max_aggregate_output_tokens=1536,
        max_requests_per_run=3,
        billing_budget_usd="0.10",
    )
    active = activation()
    active["max_operations"] = 3
    activation_bytes = json.dumps(active).encode()
    root = tmp_path / "state"
    app = root / "runs" / old_run / "app" / "owner"
    app.mkdir(parents=True)
    worker = root / "worker"
    worker.mkdir()
    control = root / "control"
    control.mkdir()
    (control / "activation.json").write_bytes(activation_bytes)
    (control / "emergency-stop.json").write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": True})
    )
    with sqlite3.connect(app / "owner.sqlite") as db:
        db.execute("CREATE TABLE owner_stop (singleton INTEGER, active INTEGER)")
        db.execute("INSERT INTO owner_stop VALUES (1, 1)")
    with sqlite3.connect(worker / "provider.sqlite3") as db:
        db.execute("CREATE TABLE provider_usage_attempts (attempt_id TEXT)")
        db.execute("CREATE TABLE provider_attempt_reservations (attempt_id TEXT)")
    with sqlite3.connect(worker / "allowance.sqlite3") as db:
        db.execute("CREATE TABLE authority (digest TEXT, max_operations INTEGER)")
        db.execute("CREATE TABLE consumed (operation_id TEXT)")
        db.execute(
            "INSERT INTO authority VALUES (?, 3)",
            (hashlib.sha256(activation_bytes).hexdigest(),),
        )
    ledger = tmp_path / "aggregate.sqlite3"
    with sqlite3.connect(ledger) as db:
        db.execute("CREATE TABLE policy (value TEXT NOT NULL)")
        db.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, slots INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT INTO policy VALUES (?)",
            (json.dumps(approved, sort_keys=True, separators=(",", ":")),),
        )
        db.execute("INSERT INTO runs VALUES (?, 3)", (old_run,))
    args = SimpleNamespace(
        state_root=root,
        aggregate_ledger=ledger,
        previous_report=tmp_path / "old-report.json",
        report=tmp_path / "new-report.json",
        origin="https://chat.test",
        backend_subnet="172.30.253.0/29",
        backend_ip="172.30.253.3",
    )
    monkeypatch.setattr(live, "_protected", lambda *_: None)
    monkeypatch.setattr(live, "_protected_parents", lambda *_: None)
    monkeypatch.setattr(live, "_checked_directory", lambda *_: None)
    monkeypatch.setattr(live, "_read_protected", lambda path, *_: path.read_bytes())
    monkeypatch.setattr(
        live,
        "_compose",
        lambda _env, *parts: (
            "private-app\nprovider-worker"
            if parts == ("ps", "--all", "--services")
            else ""
        ),
    )
    return args, approved, activation_bytes, old_run


def test_zero_call_continuation_reuses_only_original_reservation(zero_call_state):
    args, approved, activation_bytes, old_run = zero_call_state
    live._verify_zero_call_continuation(args, approved, activation_bytes, old_run)
    receipt = live._claim_zero_call_continuation(
        args.aggregate_ledger, approved, old_run, "b" * 32, "c" * 40, activation_bytes
    )
    assert receipt["original_run_id"] == old_run
    assert receipt["continuation_run_id"] == "b" * 32
    with sqlite3.connect(args.aggregate_ledger) as db:
        assert db.execute("SELECT run_id, slots FROM runs").fetchall() == [(old_run, 3)]
        assert db.execute(
            "SELECT original_run_id, continuation_run_id FROM zero_call_continuations"
        ).fetchall() == [(old_run, "b" * 32)]
    with pytest.raises(ValueError, match="already continued"):
        live._claim_zero_call_continuation(
            args.aggregate_ledger,
            approved,
            old_run,
            "d" * 32,
            "c" * 40,
            activation_bytes,
        )
    with pytest.raises(ValueError, match="zero-call continuation"):
        live._verify_zero_call_continuation(args, approved, activation_bytes, old_run)


def test_continuation_directory_check_rejects_symlink_and_open_mode(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(live, "_protected_parents", lambda *_: None)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    live._checked_directory(state, os.geteuid(), 0o700)
    state.chmod(0o755)
    with pytest.raises(ValueError, match="unsafe state directory"):
        live._checked_directory(state, os.geteuid(), 0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(state, target_is_directory=True)
    with pytest.raises(OSError):
        live._checked_directory(alias, os.geteuid(), 0o700)


def test_zero_call_continuation_accepts_consistent_interrupted_report(zero_call_state):
    args, approved, activation_bytes, old_run = zero_call_state
    args.previous_report.write_text(
        json.dumps(
            {
                "run_id": old_run,
                "status": "interrupted",
                "error": "KeyboardInterrupt",
                "provider_attempts": 0,
                "worker_stop": True,
                "owner_stop": True,
                "running_services": [],
            }
        )
    )
    live._verify_zero_call_continuation(args, approved, activation_bytes, old_run)


def test_continuation_rechecks_original_evidence_after_new_state_is_prepared(
    zero_call_state,
):
    args, approved, activation_bytes, old_run = zero_call_state
    new_run = "b" * 32
    (args.state_root / "runs" / new_run).mkdir()
    with pytest.raises(ValueError, match="zero-call continuation"):
        live._verify_zero_call_continuation(args, approved, activation_bytes, old_run)
    live._verify_zero_call_continuation(
        args, approved, activation_bytes, old_run, new_run
    )


@pytest.mark.parametrize(
    "change",
    [
        "attempt",
        "provider_reservation",
        "consumed",
        "owner_open",
        "worker_open",
        "changed_activation",
        "changed_allowance",
        "other_run",
        "contradictory_report",
        "changed_approval",
        "missing_provider_database",
        "uncertain_sidecar",
        "active_container",
        "missing_container",
    ],
)
def test_zero_call_continuation_refuses_uncertain_state(
    zero_call_state, monkeypatch, change
):
    args, approved, activation_bytes, old_run = zero_call_state
    if change == "attempt":
        database, command = (
            args.state_root / "worker" / "provider.sqlite3",
            "INSERT INTO provider_usage_attempts VALUES ('attempt')",
        )
    elif change == "provider_reservation":
        database, command = (
            args.state_root / "worker" / "provider.sqlite3",
            "INSERT INTO provider_attempt_reservations VALUES ('reservation')",
        )
    elif change == "consumed":
        database, command = (
            args.state_root / "worker" / "allowance.sqlite3",
            "INSERT INTO consumed VALUES ('operation')",
        )
    elif change == "owner_open":
        database, command = (
            args.state_root / "runs" / old_run / "app" / "owner" / "owner.sqlite",
            "UPDATE owner_stop SET active=0",
        )
    elif change == "other_run":
        database, command = (
            args.aggregate_ledger,
            "INSERT INTO runs VALUES ('other', 3)",
        )
    else:
        database = command = None
    if database is not None:
        with sqlite3.connect(database) as db:
            db.execute(command)
    if change == "worker_open":
        (args.state_root / "control" / "emergency-stop.json").write_text(
            json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": False})
        )
    if change == "changed_activation":
        (args.state_root / "control" / "activation.json").write_text("changed")
    if change == "changed_allowance":
        with sqlite3.connect(args.state_root / "worker" / "allowance.sqlite3") as db:
            db.execute("UPDATE authority SET digest='changed'")
    if change == "contradictory_report":
        args.previous_report.write_text(
            json.dumps({"run_id": old_run, "status": "passed", "error": None})
        )
    if change == "changed_approval":
        approved["billing_budget_usd"] = "0.09"
    if change == "missing_provider_database":
        (args.state_root / "worker" / "provider.sqlite3").unlink()
    if change == "uncertain_sidecar":
        (args.state_root / "worker" / "provider.sqlite3-wal").write_bytes(b"")
    if change in {"active_container", "missing_container"}:
        monkeypatch.setattr(
            live,
            "_compose",
            (
                lambda _env, *parts: "private-app"
                if change == "active_container"
                or parts == ("ps", "--all", "--services")
                else ""
            ),
        )
    with pytest.raises(ValueError, match="zero-call continuation"):
        live._verify_zero_call_continuation(args, approved, activation_bytes, old_run)
    with sqlite3.connect(args.aggregate_ledger) as db:
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='zero_call_continuations'"
            ).fetchall()
            == []
        )


@pytest.mark.parametrize("protected", [False, True])
def test_second_real_sigint_during_blocking_cleanup_keeps_report(tmp_path, protected):
    report = tmp_path / "interrupted.json"
    script = """
import signal
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from acceptance.private_pilot_live import _ignore_sigint
report = Path(sys.argv[1])
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
print('child-started', flush=True)
try:
    child.wait()
except KeyboardInterrupt:
    with (_ignore_sigint() if sys.argv[2] == 'protected' else nullcontext()):
        print('cleanup-started', flush=True)
        try:
            subprocess.run([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=1)
        except subprocess.TimeoutExpired:
            pass
        report.write_text('{"status":"interrupted"}')
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(report),
            "protected" if protected else "old",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    try:
        assert process.stdout.readline().strip() == "child-started"
        os.killpg(process.pid, signal.SIGINT)
        assert process.stdout.readline().strip() == "cleanup-started"
        os.killpg(process.pid, signal.SIGINT)
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    if protected:
        assert report.read_text() == '{"status":"interrupted"}'
    else:
        assert not report.exists()


def test_first_sigint_during_cleanup_is_recorded_without_skipping_report(tmp_path):
    report = tmp_path / "cleanup.json"
    script = """
import json
import signal
import subprocess
import sys
from pathlib import Path
from acceptance.private_pilot_live import _ignore_sigint
report = {'status': 'in_progress'}
interrupted = [False]
with _ignore_sigint(lambda: interrupted.__setitem__(0, True)):
    print('cleanup-started', flush=True)
    try:
        subprocess.run([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=2)
    except subprocess.TimeoutExpired:
        pass
    if interrupted[0]:
        report.update(status='interrupted', error='KeyboardInterrupt')
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    Path(sys.argv[1]).write_text(json.dumps(report))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(report)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    try:
        assert process.stdout.readline().strip() == "cleanup-started"
        os.killpg(process.pid, signal.SIGINT)
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    assert json.loads(report.read_text()) == {
        "status": "interrupted",
        "error": "KeyboardInterrupt",
    }


@pytest.mark.parametrize("case", ["commit_changed", "window_expired"])
def test_continuation_rejects_unreviewed_commit_or_expired_window(
    zero_call_state, monkeypatch, case
):
    args, approved, activation_bytes, old_run = zero_call_state
    args.authorization = args.report.parent / "authorization.json"
    args.activation = args.report.parent / "activation.json"
    args.continue_zero_call = old_run
    args.reviewed_commit = "d" * 40 if case == "commit_changed" else "c" * 40
    if case == "window_expired":
        now = datetime.now(UTC)
        approved["valid_from"] = (now - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        approved["valid_until"] = (now + timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        active = json.loads(activation_bytes)
        active["not_before"] = (now - timedelta(minutes=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        active["expires_at"] = (now - timedelta(minutes=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        activation_bytes = json.dumps(active).encode()
    args.authorization.write_text(json.dumps(approved))
    args.activation.write_bytes(activation_bytes)
    monkeypatch.setattr(live.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        live,
        "_command",
        lambda parts, _env: "c" * 40 if parts[:2] == ("git", "rev-parse") else "",
    )
    monkeypatch.setattr(
        live,
        "_compose",
        lambda *_args, **_kwargs: pytest.fail(
            "preparation began before continuation approval"
        ),
    )
    with pytest.raises(ValueError, match="reviewed commit|window expired"):
        live.run(args)
    assert not args.report.exists()


def test_contradictory_mars_text_does_not_pass_destination_oracle():
    replies = iter(
        (
            "Искра",
            "Корабль: Искра; Назначение: Юпитер",
            "Корабль Искра всё ещё летит к Юпитеру, а не к Марсу. Марс не выбран.",
        )
    )

    def send(_message_id, _content):
        return {"model_state": "completed", "response": next(replies)}

    with pytest.raises(AssertionError, match="Mars"):
        run_ship_journey(send, lambda: [], lambda: None)


def test_protected_file_rejects_writable_parent_and_open_replacement(
    tmp_path, monkeypatch
):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o770)
    unsafe.chmod(0o770)
    protected = unsafe / "approval.json"
    protected.write_bytes(b"approved")
    protected.chmod(0o400)
    with pytest.raises(ValueError, match="unsafe protected directory"):
        live._protected(protected, 0o400, os.geteuid())

    monkeypatch.setattr(live, "_protected", lambda path, _mode, _uid: path.lstat())
    assert live._read_protected(protected, 0o400, os.geteuid()) == b"approved"
    original_open = live.os.open

    def replace_before_open(path, flags):
        replacement = unsafe / "replacement.json"
        replacement.write_bytes(b"substituted")
        replacement.chmod(0o400)
        replacement.replace(path)
        return original_open(path, flags)

    monkeypatch.setattr(live.os, "open", replace_before_open)
    with pytest.raises(ValueError, match="changed while opening"):
        live._read_protected(protected, 0o400, os.geteuid())


def test_compose_build_uses_separate_preparation_timeout(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs["timeout"]))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(live.subprocess, "run", run)
    live._compose({}, "build", timeout=600)
    live._compose({}, "ps")
    assert calls == [((*live.COMPOSE, "build"), 600), ((*live.COMPOSE, "ps"), 120)]


def test_report_symlink_is_rejected_before_preparation(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_text("keep")
    report_path = tmp_path / "report.json"
    report_path.symlink_to(target)
    monkeypatch.setattr(live, "_protected_parents", lambda *_: None)
    safe = tmp_path / "safe.json"
    live._write_report(safe, {"status": "passed"})
    assert json.loads(safe.read_text()) == {"status": "passed"}
    assert safe.stat().st_mode & 0o777 == 0o600
    with pytest.raises(OSError):
        live._write_report(report_path, {"status": "failed"})
    assert target.read_text() == "keep"

    current = datetime.now(UTC)
    approved = approval()
    approved["valid_from"] = (current - timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    approved["valid_until"] = (current + timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    active = activation()
    active["not_before"] = (current - timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    active["expires_at"] = (current + timedelta(minutes=4)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    authorization = tmp_path / "authorization.json"
    authorization.write_text(json.dumps(approved))
    activation_path = tmp_path / "activation.json"
    activation_path.write_text(json.dumps(active))
    args = SimpleNamespace(
        authorization=authorization,
        activation=activation_path,
        state_root=tmp_path / "state",
        aggregate_ledger=tmp_path / "aggregate.sqlite3",
        origin="https://chat.test",
        backend_subnet="172.30.252.0/29",
        backend_ip="172.30.252.3",
        report=report_path,
    )
    monkeypatch.setattr(live.os, "geteuid", lambda: 0)
    monkeypatch.setattr(live, "_read_protected", lambda path, *_: path.read_bytes())
    monkeypatch.setattr(live, "_check_backend_conflicts", lambda *_: None)

    def command(parts, _env):
        return "synthetic-commit" if parts[:2] == ("git", "rev-parse") else ""

    monkeypatch.setattr(live, "_command", command)
    monkeypatch.setattr(
        live,
        "_compose",
        lambda *_args, **_kwargs: pytest.fail("paid preparation started"),
    )
    with pytest.raises(ValueError, match="unsafe protected file"):
        live.run(args)
    assert target.read_text() == "keep"


def test_duplicate_authorization_is_rejected_before_preparation(tmp_path, monkeypatch):
    raw = json.dumps(approval())[:-1] + ',"commit":"synthetic-commit"}'
    args = SimpleNamespace(
        authorization=tmp_path / "authorization.json",
        activation=tmp_path / "activation.json",
        state_root=tmp_path / "state",
        aggregate_ledger=tmp_path / "aggregate.sqlite3",
        origin="https://chat.test",
        backend_subnet="172.30.252.0/29",
        backend_ip="172.30.252.3",
        report=tmp_path / "report.json",
    )
    monkeypatch.setattr(live.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        live,
        "_command",
        lambda parts, _env: "synthetic-commit" if parts[1] == "rev-parse" else "",
    )
    monkeypatch.setattr(
        live,
        "_read_protected",
        lambda path, *_: raw.encode()
        if path == args.authorization
        else json.dumps(activation()).encode(),
    )
    monkeypatch.setattr(
        live,
        "_compose",
        lambda *_args, **_kwargs: pytest.fail("paid preparation started"),
    )
    with pytest.raises(ValueError, match="duplicate JSON key: commit"):
        live.run(args)


def test_control_files_reject_symlinks_and_creation_race(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "_protected_parents", lambda *_: None)
    target = tmp_path / "target.txt"
    target.write_text("keep")
    activation_path = tmp_path / "activation.json"
    activation_path.symlink_to(target)
    with pytest.raises(OSError):
        live._write_control(
            activation_path, b"activation", os.geteuid(), create_only=True
        )
    stop_path = tmp_path / "emergency-stop.json"
    stop_path.symlink_to(target)
    with pytest.raises(OSError):
        live._write_control(stop_path, b"stop", os.geteuid())
    assert target.read_text() == "keep"

    stop_path.unlink()
    os.mkfifo(stop_path)
    with pytest.raises(OSError):
        live._write_control(stop_path, b"stop", os.geteuid())
    stop_path.unlink()
    live._write_control(stop_path, b"stop", os.geteuid())
    assert stop_path.read_bytes() == b"stop"
    assert stop_path.stat().st_mode & 0o777 == 0o400

    raced = tmp_path / "raced.json"
    original_open = live.os.open

    def install_symlink(path, flags, *args):
        if path == raced:
            raced.symlink_to(target)
        return original_open(path, flags, *args)

    monkeypatch.setattr(live.os, "open", install_symlink)
    with pytest.raises(OSError):
        live._write_control(raced, b"activation", os.geteuid(), create_only=True)
    assert target.read_text() == "keep"


def test_state_directory_preparation_rejects_symlinks_and_worker_owner(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(live, "_protected_parents", lambda *_: None)
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    linked = tmp_path / "state"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        live._prepare_state_directory(linked)
    with pytest.raises(OSError):
        live._prepare_service_directory(linked, live.WORKER_UID, live.WORKER_UID, 0o700)
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert target.stat().st_uid == os.geteuid()

    linked.unlink()
    linked.mkdir(mode=0o700)
    monkeypatch.setattr(
        live.os,
        "fstat",
        lambda _fd: SimpleNamespace(
            st_uid=live.WORKER_UID, st_mode=stat.S_IFDIR | 0o700
        ),
    )
    with pytest.raises(ValueError, match="unsafe state directory"):
        live._prepare_state_directory(linked)


@pytest.mark.parametrize("invalid", ["history", "transcript"])
def test_journey_validation_survives_optimized_python(invalid):
    script = f"""
from acceptance.private_pilot_conversation import FIRST, ASK, MARS, run_ship_journey
replies = iter(('Искра', 'Корабль: Искра; Назначение: Юпитер', 'Корабль: Искра; Назначение: Марс'))
def send(_id, _content):
    return {{'model_state': 'completed', 'response': next(replies)}}
def history():
    return []
def inspect():
    return {{'conversation': []}}
try:
    run_ship_journey(send, history, lambda: None, {"inspect" if invalid == "transcript" else "None"})
except AssertionError:
    raise SystemExit(0)
raise SystemExit(1)
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def attempt(
    operation,
    correlation,
    *,
    input_tokens=3,
    output_tokens=2,
    outcome="completed",
    rejection=None,
    retry=0,
    suffix=1,
):
    return [
        f"{operation}:{suffix}",
        correlation,
        outcome,
        rejection,
        input_tokens,
        output_tokens,
        None
        if input_tokens is None or output_tokens is None
        else input_tokens + output_tokens,
        retry,
        "0.01",
        "estimated",
    ]


@pytest.mark.parametrize(
    "case",
    [
        "passed",
        "pricing_unavailable",
        "exact_limit_final",
        "limit_after_first",
        "unknown_after_first",
        "empty_ledger",
        "empty_after_journey",
        "historical_only",
        "historical_mutation",
        "duplicate",
        "extra",
        "incomplete",
        "rejected",
        "retries",
        "wrong_allowance_delta",
        "wrong_correlation",
        "unresolved",
        "expired_mid_journey",
        "route_failure",
        "wrong_username_route",
        "malformed_route",
        "restart_route_failure",
        "restart_malformed_route",
        "worker_not_ready",
        "worker_stop_failure",
        "owner_stop_failure",
        "interrupted",
        "cleanup_sigint",
        "cleanup_progress_failure",
        "journey_outcome_failure",
        "continued",
    ],
)
def test_operator_reconciles_real_journey_and_cleans_up(
    tmp_path, monkeypatch, capsys, case
):
    current = datetime.now(UTC)
    approved = approval()
    approved["valid_from"] = (current - timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    approved["valid_until"] = (current + timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    if case == "limit_after_first":
        approved["max_aggregate_input_tokens"] = 4
    if case == "exact_limit_final":
        approved["max_aggregate_input_tokens"] = 10
    if case == "continued":
        approved.update(
            commit="b" * 40,
            max_aggregate_operations=3,
            max_aggregate_input_tokens=6000,
            max_aggregate_output_tokens=1536,
            max_requests_per_run=3,
            billing_budget_usd="0.10",
        )
    state_dir = tmp_path / "state"
    (state_dir / "secrets").mkdir(parents=True)
    (state_dir / "secrets" / "openai-api-key").write_text("offline-placeholder")
    authorization = tmp_path / "authorization.json"
    authorization.write_text(json.dumps(approved))
    activation_path = tmp_path / "activation.json"
    active = activation()
    active["not_before"] = (current - timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    active["expires_at"] = (current + timedelta(minutes=4)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    if case == "continued":
        active["max_operations"] = 3
    activation_path.write_text(json.dumps(active))
    args = SimpleNamespace(
        authorization=authorization,
        activation=activation_path,
        state_root=state_dir,
        aggregate_ledger=tmp_path / "aggregate.sqlite3",
        origin="https://chat.test",
        backend_subnet="172.30.252.0/29",
        backend_ip="172.30.252.3",
        report=tmp_path / "report.json",
    )
    if case == "continued":
        args.continue_zero_call = "a" * 32
        args.reviewed_commit = "c" * 40
        args.previous_report = tmp_path / "old-report.json"
    commands = []
    owner_changes = []
    events = []
    monkeypatch.setattr(live.os, "geteuid", lambda: 0)
    monkeypatch.setattr(live.os, "chown", lambda *_: None)
    monkeypatch.setattr(live, "_protected", lambda *_: None)
    monkeypatch.setattr(live, "_protected_parents", lambda *_: None)
    monkeypatch.setattr(live, "_read_protected", lambda path, *_: path.read_bytes())
    monkeypatch.setattr(live, "_check_backend_conflicts", lambda *_: None)
    if case == "continued":
        reserve_aggregate(args.aggregate_ledger, approved, args.continue_zero_call)
        monkeypatch.setattr(
            live,
            "_verify_zero_call_continuation",
            lambda *_: events.append(("verified_zero_call", None)),
        )
    monkeypatch.setattr(
        live,
        "_prepare_state_directory",
        lambda path: path.mkdir(mode=0o700, exist_ok=True),
    )
    monkeypatch.setattr(
        live,
        "_prepare_service_directory",
        lambda path, _uid, _gid, mode: path.mkdir(mode=mode, exist_ok=True),
    )

    def write_control(path, payload, _uid, *, create_only=False):
        if create_only and path.exists():
            raise FileExistsError(path)
        path.write_bytes(payload)
        path.chmod(0o400)

    monkeypatch.setattr(live, "_write_control", write_control)

    def write_report(path, report):
        path.write_text(json.dumps(report))
        path.chmod(0o600)

    monkeypatch.setattr(live, "_write_report", write_report)
    if case == "cleanup_progress_failure":
        original_progress = live._progress

        def progress(path, report, phase):
            if phase == "cleanup":
                raise OSError("synthetic report filesystem failure")
            original_progress(path, report, phase)

        monkeypatch.setattr(live, "_progress", progress)
    monkeypatch.setattr(live, "getpass", lambda _: "synthetic-password")
    real_stop = live._stop_file

    def stop(path, active):
        events.append(("worker_stop", active))
        if (
            case == "worker_stop_failure"
            and active
            and ("worker_stop", False) in events
        ):
            raise OSError("synthetic worker Stop write failed")
        if path.exists():
            path.chmod(0o600)
        real_stop(path, active)

    monkeypatch.setattr(live, "_stop_file", stop)

    def command(parts, _env):
        if parts[:2] == ("git", "rev-parse"):
            return "c" * 40 if case == "continued" else "synthetic-commit"
        if parts[:2] == ("git", "status"):
            return ""
        return "sha256:synthetic-image"

    monkeypatch.setattr(live, "_command", command)

    def compose(_env, *parts, **_kwargs):
        if parts == ("build",):
            assert _kwargs["timeout"] == 600
        commands.append(parts)
        if parts[:2] == ("up", "-d"):
            events.append(("up", parts[2:]))
        if case == "cleanup_sigint" and parts[:2] == ("stop", "private-app"):
            signal.raise_signal(signal.SIGINT)
        return ""

    monkeypatch.setattr(live, "_compose", compose)
    source = {}

    def app_python(_env, code, input_text=None, *, running=False, timeout=120):
        assert timeout in {15, 120}
        if "FROM event_envelopes" in code:
            assert running is True
            return json.dumps([source[input_text.rstrip("\n")]])
        assert running is False
        return "True" if "print(OwnerStore" in code else ""

    monkeypatch.setattr(live, "_app_python", app_python)
    old = attempt("reply:historical", "historic", input_tokens=1, output_tokens=1)
    rows = [] if case == "continued" else [old]
    consumed = [] if case == "continued" else ["reply:historical"]
    reservations = []
    history_seen = False

    def worker_python(_env, code, *, timeout=120):
        assert timeout in {15, 120}
        if "provider_usage_attempts" in code:
            observed = [old] if case == "empty_after_journey" and history_seen else rows
            return json.dumps({"attempts": observed, "reservations": reservations})
        if "SELECT operation_id FROM consumed" in code:
            observed = (
                consumed[:1]
                if case == "empty_after_journey" and history_seen
                else consumed
            )
            return json.dumps(
                {"maximum": 3 if case == "continued" else 5, "consumed": observed}
            )
        return ""

    monkeypatch.setattr(live, "_worker_python", worker_python)

    def owner_change(_env, _password, active, *, running=False, timeout=120):
        assert timeout in {15, 120}
        owner_changes.append(active)
        events.append(("owner_mode", active, running))
        events.append(("owner_stop", active))
        if case == "owner_stop_failure" and active and len(owner_changes) > 1:
            raise RuntimeError("synthetic owner Stop command failed")

    monkeypatch.setattr(live, "_owner_change", owner_change)
    original_reserve = live.reserve_aggregate

    def reserve(*values):
        events.append(("reserve", None))
        return original_reserve(*values)

    monkeypatch.setattr(live, "reserve_aggregate", reserve)
    replies = (
        "Искра",
        "Корабль: Искра; Назначение: Юпитер",
        "Корабль: Искра; Назначение: Марс",
    )
    transcript = []
    sends = []

    class FakeOpener:
        def __init__(self, processor):
            self.jar = processor.cookiejar

        def open(self, request, *, timeout):
            nonlocal history_seen
            assert timeout in {20, 55}
            assert request.get_header("Origin") == args.origin
            path = urlsplit(request.full_url).path
            method = request.get_method()
            if path == "/login":
                if method == "GET":
                    _cookie(self.jar, "ar_login_csrf", "synthetic-login-csrf")
                    return _response(b"<html>login</html>")
                form = parse_qs(request.data.decode())
                assert form["csrf"] == ["synthetic-login-csrf"]
                assert form["password"] == ["synthetic-password"]
                self.username = form["username"][0]
                _cookie(self.jar, "ar_csrf", "synthetic-csrf")
                return _response(b"<html>signed in</html>")
            if path == "/session":
                events.append(("session", None))
                if case == "malformed_route" or (
                    case == "restart_malformed_route"
                    and ("restart", "private-app") in commands
                ):
                    return _response(b"<html>wrong route</html>")
                if case == "route_failure" or (
                    case == "restart_route_failure"
                    and ("restart", "private-app") in commands
                ):
                    return _response({"authenticated": False})
                if case == "wrong_username_route":
                    return _response({"authenticated": True, "username": "other-user"})
                assert request.data is None
                return _response({"authenticated": True, "username": self.username})
            if path == "/api/history":
                history_seen = True
                return _response({"messages": transcript})
            if path != "/api/messages":
                return _response({})
            if case == "interrupted":
                raise KeyboardInterrupt
            index = len(sends)
            assert request.get_header("X-csrf-token") == "synthetic-csrf"
            payload = json.loads(request.data)
            sends.append(payload)
            message_id = payload["message_id"]
            event_id = f"event-{index}"
            correlation = f"correlation-{index}"
            source[message_id] = [event_id, correlation]
            operation = "reply:" + hashlib.sha256(event_id.encode()).hexdigest()
            transcript.extend(
                (
                    {"role": "participant", "content": payload["content"]},
                    {"role": "agent", "content": replies[index]},
                )
            )
            if case not in {"empty_ledger", "historical_only"}:
                row = attempt(operation, correlation)
                if case == "limit_after_first" and index == 0:
                    row[4] = 4
                if case == "unknown_after_first" and index == 0:
                    row[4] = None
                if case == "incomplete" and index == 2:
                    row[2] = "timeout"
                if case == "rejected" and index == 2:
                    row[3] = "project_spend_limit_exceeded"
                if case == "retries" and index == 2:
                    row[7] = 1
                if case == "wrong_correlation" and index == 2:
                    row[1] = "other-correlation"
                if case == "pricing_unavailable":
                    row[8] = None
                    row[9] = "pricing_unavailable"
                rows.append(row)
                if case == "duplicate" and index == 2:
                    rows.append(row.copy())
                if case == "extra" and index == 2:
                    rows.append(attempt("reply:extra", "other"))
                if case == "unresolved" and index == 2:
                    reservations.append("reply:unresolved:1")
                if case == "historical_mutation" and index == 2:
                    old[4] = 2
                if case != "wrong_allowance_delta" or index != 2:
                    consumed.append(operation)
            return _response({"model_state": "completed", "response": replies[index]})

    monkeypatch.setattr(live, "build_opener", FakeOpener)
    if case == "journey_outcome_failure":
        from acceptance.private_memory_journey import JourneyOutcomeError

        def fail_after_first(send, *_args, **_kwargs):
            send("ship-name", FIRST)
            raise JourneyOutcomeError("robot-filler-0", "no_action")

        monkeypatch.setattr(live, "run_ship_journey", fail_after_first)

    def worker_ready(env):
        assert env["PILOT_SOCKET_DIR"].endswith("ipc")
        events.append(("worker_ready", case != "worker_not_ready"))
        if case == "worker_not_ready":
            raise live.BackendReadinessError("provider worker socket unavailable")

    monkeypatch.setattr(live, "_wait_worker_ready", worker_ready)
    if case == "expired_mid_journey":
        original = live.validate_authorization
        count = 0

        def validate(*values):
            nonlocal count
            count += 1
            if count == 4:
                raise ValueError("authorization is outside its validity period")
            return original(*values)

        monkeypatch.setattr(live, "validate_authorization", validate)
    if case in {"passed", "exact_limit_final", "pricing_unavailable", "continued"}:
        live.run(args)
    elif case == "interrupted":
        with pytest.raises(KeyboardInterrupt):
            live.run(args)
    else:
        with pytest.raises(
            (ValueError, RuntimeError, AssertionError),
            match="allowance|usage|accounting|reservation|attempt|identity|period|exhausted|cleanup|match|HTTPS route|Expecting value|worker socket|robot-filler",
        ):
            live.run(args)
    report = json.loads(args.report.read_text())
    assert report["status"] == (
        "passed"
        if case in {"passed", "exact_limit_final", "pricing_unavailable", "continued"}
        else "interrupted"
        if case in {"interrupted", "cleanup_sigint"}
        else "failed"
    )
    assert report["worker_stop"] is (case != "worker_stop_failure")
    assert report["owner_stop"] is True
    if case == "journey_outcome_failure":
        assert report["failure_step"] == "robot-filler-0"
        assert report["failure_model_state"] == "no_action"
        assert report["error"] == "JourneyOutcomeError"
        assert len(sends) == 1
    if case != "owner_stop_failure":
        assert report.get("owner_stop_error") is None
    assert report["running_services"] == []
    if case in {"route_failure", "wrong_username_route", "malformed_route"}:
        assert owner_changes == [True, True]
        assert [entry[2] for entry in events if entry[0] == "owner_mode"] == [
            False,
            True,
        ]
        assert ("reserve", None) not in events
        assert ("worker_stop", False) not in events
        assert report.get("backend_ready") is None
        if case != "malformed_route":
            assert (
                report["backend_error"]
                == "HTTPS route does not reach this acceptance app"
            )
        assert sends == []
    elif case == "worker_not_ready":
        assert owner_changes == [True, True]
        assert report.get("owner_stop_error") is None
        assert report.get("worker_ready") is None
        assert report["backend_error"] == ("provider worker socket unavailable")
        assert ("reserve", None) not in events
        assert ("worker_stop", False) not in events
        assert events.index(("up", ("provider-worker", "private-app"))) < events.index(
            ("worker_ready", False)
        )
        assert sends == []
    else:
        assert owner_changes == [True, False, True]
        assert report["worker_ready"] is True
        assert [entry[2] for entry in events if entry[0] == "owner_mode"] == [
            False,
            True,
            True,
        ]
        assert events.index(("up", ("private-app",))) < events.index(("session", None))
        if case == "continued":
            assert ("reserve", None) not in events
            assert events.index(("verified_zero_call", None)) < events.index(
                ("up", ("private-app",))
            )
            assert report["continuation_receipt"]["original_run_id"] == "a" * 32
            with sqlite3.connect(args.aggregate_ledger) as db:
                assert db.execute("SELECT run_id, slots FROM runs").fetchall() == [
                    ("a" * 32, 3)
                ]
        else:
            assert events.index(("session", None)) < events.index(("reserve", None))
            assert events.index(("worker_ready", True)) < events.index(
                ("reserve", None)
            )
            assert events.index(("reserve", None)) < events.index(
                ("worker_stop", False)
            )
        assert events.index(("up", ("provider-worker", "private-app"))) < events.index(
            ("worker_stop", False)
        )
        assert report["backend_ready"] is True
    assert any(parts[:2] == ("stop", "private-app") for parts in commands)
    assert Path(args.report).stat().st_mode & 0o777 == 0o600
    if case in {"limit_after_first", "unknown_after_first"}:
        assert len(sends) == 1
    if case in {"interrupted", "cleanup_sigint"}:
        assert report["error"] == "KeyboardInterrupt"
    if case == "cleanup_progress_failure":
        assert report["cleanup_progress_error"] is True
        assert ("worker_stop", True) in events
        assert ("owner_stop", True) in events
        assert len(sends) == 3
    if case == "worker_stop_failure":
        assert report["owner_stop"] is True
        assert report.get("owner_stop_error") is None
    if case == "owner_stop_failure":
        assert report["owner_stop_error"] is True
        assert report["worker_stop"] is True
    if case == "interrupted":
        assert sends == []
        assert report["provider_attempts"] == 0
        assert report.get("accounting_error") is not True
    if case in {"restart_route_failure", "restart_malformed_route"}:
        assert len(sends) == 2
        if case == "restart_route_failure":
            assert report["backend_error"] == "HTTPS route changed after app restart"
    if case in {"passed", "exact_limit_final", "pricing_unavailable", "continued"}:
        assert [item["content"] for item in sends] == [FIRST, ASK, MARS]
        assert report["provider_attempts"] == 3
        assert report["remaining_allowance"] == (0 if case == "continued" else 1)
        assert report["aggregate_input_tokens"] == (9 if case == "continued" else 10)
        assert any(parts[:2] == ("restart", "private-app") for parts in commands)
    if case == "pricing_unavailable":
        assert report["estimated_cost_total"] is None
        assert report["cost_estimate_statuses"] == ["pricing_unavailable"]
    output = capsys.readouterr().err
    if case != "cleanup_progress_failure":
        assert "Acceptance phase: cleanup" in output
    assert "synthetic-password" not in output
    assert FIRST not in output
    assert replies[0] not in output
