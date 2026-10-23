"""Explicitly authorized, synthetic private-pilot acceptance on an approved host."""

import argparse
import hashlib
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from getpass import getpass
from http.cookiejar import CookieJar
from ipaddress import ip_address, ip_network
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

from acceptance.private_pilot_conversation import run_ship_journey


COMPOSE = (
    "docker",
    "compose",
    "-f",
    "ops/compose.private-pilot.yaml",
    "-f",
    "ops/compose.private-pilot-acceptance.yaml",
)
TASK = "synthetic-private-pilot-ship-restart-mars"
MODEL = "gpt-6-luna"
APP_UID = 11022
WORKER_UID = 11021


class BackendConfigurationError(ValueError):
    """Safe operator-facing network configuration failure."""


class BackendReadinessError(ValueError):
    """Safe operator-facing HTTPS route failure."""


def _backend_network(subnet_text, address_text):
    try:
        subnet = ip_network(subnet_text, strict=True)
        address = ip_address(address_text)
    except ValueError:
        raise BackendConfigurationError(
            "invalid acceptance backend address or subnet"
        ) from None
    if (
        subnet.version != 4
        or address.version != 4
        or not subnet.is_private
        or address not in subnet
        or address in {subnet.network_address, subnet.broadcast_address}
        or address == subnet.network_address + 1
    ):
        raise BackendConfigurationError("invalid acceptance backend address or subnet")
    return subnet, address


def _check_backend_conflicts(subnet, env):
    try:
        network_ids = _command(("docker", "network", "ls", "-q"), env).splitlines()
        if network_ids:
            networks = json.loads(
                _command(("docker", "network", "inspect", *network_ids), env)
            )
            for network in networks:
                for config in network.get("IPAM", {}).get("Config") or []:
                    existing = config.get("Subnet")
                    if existing and subnet.overlaps(ip_network(existing, strict=False)):
                        raise BackendConfigurationError(
                            "acceptance backend subnet conflicts with Docker network"
                        )
        routes = json.loads(_command(("ip", "-j", "-4", "route", "show"), env))
        for route in routes:
            destination = route.get("dst")
            if (
                destination
                and destination != "default"
                and subnet.overlaps(ip_network(destination, strict=False))
            ):
                raise BackendConfigurationError(
                    "acceptance backend subnet conflicts with host route"
                )
    except (RuntimeError, ValueError) as error:
        if isinstance(error, BackendConfigurationError):
            raise
        raise BackendConfigurationError(
            "cannot inspect Docker networks or host routes"
        ) from error


def _utc(value):
    result = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return result


def _protected_parents(path, uid):
    for directory in (path.parent, *path.parent.parents):
        parent = directory.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid not in {0, uid}
            or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            raise ValueError(f"unsafe protected directory: {directory}")


def _protected(path, mode, uid):
    _protected_parents(path, uid)
    entry = path.lstat()
    if (
        not stat.S_ISREG(entry.st_mode)
        or stat.S_IMODE(entry.st_mode) != mode
        or entry.st_uid != uid
        or entry.st_nlink != 1
    ):
        raise ValueError(f"unsafe protected file: {path}")
    return entry


def _read_protected(path, mode, uid):
    entry = _protected(path, mode, uid)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        if (
            (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != mode
            or opened.st_uid != uid
            or opened.st_nlink != 1
        ):
            raise ValueError(f"protected file changed while opening: {path}")
        with os.fdopen(fd, "rb", closefd=False) as source:
            return source.read()
    finally:
        os.close(fd)


def _unique_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=unique)


def _write_control(path, payload, uid, *, create_only=False):
    _protected_parents(path, uid)
    flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        if create_only:
            raise
        fd = os.open(path, flags)
        created = False
    try:
        entry = os.fstat(fd)
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_nlink != 1
            or (
                created
                and (
                    entry.st_uid != os.geteuid() or stat.S_IMODE(entry.st_mode) != 0o600
                )
            )
            or (
                not created
                and (entry.st_uid != uid or stat.S_IMODE(entry.st_mode) != 0o400)
            )
        ):
            raise ValueError(f"unsafe control file: {path}")
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
        if entry.st_uid != uid:
            os.fchown(fd, uid, uid)
        os.fchmod(fd, 0o400)
        os.fsync(fd)
    finally:
        os.close(fd)


