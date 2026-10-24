"""Explicitly authorized, synthetic private-pilot acceptance on an approved host."""

import argparse
from contextlib import contextmanager
import hashlib
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from getpass import getpass
from http.cookiejar import CookieJar
from ipaddress import ip_address, ip_network
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

from acceptance.private_pilot_conversation import run_ship_journey
from acceptance.private_memory_journey import MAX_OPERATIONS, run_robot_journey
from app.trusted.private_pilot import MEMORY_ACTIVATION_VERSION


COMPOSE = (
    "docker",
    "compose",
    "-f",
    "ops/compose.private-pilot.yaml",
    "-f",
    "ops/compose.private-pilot-acceptance.yaml",
)
TASK = "synthetic-private-pilot-ship-restart-mars"
MEMORY_TASK = "synthetic-private-robot-memory-b03d"
MODEL = "gpt-6-luna"
APP_UID = 11022
WORKER_UID = 11021


class BackendConfigurationError(ValueError):
    """Safe operator-facing network configuration failure."""


class BackendReadinessError(ValueError):
    """Safe operator-facing HTTPS route failure."""


@contextmanager
def _ignore_sigint(on_interrupt=None):
    handler = (
        signal.SIG_IGN
        if on_interrupt is None
        else lambda _signum, _frame: on_interrupt()
    )
    previous = signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _first_sigint(_signum, _frame):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise KeyboardInterrupt


def _progress(path, report, phase):
    report["phase"] = phase
    print(f"Acceptance phase: {phase}", file=sys.stderr, flush=True)
    _write_report(path, report)


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


def _checked_directory(path, uid, mode):
    _protected_parents(path, uid)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        entry = os.fstat(fd)
        if entry.st_uid != uid or stat.S_IMODE(entry.st_mode) != mode:
            raise ValueError("zero-call continuation has an unsafe state directory")
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


def validate_authorization(approval, activation, commit, now, *, memory=False):
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
    if set(approval) != required or approval["task"] != (
        MEMORY_TASK if memory else TASK
    ):
        raise ValueError("explicit task authorization is missing")
    if approval["commit"] != commit or approval["model"] != MODEL:
        raise ValueError("commit or model is not authorized")
    if memory and (
        activation.get("version") != MEMORY_ACTIVATION_VERSION
        or activation.get("task") != MEMORY_TASK
    ):
        raise ValueError("memory acceptance activation is missing")
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
    minimum = MAX_OPERATIONS if memory else 3
    if (
        approval["max_requests_per_run"] < minimum
        or activation["max_operations"] < minimum
    ):
        raise ValueError("distinct operation allowance is insufficient")
    try:
        budget = Decimal(approval["billing_budget_usd"])
    except (InvalidOperation, TypeError):
        raise ValueError("invalid billing budget") from None
    if not budget.is_finite() or budget <= 0:
        raise ValueError("invalid billing budget")


def reserve_aggregate(path, approval, run_id, *, slots=3):
    """Durable conservative reservation; crashes and failed runs consume slots."""

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
        if used + slots > approval["max_aggregate_operations"]:
            raise ValueError("aggregate operation allowance exhausted")
        db.execute("INSERT INTO runs VALUES (?,?)", (run_id, slots))
    os.chmod(path, 0o600)
    return approval["max_aggregate_operations"] - used - slots


def _continuation_rows(path, uid, queries):
    _protected(path, 0o600, uid)
    if any(
        Path(str(path) + suffix).exists() for suffix in ("-journal", "-wal", "-shm")
    ):
        raise ValueError("zero-call continuation has unresolved database state")
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("zero-call continuation database integrity is uncertain")
        return [db.execute(query).fetchall() for query in queries]