def _prepare_state_directory(path):
    _protected_parents(path, 0)
    path.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        entry = os.fstat(fd)
        if entry.st_uid != 0 or stat.S_IMODE(entry.st_mode) != 0o700:
            raise ValueError(f"unsafe state directory: {path}")
    finally:
        os.close(fd)


def _prepare_service_directory(path, uid, gid, mode):
    path.mkdir(mode=mode, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _write_report(path, report):
    _protected_parents(path, os.geteuid())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        entry = os.fstat(fd)
        if (
            not stat.S_ISREG(entry.st_mode)
            or stat.S_IMODE(entry.st_mode) != 0o600
            or entry.st_uid != os.geteuid()
            or entry.st_nlink != 1
        ):
            raise ValueError(f"unsafe report file: {path}")
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", closefd=False) as output:
            output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            output.flush()
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_authorization(approval, activation, commit, now):
    """Refuse missing or ambiguous approval before preparing paid controls."""

    required = {
        "task",
        "commit",
        "model",
        "project_id",
        "organization_id",
        "valid_from",
        "valid_until",
        "billing_budget_usd",
        "max_aggregate_operations",
        "max_aggregate_input_tokens",
        "max_aggregate_output_tokens",
        "max_requests_per_run",
    }
    if set(approval) != required or approval["task"] != TASK:
        raise ValueError("explicit task authorization is missing")
    if approval["commit"] != commit or approval["model"] != MODEL:
        raise ValueError("commit or model is not authorized")
    if not _utc(approval["valid_from"]) <= now < _utc(approval["valid_until"]):
        raise ValueError("authorization is outside its validity period")
    if not (
        _utc(approval["valid_from"])
        <= _utc(activation["not_before"])
        < _utc(activation["expires_at"])
        <= _utc(approval["valid_until"])
    ):
        raise ValueError("activation extends beyond the authorized period")
    if any(
        approval[key] != activation[key]
        for key in ("model", "project_id", "organization_id")
    ):
        raise ValueError("activation billing scope differs from authorization")
    if activation.get("max_attempts_per_operation") != 1:
        raise ValueError("activation must permit one attempt per operation")
    for key in (
        "max_aggregate_operations",
        "max_aggregate_input_tokens",
        "max_aggregate_output_tokens",
        "max_requests_per_run",
    ):
        if type(approval[key]) is not int or approval[key] <= 0:
            raise ValueError(f"invalid authorization limit: {key}")
    if approval["max_requests_per_run"] < 3 or activation["max_operations"] < 3:
        raise ValueError("three distinct operations are required")
    try:
        budget = Decimal(approval["billing_budget_usd"])
    except (InvalidOperation, TypeError):
        raise ValueError("invalid billing budget") from None
    if not budget.is_finite() or budget <= 0:
        raise ValueError("invalid billing budget")


def reserve_aggregate(path, approval, run_id):
    """Durable conservative reservation; crashes and failed runs consume three slots."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
    _protected(path, 0o600, os.geteuid())
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS policy (value TEXT NOT NULL)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, slots INTEGER NOT NULL)"
        )
        policy = json.dumps(approval, sort_keys=True, separators=(",", ":"))
        existing = db.execute("SELECT value FROM policy").fetchall()
        if not existing:
            db.execute("INSERT INTO policy VALUES (?)", (policy,))
        elif existing != [(policy,)]:
            raise ValueError("aggregate policy changed; preserve and review ledger")
        used = db.execute("SELECT COALESCE(SUM(slots),0) FROM runs").fetchone()[0]
        if used + 3 > approval["max_aggregate_operations"]:
            raise ValueError("aggregate operation allowance exhausted")
        db.execute("INSERT INTO runs VALUES (?,3)", (run_id,))
    os.chmod(path, 0o600)
    return approval["max_aggregate_operations"] - used - 3


def _command(args, env, *, input_text=None, timeout=120):
    result = subprocess.run(
        args,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args[:5])}"
        )
    return result.stdout.strip()


def _compose(env, *args, input_text=None, timeout=120):
    return _command((*COMPOSE, *args), env, input_text=input_text, timeout=timeout)


def _app_python(env, code, input_text=None):
    return _compose(
        env,
        "run",
        "--rm",
        "--no-deps",
        "-i",
        "private-app",
        "python",
        "-B",
        "-c",
        code,
        input_text=input_text,
    )


def _worker_python(env, code):
    return _compose(
        env,
        "run",
        "--rm",
        "--no-deps",
        "provider-worker",
        "python",
        "-B",
        "-c",
        code,
    )


def _owner_change(env, password, active):
    code = (
        "import sys; from app.owner_control.store import OwnerStore; "
        "from app.private_web.auth import LoginStatus; "
        "s=OwnerStore('/var/lib/pilot-app/owner'); "
        "r=s.login('synthetic-operator',sys.stdin.readline().rstrip('\\n'),'127.0.0.1'); "
        "assert r.status is LoginStatus.SUCCESS; "
        f"assert s.change(r.session_token,s.issue(r.session_token),{active})=='applied'"
    )
    _app_python(env, code, password + "\n")


def _stop_file(path, active):
    _write_control(
        path,
        json.dumps(
            {"version": "private-pilot-emergency-stop-v1", "stop": active}
        ).encode(),
        WORKER_UID,
    )


class HTTPSParticipant:
    def __init__(self, origin, username):
        self.origin = origin
        self.username = username
        self.jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))

    def request(self, method, path, payload=None):
        cookies = {cookie.name: cookie.value for cookie in self.jar}
        headers = {"Origin": self.origin}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            headers.update(
                {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": cookies.get("ar_csrf", ""),
                }
            )
        response = self.opener.open(
            Request(self.origin + path, data=data, headers=headers, method=method),
            timeout=55,
        )
        return json.loads(response.read()) if path.startswith("/api/") else None

    def login(self, password):
        self.request("GET", "/login")
        csrf = next(
            cookie.value for cookie in self.jar if cookie.name == "ar_login_csrf"
        )
        data = urlencode(
            {"username": self.username, "password": password, "csrf": csrf}
        ).encode()
        self.opener.open(
            Request(
                self.origin + "/login",
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": self.origin,
                },
                method="POST",
            ),
            timeout=20,
        ).read()


def _wait_http_ready(participant):
    deadline = time.monotonic() + 15
    while True:
        try:
            participant.request("GET", "/login")
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def _usage(env):
    code = (
        "import json,sqlite3; "
        "d=sqlite3.connect('file:/var/lib/pilot-worker/provider.sqlite3?mode=ro',uri=True); "
        "r=d.execute('SELECT attempt_id,correlation_id,outcome,rejection_reason,input_tokens,output_tokens,total_tokens,retry_count,estimated_cost,cost_estimate_status FROM provider_usage_attempts').fetchall(); "
        "p=d.execute('SELECT attempt_id FROM provider_attempt_reservations').fetchall(); "
        "print(json.dumps({'attempts':r,'reservations':[x[0] for x in p]}))"
    )
    return json.loads(_worker_python(env, code))


def _allowance(env):
    code = (
        "import json,sqlite3; "
        "d=sqlite3.connect('file:/var/lib/pilot-worker/allowance.sqlite3?mode=ro',uri=True); "
        "print(json.dumps({'maximum':d.execute('SELECT max_operations FROM authority').fetchone()[0],"
        "'consumed':[x[0] for x in d.execute('SELECT operation_id FROM consumed')]}))"
    )
    return json.loads(_worker_python(env, code))


def _source(env, message_id):
    code = (
        "import json,sqlite3,sys; "
        "d=sqlite3.connect('file:/var/lib/pilot-app/events.db?mode=ro',uri=True); "
        "r=d.execute('SELECT event_id,correlation_id FROM event_envelopes WHERE source_message_id=?',"
        "(sys.stdin.readline().rstrip('\\n'),)).fetchall(); print(json.dumps(r))"
    )
    rows = json.loads(_app_python(env, code, message_id + "\n"))
    if len(rows) != 1:
        raise ValueError("current message identity is unresolved")
    return "reply:" + hashlib.sha256(rows[0][0].encode()).hexdigest(), rows[0][1]


def _reconcile(env, baseline, expected, approval, *, final=False):
    usage = _usage(env)
    allowance = _allowance(env)
    rows = usage["attempts"]
    ids = [row[0] for row in rows]
    consumed = allowance["consumed"]
    if len(ids) != len(set(ids)) or len(consumed) != len(set(consumed)):
        raise ValueError("duplicate accounting identity")
    if usage["reservations"]:
        raise ValueError("unresolved provider reservation")
    if any(row[4] is None or row[5] is None or row[6] is None for row in rows):
        raise ValueError("provider token usage is unavailable")
    totals = (sum(row[4] for row in rows), sum(row[5] for row in rows))
    limits = (
        approval["max_aggregate_input_tokens"],
        approval["max_aggregate_output_tokens"],
    )
    if any(
        value > limit if final else value >= limit
        for value, limit in zip(totals, limits)
    ):
        raise ValueError("aggregate token allowance exhausted")
    if not baseline["attempts"] <= set(ids) or not baseline["consumed"] <= set(
        consumed
    ):
        raise ValueError("historical accounting changed")
    if any(
        tuple(row) != baseline["rows"][row[0]]
        for row in rows
        if row[0] in baseline["rows"]
    ):
        raise ValueError("historical accounting changed")
    current = [row for row in rows if row[0] not in baseline["attempts"]]
    current_consumed = set(consumed) - baseline["consumed"]
    required = {operation + ":1": correlation for operation, correlation in expected}
    if len(required) != len(expected):
        raise ValueError("duplicate current operation")
    if set(row[0] for row in current) != set(required) or current_consumed != {
        operation for operation, _ in expected
    }:
        raise ValueError("current provider or allowance accounting does not match")
    if any(
        row[1] != required[row[0]]
        or row[2] != "completed"
        or row[3] is not None
        or row[7] != 0
        for row in current
    ):
        raise ValueError("current provider attempt is incomplete or retried")
    if allowance["maximum"] != baseline["maximum"]:
        raise ValueError("allowance authority changed")
    remaining = allowance["maximum"] - len(consumed)
    if remaining < (0 if final else 3 - len(expected)):
        raise ValueError("worker allowance exhausted")
    if final and (
        len(expected) != 3 or len(current) != 3 or len(current_consumed) != 3
    ):
        raise ValueError("three current attempts are required")
    return rows, current, remaining


def run(args):
    repo = Path(__file__).resolve().parents[1]
    if os.geteuid() != 0 or Path.cwd().resolve() != repo:
        raise ValueError("run as root from the reviewed repository checkout")
    commit = _command(("git", "rev-parse", "HEAD"), os.environ.copy())
    if _command(("git", "status", "--porcelain"), os.environ.copy()):
        raise ValueError("checkout must be clean")
    approval = _unique_json(_read_protected(args.authorization, 0o400, 0))
    activation_bytes = _read_protected(args.activation, 0o400, 0)
    activation = _unique_json(activation_bytes)
    validate_authorization(approval, activation, commit, datetime.now(UTC))
    if (
        urlsplit(args.origin).scheme != "https"
        or urlsplit(args.origin).hostname is None
    ):
        raise ValueError("an approved HTTPS origin is required")
    backend_subnet, backend_ip = _backend_network(args.backend_subnet, args.backend_ip)
    _check_backend_conflicts(backend_subnet, os.environ.copy())
    args.report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _protected_parents(args.report, os.geteuid())
    if args.report.exists() or args.report.is_symlink():
        _protected(args.report, 0o600, os.geteuid())
    _prepare_state_directory(args.state_root)
    if args.aggregate_ledger.exists() or args.aggregate_ledger.is_symlink():
        _protected(args.aggregate_ledger, 0o600, 0)
    run_id = uuid4().hex
    participant_username = "synthetic-a-" + run_id[:12]
    participant_id = "synthetic-participant-" + run_id[:12]
    socket_dir = args.state_root / "runs" / run_id / "ipc"
    worker_dir = args.state_root / "worker"
    control_dir = args.state_root / "control"
    secret_dir = args.state_root / "secrets"
    app_dir = args.state_root / "runs" / run_id / "app"
    _protected(secret_dir / "openai-api-key", 0o400, WORKER_UID)
    _prepare_state_directory(args.state_root / "runs")
    _prepare_state_directory(args.state_root / "runs" / run_id)
    for path, uid, gid, mode in (
        (socket_dir, WORKER_UID, 11020, 0o710),
        (worker_dir, WORKER_UID, WORKER_UID, 0o700),
        (control_dir, WORKER_UID, WORKER_UID, 0o700),
        (app_dir, APP_UID, APP_UID, 0o700),
    ):
        _prepare_service_directory(path, uid, gid, mode)
    activation_target = control_dir / "activation.json"
    if activation_target.exists() or activation_target.is_symlink():
        if _read_protected(activation_target, 0o400, WORKER_UID) != activation_bytes:
            raise ValueError("existing allowance is bound to another activation")
    else:
        _write_control(
            activation_target, activation_bytes, WORKER_UID, create_only=True
        )
    stop = control_dir / "emergency-stop.json"
    if stop.exists() or stop.is_symlink():
        _protected(stop, 0o400, WORKER_UID)
    _stop_file(stop, True)
    env = os.environ.copy()
    env.update(
        {
            "PILOT_SOCKET_DIR": str(socket_dir),
            "PILOT_WORKER_STATE_DIR": str(worker_dir),
            "PILOT_APP_STATE_DIR": str(app_dir),
            "PILOT_CONTROL_DIR": str(control_dir),
            "PILOT_SECRET_DIR": str(secret_dir),
            "PILOT_ALLOWED_HOST": urlsplit(args.origin).hostname,
            "PILOT_ALLOWED_ORIGIN": args.origin,
            "PILOT_APP_SUBNET": str(backend_subnet),
            "PILOT_APP_IP": str(backend_ip),
            "COMPOSE_PROJECT_NAME": "almost-reasonable-ship-" + run_id[:12],
        }
    )
    report = {
        "commit": commit,
        "run_id": run_id,
        "status": "failed",
        "steps": [],
        "images": {},
        "compose_project": env["COMPOSE_PROJECT_NAME"],
    }
    owner_password = None
    try:
        owner_password = getpass("Synthetic owner password: ")
        participant_password = getpass("Synthetic participant password: ")
        _compose(env, "config", "--quiet")
        _compose(env, "build", timeout=600)
        for image in (
            "almost-reasonable-private-pilot-worker:local",
            "almost-reasonable-private-pilot-app:local",
        ):
            report["images"][image] = _command(
                ("docker", "image", "inspect", "--format", "{{.Id}}", image), env
            )
        _app_python(
            env,
            "from app.private_web.auth import AuthStore; from app.core.store import EventStore; from app.privacy.ledger import PrivacyLedger; from app.owner_control.store import OwnerStore; p='/var/lib/pilot-app/'; AuthStore(p+'auth.db'); EventStore(p+'events.db'); PrivacyLedger(p+'ledger.db'); EventStore(p+'ipc-state.db'); OwnerStore(p+'owner')",
        )
        _worker_python(
            env,
            "from pathlib import Path; from app.core.store import EventStore; EventStore(Path('/var/lib/pilot-worker/provider.sqlite3'))",
        )
        _app_python(
            env,
            "import sys; from app.owner_control.store import OwnerStore; OwnerStore('/var/lib/pilot-app/owner').provision('synthetic-operator',sys.stdin.readline().rstrip('\\n'))",
            owner_password + "\n",
        )
        _app_python(
            env,
            "import sys; from app.private_web.auth import AuthStore; "
            f"AuthStore('/var/lib/pilot-app/auth.db').provision_account('{participant_username}',"
            f"sys.stdin.readline().rstrip('\\n'),'{participant_id}')",
            participant_password + "\n",
        )
        _owner_change(env, owner_password, True)
        _compose(env, "up", "-d", "private-app")
        participant = HTTPSParticipant(args.origin, participant_username)
        try:
            _wait_http_ready(participant)
            participant.login(participant_password)
        except OSError as error:
            raise BackendReadinessError(
                "acceptance HTTPS route unavailable before paid activation"
            ) from error
        if participant.request("GET", "/session") != {
            "authenticated": True,
            "username": participant_username,
        }:
            raise BackendReadinessError(
                "HTTPS route does not reach this acceptance app"
            )
        report["backend_ready"] = True
        delay = (_utc(activation["not_before"]) - datetime.now(UTC)).total_seconds()
        if delay > 600:
            raise ValueError("activation window starts too far in the future")
        if delay > 0:
            time.sleep(delay)
        validate_authorization(approval, activation, commit, datetime.now(UTC))
        report["aggregate_remaining"] = reserve_aggregate(
            args.aggregate_ledger, approval, run_id
        )
        if not (worker_dir / "allowance.sqlite3").exists():
            _compose(
                env,
                "run",
                "--rm",
                "--no-deps",
                "provider-worker",
                "python",
                "-B",
                "-m",
                "app.ops.private_pilot_ops",
                "prepare",
                "--activation",
                "/run/almost-reasonable-private-pilot/control/activation.json",
                "--allowance",
                "/var/lib/pilot-worker/allowance.sqlite3",
            )
        initial_usage = _usage(env)
        initial_allowance = _allowance(env)
        baseline = {
            "attempts": {row[0] for row in initial_usage["attempts"]},
            "rows": {row[0]: tuple(row) for row in initial_usage["attempts"]},
            "consumed": set(initial_allowance["consumed"]),
            "maximum": initial_allowance["maximum"],
        }
        expected = []
        _reconcile(env, baseline, expected, approval)
        _owner_change(env, owner_password, False)
        _stop_file(stop, False)
        _compose(env, "up", "-d", "provider-worker", "private-app")
        _wait_http_ready(participant)
        participant.request(
            "POST", "/api/privacy/notice", {"version": "privacy-notice-ru-v1"}
        )

        def send(message_id, content):
            validate_authorization(approval, activation, commit, datetime.now(UTC))
            _reconcile(env, baseline, expected, approval)
            source_id = run_id + "-" + message_id
            response = participant.request(
                "POST",
                "/api/messages",
                {"message_id": source_id, "content": content},
            )
            expected.append(_source(env, source_id))
            _reconcile(env, baseline, expected, approval, final=len(expected) == 3)
            return response

        def history():
            return participant.request("GET", "/api/history")["messages"]

        def restart_app():
            _compose(env, "restart", "private-app")
            try:
                _wait_http_ready(participant)
            except OSError as error:
                raise BackendReadinessError(
                    "acceptance HTTPS route unavailable after app restart"
                ) from error
            if participant.request("GET", "/session") != {
                "authenticated": True,
                "username": participant_username,
            }:
                raise BackendReadinessError("HTTPS route changed after app restart")

        journey = run_ship_journey(
            send,
            history,
            restart_app,
            record_step=report["steps"].append,
            record_name=lambda name: report.update(ship_name=name),
        )
        report.update(journey)
        report["status"] = "passed"
    except (Exception, KeyboardInterrupt) as error:
        report["error"] = type(error).__name__
        if isinstance(error, BackendReadinessError):
            report["backend_error"] = str(error)
        raise
    finally:
        try:
            _stop_file(stop, True)
            report["worker_stop"] = json.loads(stop.read_text())["stop"] is True
        except Exception:
            report["worker_stop"] = False
        if owner_password is not None:
            try:
                _owner_change(env, owner_password, True)
            except Exception:
                report["owner_stop_error"] = True
        try:
            _compose(env, "stop", "private-app", "provider-worker")
            report["running_services"] = _compose(
                env, "ps", "--status", "running", "--services"
            ).splitlines()
        except Exception:
            report["container_stop_error"] = True
        try:
            usage = _usage(env)
            rows = usage["attempts"]
            current = (
                [row for row in rows if row[0] not in baseline["attempts"]]
                if "baseline" in locals()
                else rows
            )
            report["provider_attempts"] = len(current)
            report["rejection_reasons"] = sorted({row[3] for row in current if row[3]})
            final_allowance = _allowance(env)
            report["remaining_allowance"] = final_allowance["maximum"] - len(
                final_allowance["consumed"]
            )
            report["usage_unavailable"] = any(
                row[4] is None or row[5] is None or row[6] is None for row in rows
            )
            if report["usage_unavailable"]:
                report["input_tokens"] = None
                report["output_tokens"] = None
                report["total_tokens"] = None
                report["aggregate_input_tokens"] = None
                report["aggregate_output_tokens"] = None
                report["accounting_error"] = True
            else:
                report["input_tokens"] = sum(row[4] for row in current)
                report["output_tokens"] = sum(row[5] for row in current)
                report["total_tokens"] = (
                    None
                    if any(row[6] is None for row in current)
                    else sum(row[6] for row in current)
                )
                report["aggregate_input_tokens"] = sum(row[4] for row in rows)
                report["aggregate_output_tokens"] = sum(row[5] for row in rows)
                if (
                    report["aggregate_input_tokens"]
                    > approval["max_aggregate_input_tokens"]
                    or report["aggregate_output_tokens"]
                    > approval["max_aggregate_output_tokens"]
                ):
                    report["token_limit_exceeded"] = True
            report["estimated_cost_total"] = (
                str(sum((Decimal(row[8]) for row in current), Decimal(0)))
                if current and all(row[8] is not None for row in current)
                else None
            )
            report["cost_estimate_statuses"] = sorted({row[9] for row in current})
            if "baseline" in locals():
                _reconcile(env, baseline, expected, approval, final=True)
            else:
                report["accounting_error"] = True
        except Exception:
            report["accounting_error"] = True
        try:
            report["owner_stop"] = (
                _app_python(
                    env,
                    "from app.owner_control.store import OwnerStore; print(OwnerStore('/var/lib/pilot-app/owner').active())",
                )
                == "True"
            )
        except Exception:
            report["owner_stop"] = False
        if (
            not report["worker_stop"]
            or not report["owner_stop"]
            or report.get("running_services")
            or report.get("container_stop_error")
            or report.get("accounting_error")
            or report.get("token_limit_exceeded")
        ):
            report["status"] = "failed"
        _write_report(args.report, report)
    if report["status"] != "passed":
        raise RuntimeError("acceptance or cleanup failed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in (
        "authorization",
        "activation",
        "state-root",
        "aggregate-ledger",
        "origin",
        "backend-subnet",
        "backend-ip",
        "report",
    ):
        parser.add_argument(
            "--" + flag,
            required=True,
            type=str if flag in {"origin", "backend-subnet", "backend-ip"} else Path,
        )
    args = parser.parse_args(argv)
    try:
        run(args)
    except (BackendConfigurationError, BackendReadinessError) as error:
        print(f"Acceptance failed: {error}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError, KeyboardInterrupt) as error:
        print(
            f"Acceptance failed: {type(error).__name__}; inspect protected report",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