def _verify_zero_call_continuation(
    args, approval, activation_bytes, old_run_id, new_run_id=None
):
    """Read only: require the one preserved, stopped, zero-call interrupted run."""

    try:
        if re.fullmatch(r"[0-9a-f]{32}", old_run_id) is None:
            raise ValueError("invalid interrupted run ID")
        if (
            approval["max_aggregate_operations"] != 3
            or approval["max_requests_per_run"] != 3
            or approval["max_aggregate_input_tokens"] != 6000
            or approval["max_aggregate_output_tokens"] != 1536
            or Decimal(approval["billing_budget_usd"]) != Decimal("0.10")
            or _unique_json(activation_bytes)["max_operations"] != 3
        ):
            raise ValueError("different paid limits")
        if args.previous_report.absolute() == args.report.absolute():
            raise ValueError("previous report would be overwritten")
        root = args.state_root
        old = root / "runs" / old_run_id
        app = old / "app"
        for path, uid, mode in (
            (root, 0, 0o700),
            (root / "runs", 0, 0o700),
            (old, 0, 0o700),
            (app, APP_UID, 0o700),
            (app / "owner", APP_UID, 0o700),
            (root / "worker", WORKER_UID, 0o700),
            (root / "control", WORKER_UID, 0o700),
        ):
            _checked_directory(path, uid, mode)
        expected_runs = {old_run_id} if new_run_id is None else {old_run_id, new_run_id}
        if {path.name for path in (root / "runs").iterdir()} != expected_runs:
            raise ValueError("another acceptance run exists")
        control = root / "control"
        if (
            _read_protected(control / "activation.json", 0o400, WORKER_UID)
            != activation_bytes
        ):
            raise ValueError("activation changed")
        if _unique_json(
            _read_protected(control / "emergency-stop.json", 0o400, WORKER_UID)
        ) != {"version": "private-pilot-emergency-stop-v1", "stop": True}:
            raise ValueError("worker Stop is open")
        (owner,) = _continuation_rows(
            app / "owner" / "owner.sqlite",
            APP_UID,
            ("SELECT singleton, active FROM owner_stop",),
        )
        if owner != [(1, 1)]:
            raise ValueError("application Owner Stop is open")
        attempts, reservations = _continuation_rows(
            root / "worker" / "provider.sqlite3",
            WORKER_UID,
            (
                "SELECT attempt_id FROM provider_usage_attempts",
                "SELECT attempt_id FROM provider_attempt_reservations",
            ),
        )
        authority, consumed = _continuation_rows(
            root / "worker" / "allowance.sqlite3",
            WORKER_UID,
            (
                "SELECT digest, max_operations FROM authority",
                "SELECT operation_id FROM consumed",
            ),
        )
        if (
            attempts
            or reservations
            or consumed
            or authority != [(hashlib.sha256(activation_bytes).hexdigest(), 3)]
        ):
            raise ValueError("provider or allowance is not verified zero-call")
        policy, runs = _continuation_rows(
            args.aggregate_ledger,
            0,
            ("SELECT value FROM policy", "SELECT run_id, slots FROM runs"),
        )
        if policy != [
            (json.dumps(approval, sort_keys=True, separators=(",", ":")),)
        ] or runs != [(old_run_id, 3)]:
            raise ValueError("aggregate history differs from the interrupted run")
        (existing_receipt,) = _continuation_rows(
            args.aggregate_ledger,
            0,
            (
                "SELECT name FROM sqlite_master WHERE type='table' AND name='zero_call_continuations'",
            ),
        )
        if existing_receipt:
            raise ValueError("zero-call interruption was already continued")
        _protected_parents(args.previous_report, 0)
        if args.previous_report.is_symlink():
            raise ValueError("unsafe previous report")
        if args.previous_report.exists():
            previous = _unique_json(_read_protected(args.previous_report, 0o600, 0))
            if (
                previous.get("run_id") != old_run_id
                or previous.get("error") != "KeyboardInterrupt"
                or previous.get("status") not in {"failed", "interrupted"}
                or previous.get("provider_attempts") != 0
                or previous.get("worker_stop") is not True
                or previous.get("owner_stop") is not True
                or previous.get("running_services") != []
                or previous.get("accounting_error") is True
            ):
                raise ValueError("previous report contradicts zero-call state")
        env = os.environ.copy()
        env.update(
            COMPOSE_PROJECT_NAME="almost-reasonable-ship-" + old_run_id[:12],
            PILOT_SOCKET_DIR=str(old / "ipc"),
            PILOT_WORKER_STATE_DIR=str(root / "worker"),
            PILOT_APP_STATE_DIR=str(app),
            PILOT_CONTROL_DIR=str(control),
            PILOT_SECRET_DIR=str(root / "secrets"),
            PILOT_ALLOWED_HOST=urlsplit(args.origin).hostname or "",
            PILOT_ALLOWED_ORIGIN=args.origin,
            PILOT_APP_SUBNET=args.backend_subnet,
            PILOT_APP_IP=args.backend_ip,
        )
        services = _compose(env, "ps", "--all", "--services").splitlines()
        running = _compose(env, "ps", "--status", "running", "--services").splitlines()
        if (
            set(services) != {"private-app", "provider-worker"}
            or len(services) != 2
            or running
        ):
            raise ValueError("acceptance containers are active or missing")
    except (
        OSError,
        sqlite3.Error,
        KeyError,
        TypeError,
        ValueError,
        InvalidOperation,
    ) as error:
        raise ValueError(
            "zero-call continuation evidence is missing or inconsistent"
        ) from error


def _claim_zero_call_continuation(
    ledger, approval, old_run_id, new_run_id, commit, activation_bytes
):
    """Atomically bind one new run to the old three slots without adding capacity."""

    _protected(ledger, 0o600, 0)
    with sqlite3.connect(ledger) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT value FROM policy").fetchall() != [
            (json.dumps(approval, sort_keys=True, separators=(",", ":")),)
        ] or db.execute("SELECT run_id, slots FROM runs").fetchall() != [
            (old_run_id, 3)
        ]:
            raise ValueError("zero-call continuation aggregate history changed")
        db.execute(
            "CREATE TABLE IF NOT EXISTS zero_call_continuations ("
            "original_run_id TEXT PRIMARY KEY, continuation_run_id TEXT UNIQUE NOT NULL, "
            "authorized_commit TEXT NOT NULL, reviewed_commit TEXT NOT NULL, "
            "activation_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        if db.execute("SELECT 1 FROM zero_call_continuations").fetchone() is not None:
            raise ValueError("zero-call interruption was already continued")
        receipt = {
            "original_run_id": old_run_id,
            "continuation_run_id": new_run_id,
            "authorized_commit": approval["commit"],
            "reviewed_commit": commit,
            "activation_sha256": hashlib.sha256(activation_bytes).hexdigest(),
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        db.execute(
            "INSERT INTO zero_call_continuations VALUES (?, ?, ?, ?, ?, ?)",
            tuple(receipt.values()),
        )
    return receipt


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
    command = COMPOSE
    if env.get("PILOT_MEMORY_JOURNEY") == "1":
        command = (
            *COMPOSE,
            "-f",
            "ops/compose.private-pilot-memory-acceptance.yaml",
        )
    return _command((*command, *args), env, input_text=input_text, timeout=timeout)


def _app_python(env, code, input_text=None, *, running=False, timeout=120):
    target = (
        ("exec", "-T", "private-app")
        if running
        else ("run", "--rm", "--no-deps", "-i", "private-app")
    )
    return _compose(
        env,
        *target,
        "python",
        "-B",
        "-c",
        code,
        input_text=input_text,
        timeout=timeout,
    )


def _worker_python(env, code, *, timeout=120):
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
        timeout=timeout,
    )


def _owner_change(env, password, active, *, running=False, timeout=120):
    code = (
        "import sys; from app.owner_control.store import OwnerStore; "
        "from app.private_web.auth import LoginStatus; "
        "s=OwnerStore('/var/lib/pilot-app/owner'); "
        "r=s.login('synthetic-operator',sys.stdin.readline().rstrip('\\n'),'127.0.0.1'); "
        "assert r.status is LoginStatus.SUCCESS; "
        f"assert s.change(r.session_token,s.issue(r.session_token),{active})=='applied'"
    )
    _app_python(env, code, password + "\n", running=running, timeout=timeout)


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
        return (
            json.loads(response.read())
            if path == "/session" or path.startswith("/api/")
            else None
        )

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


def _wait_worker_ready(env):
    code = (
        "import socket; "
        "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); "
        "s.settimeout(1); s.connect('/run/pilot-ipc/worker.sock'); s.close()"
    )
    deadline = time.monotonic() + 15
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BackendReadinessError(
                "provider worker socket unavailable inside acceptance app"
            )
        try:
            _app_python(env, code, running=True, timeout=min(2, remaining))
            return
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            if time.monotonic() >= deadline:
                raise BackendReadinessError(
                    "provider worker socket unavailable inside acceptance app"
                ) from error
            time.sleep(0.25)


def _usage(env, *, timeout=120):
    code = (
        "import json,sqlite3; "
        "d=sqlite3.connect('file:/var/lib/pilot-worker/provider.sqlite3?mode=ro',uri=True); "
        "r=d.execute('SELECT attempt_id,correlation_id,outcome,rejection_reason,input_tokens,output_tokens,total_tokens,retry_count,estimated_cost,cost_estimate_status FROM provider_usage_attempts').fetchall(); "
        "p=d.execute('SELECT attempt_id FROM provider_attempt_reservations').fetchall(); "
        "print(json.dumps({'attempts':r,'reservations':[x[0] for x in p]}))"
    )
    return json.loads(_worker_python(env, code, timeout=timeout))


def _allowance(env, *, timeout=120):
    code = (
        "import json,sqlite3; "
        "d=sqlite3.connect('file:/var/lib/pilot-worker/allowance.sqlite3?mode=ro',uri=True); "
        "print(json.dumps({'maximum':d.execute('SELECT max_operations FROM authority').fetchone()[0],"
        "'consumed':[x[0] for x in d.execute('SELECT operation_id FROM consumed')]}))"
    )
    return json.loads(_worker_python(env, code, timeout=timeout))


def _source(env, message_id):
    code = (
        "import json,sqlite3,sys; "
        "d=sqlite3.connect('file:/var/lib/pilot-app/events.db?mode=ro',uri=True); "
        "r=d.execute('SELECT event_id,correlation_id FROM event_envelopes WHERE source_message_id=?',"
        "(sys.stdin.readline().rstrip('\\n'),)).fetchall(); print(json.dumps(r))"
    )
    rows = json.loads(_app_python(env, code, message_id + "\n", running=True))
    if len(rows) != 1:
        raise ValueError("current message identity is unresolved")
    return "reply:" + hashlib.sha256(rows[0][0].encode()).hexdigest(), rows[0][1]


def _reconcile(
    env, baseline, expected, approval, *, final=False, timeout=120, required=3
):
    usage = _usage(env, timeout=timeout)
    allowance = _allowance(env, timeout=timeout)
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
    required_ids = {
        operation + ":1": correlation for operation, correlation in expected
    }
    if len(required_ids) != len(expected):
        raise ValueError("duplicate current operation")
    if set(row[0] for row in current) != set(required_ids) or current_consumed != {
        operation for operation, _ in expected
    }:
        raise ValueError("current provider or allowance accounting does not match")
    if any(
        row[1] != required_ids[row[0]]
        or row[2] != "completed"
        or row[3] is not None
        or row[7] != 0
        for row in current
    ):
        raise ValueError("current provider attempt is incomplete or retried")
    if allowance["maximum"] != baseline["maximum"]:
        raise ValueError("allowance authority changed")
    remaining = allowance["maximum"] - len(consumed)
    if remaining < (0 if final else required - len(expected)):
        raise ValueError("worker allowance exhausted")
    if final and (
        len(expected) != required
        or len(current) != required
        or len(current_consumed) != required
    ):
        raise ValueError("required current attempts are missing")
    return rows, current, remaining


def run(args):
    memory = getattr(args, "memory_journey", False)
    required = MAX_OPERATIONS if memory else 3
    repo = Path(__file__).resolve().parents[1]
    if os.geteuid() != 0 or Path.cwd().resolve() != repo:
        raise ValueError("run as root from the reviewed repository checkout")
    commit = _command(("git", "rev-parse", "HEAD"), os.environ.copy())
    if _command(("git", "status", "--porcelain"), os.environ.copy()):
        raise ValueError("checkout must be clean")
    approval = _unique_json(_read_protected(args.authorization, 0o400, 0))
    activation_bytes = _read_protected(args.activation, 0o400, 0)
    activation = _unique_json(activation_bytes)
    continuation_of = getattr(args, "continue_zero_call", None)
    if memory and continuation_of is not None:
        raise ValueError("memory journey does not support zero-call continuation")
    reviewed_commit = getattr(args, "reviewed_commit", None)
    previous_report = getattr(args, "previous_report", None)
    if continuation_of is None:
        if reviewed_commit is not None or previous_report is not None:
            raise ValueError("continuation options must be used together")
        authorized_commit = commit
    else:
        if (
            reviewed_commit != commit
            or re.fullmatch(r"[0-9a-f]{40}", reviewed_commit or "") is None
            or previous_report is None
        ):
            raise ValueError(
                "explicit current reviewed commit and previous report are required"
            )
        authorized_commit = approval.get("commit")

    def validate_current():
        if memory:
            validate_authorization(
                approval, activation, authorized_commit, datetime.now(UTC), memory=True
            )
        else:
            validate_authorization(
                approval, activation, authorized_commit, datetime.now(UTC)
            )

    validate_current()
    if continuation_of is not None:
        if (
            not _utc(activation["not_before"])
            <= datetime.now(UTC)
            < _utc(activation["expires_at"])
        ):
            raise ValueError("zero-call continuation activation window expired")
        _verify_zero_call_continuation(
            args, approval, activation_bytes, continuation_of
        )
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
        if continuation_of is not None:
            raise ValueError("zero-call continuation needs a new report path")
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
    if memory:
        for name, version in (
            ("memory-evolution.json", "private-memory-evolution-permission-v1"),
            ("typed-memory.json", "private-typed-memory-permission-v1"),
        ):
            path = control_dir / name
            if path.exists() or path.is_symlink():
                raise ValueError("memory acceptance requires fresh permission files")
            _write_control(
                path,
                json.dumps(
                    {
                        "version": version,
                        "pilot_id": activation["pilot_id"],
                        "max_operations": required,
                    },
                    separators=(",", ":"),
                ).encode(),
                WORKER_UID,
                create_only=True,
            )
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
    if memory:
        env["PILOT_MEMORY_JOURNEY"] = "1"
    report = {
        "commit": commit,
        "run_id": run_id,
        "status": "in_progress",
        "steps": [],
        "images": {},
        "compose_project": env["COMPOSE_PROJECT_NAME"],
    }
    if continuation_of is not None:
        report["continuation_of"] = continuation_of
    owner_password = None
    app_started = False
    previous_sigint = signal.signal(signal.SIGINT, _first_sigint)
    try:
        _progress(args.report, report, "passwords")
        owner_password = getpass("Synthetic owner password: ")
        participant_password = getpass("Synthetic participant password: ")
        _progress(args.report, report, "preparation")
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
        _progress(args.report, report, "readiness")
        app_started = True
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
        if not (worker_dir / "allowance.sqlite3").exists():
            command = (
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
            _compose(
                env,
                *command,
                *(("--memory-acceptance",) if memory else ()),
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
        _reconcile(env, baseline, expected, approval, required=required)
        if continuation_of is not None:
            _verify_zero_call_continuation(
                args, approval, activation_bytes, continuation_of, run_id
            )
        _compose(env, "up", "-d", "provider-worker", "private-app")
        _wait_worker_ready(env)
        report["worker_ready"] = True
        _wait_http_ready(participant)
        validate_current()
        if continuation_of is not None and datetime.now(UTC) >= _utc(
            activation["expires_at"]
        ):
            raise ValueError("zero-call continuation activation window expired")
        if continuation_of is None:
            report["aggregate_remaining"] = (
                reserve_aggregate(
                    args.aggregate_ledger, approval, run_id, slots=required
                )
                if memory
                else reserve_aggregate(args.aggregate_ledger, approval, run_id)
            )
        else:
            current_usage = _usage(env)
            if (
                current_usage["attempts"]
                or current_usage["reservations"]
                or _allowance(env)["consumed"]
            ):
                raise ValueError("zero-call continuation provider state changed")
            report["continuation_receipt"] = _claim_zero_call_continuation(
                args.aggregate_ledger,
                approval,
                continuation_of,
                run_id,
                commit,
                activation_bytes,
            )
            report["aggregate_remaining"] = 0
            _write_report(args.report, report)
        _owner_change(env, owner_password, False, running=True)
        _stop_file(stop, False)
        _progress(args.report, report, "inference")
        participant.request(
            "POST", "/api/privacy/notice", {"version": "privacy-notice-ru-v1"}
        )

        def send(message_id, content):
            validate_current()
            if continuation_of is not None and datetime.now(UTC) >= _utc(
                activation["expires_at"]
            ):
                raise ValueError("zero-call continuation activation window expired")
            _reconcile(env, baseline, expected, approval, required=required)
            source_id = run_id + "-" + message_id
            response = participant.request(
                "POST",
                "/api/messages",
                {"message_id": source_id, "content": content},
            )
            reply_id, correlation = _source(env, source_id)
            if memory:
                suffix = reply_id.removeprefix("reply:")
                expected.extend(
                    (kind + ":" + suffix, correlation)
                    for kind in ("evolution", "typed")
                )
            expected.append((reply_id, correlation))
            _reconcile(
                env,
                baseline,
                expected,
                approval,
                final=len(expected) == required,
                required=required,
            )
            return response

        def history():
            return participant.request("GET", "/api/history")["messages"]

        def restart_app():
            _progress(args.report, report, "restart")
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
            _progress(args.report, report, "inference")

        if memory:

            def inspect_request():
                capture = worker_dir / "robot-projections.jsonl"
                _protected(capture, 0o600, WORKER_UID)
                lines = capture.read_text().splitlines()
                if not lines:
                    raise ValueError("actual provider projection is unavailable")
                return json.loads(json.loads(lines[-1])["input"])

            def delete_revision():
                source_id = run_id + "-robot-revision"
                code = (
                    "import json,sqlite3,sys; "
                    "d=sqlite3.connect('file:/var/lib/pilot-app/events.db?mode=ro',uri=True); "
                    "r=d.execute('SELECT event_id FROM event_envelopes WHERE source_message_id=?',"
                    "(sys.stdin.readline().rstrip('\\n'),)).fetchall(); print(json.dumps(r))"
                )
                rows = json.loads(
                    _app_python(env, code, source_id + "\n", running=True)
                )
                if len(rows) != 1:
                    raise ValueError("revision source is unresolved")
                return participant.request(
                    "POST",
                    "/api/privacy/deletions",
                    {
                        "operation_id": run_id + "-robot-delete",
                        "source": rows[0][0],
                        "confirm": "delete",
                    },
                )

            journey = run_robot_journey(
                send,
                restart_app,
                inspect_request,
                delete_revision,
                record_step=report["steps"].append,
            )
        else:
            journey = run_ship_journey(
                send,
                history,
                restart_app,
                record_step=report["steps"].append,
                record_name=lambda name: report.update(ship_name=name),
            )
        report.update(journey)
        report["journey_complete"] = True
    except (Exception, KeyboardInterrupt) as error:
        report["error"] = type(error).__name__
        report["status"] = (
            "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        )
        if isinstance(error, BackendReadinessError):
            report["backend_error"] = str(error)
        if isinstance(error, KeyboardInterrupt):
            _write_report(args.report, report)
        raise
    finally:
        cleanup_interrupted = [False]
        try:
            with _ignore_sigint(lambda: cleanup_interrupted.__setitem__(0, True)):
                try:
                    _progress(args.report, report, "cleanup")
                except Exception:
                    report["cleanup_progress_error"] = True
                try:
                    _stop_file(stop, True)
                    report["worker_stop"] = json.loads(stop.read_text())["stop"] is True
                except Exception:
                    report["worker_stop"] = False
                if owner_password is not None:
                    try:
                        _owner_change(
                            env, owner_password, True, running=app_started, timeout=15
                        )
                    except Exception:
                        report["owner_stop_error"] = True
                try:
                    _compose(env, "stop", "private-app", "provider-worker", timeout=15)
                    report["running_services"] = _compose(
                        env, "ps", "--status", "running", "--services", timeout=15
                    ).splitlines()
                except Exception:
                    report["container_stop_error"] = True
                try:
                    usage = _usage(env, timeout=15)
                    rows = usage["attempts"]
                    current = (
                        [row for row in rows if row[0] not in baseline["attempts"]]
                        if "baseline" in locals()
                        else rows
                    )
                    report["provider_attempts"] = len(current)
                    report["rejection_reasons"] = sorted(
                        {row[3] for row in current if row[3]}
                    )
                    final_allowance = _allowance(env, timeout=15)
                    report["remaining_allowance"] = final_allowance["maximum"] - len(
                        final_allowance["consumed"]
                    )
                    report["usage_unavailable"] = any(
                        row[4] is None or row[5] is None or row[6] is None
                        for row in rows
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
                    report["cost_estimate_statuses"] = sorted(
                        {row[9] for row in current}
                    )
                    if "baseline" in locals():
                        _reconcile(
                            env,
                            baseline,
                            expected,
                            approval,
                            final=report.get("journey_complete", False),
                            timeout=15,
                            required=required,
                        )
                    else:
                        report["accounting_error"] = True
                except Exception:
                    report["accounting_error"] = True
                try:
                    report["owner_stop"] = (
                        _app_python(
                            env,
                            "from app.owner_control.store import OwnerStore; print(OwnerStore('/var/lib/pilot-app/owner').active())",
                            timeout=15,
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
                    or report.get("owner_stop_error")
                    or report.get("accounting_error")
                    or report.get("token_limit_exceeded")
                    or report.get("cleanup_progress_error")
                ):
                    if report["status"] != "interrupted":
                        report["status"] = "failed"
                elif (
                    report.get("journey_complete") and report["status"] != "interrupted"
                ):
                    report["status"] = "passed"
                elif report["status"] == "in_progress":
                    report["status"] = "failed"
                if cleanup_interrupted[0]:
                    report.update(status="interrupted", error="KeyboardInterrupt")
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                _write_report(args.report, report)
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
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
    parser.add_argument("--continue-zero-call", metavar="INTERRUPTED_RUN_ID")
    parser.add_argument("--previous-report", type=Path)
    parser.add_argument("--reviewed-commit", metavar="FULL_CURRENT_SHA")
    parser.add_argument("--memory-journey", action="store_true")
    args = parser.parse_args(argv)
    try:
        run(args)
    except (BackendConfigurationError, BackendReadinessError) as error:
        print(f"Acceptance failed: {error}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError, KeyboardInterrupt) as error:
        report_hint = (
            "inspect protected report"
            if args.report.exists()
            else "no report was written during preflight"
        )
        print(
            f"Acceptance failed: {type(error).__name__}; {report_hint}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
