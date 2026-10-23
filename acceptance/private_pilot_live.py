"""Explicitly authorized, synthetic private-pilot acceptance on an approved host."""

import argparse
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from getpass import getpass
from http.cookiejar import CookieJar
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


COMPOSE = ("docker", "compose", "-f", "ops/compose.private-pilot.yaml")
TASK = "synthetic-private-pilot-ship-restart-mars"
MODEL = "gpt-6-luna"
APP_UID = 11022
WORKER_UID = 11021


def _utc(value):
    result = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return result


def _protected(path, mode, uid):
    entry = path.lstat()
    if (
        not stat.S_ISREG(entry.st_mode)
        or stat.S_IMODE(entry.st_mode) != mode
        or entry.st_uid != uid
        or entry.st_nlink != 1
    ):
        raise ValueError(f"unsafe protected file: {path}")


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


def _command(args, env, *, input_text=None):
    result = subprocess.run(
        args,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args[:5])}"
        )
    return result.stdout.strip()


def _compose(env, *args, input_text=None):
    return _command((*COMPOSE, *args), env, input_text=input_text)


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
    path.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": active})
    )
    os.chown(path, WORKER_UID, WORKER_UID)
    path.chmod(0o400)


class HTTPSParticipant:
    def __init__(self, origin):
        self.origin = origin
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
            {"username": "synthetic-a", "password": password, "csrf": csrf}
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
        "r=d.execute('SELECT outcome,rejection_reason,input_tokens,output_tokens,total_tokens,retry_count,estimated_cost,cost_estimate_status FROM provider_usage_attempts ORDER BY started_at,attempt_id').fetchall(); "
        "print(json.dumps(r))"
    )
    return json.loads(_worker_python(env, code))


def _remaining(env):
    code = (
        "import sqlite3; "
        "d=sqlite3.connect('file:/var/lib/pilot-worker/allowance.sqlite3?mode=ro',uri=True); "
        "print(d.execute('SELECT max_operations FROM authority').fetchone()[0]-"
        "d.execute('SELECT count(*) FROM consumed').fetchone()[0])"
    )
    return int(_worker_python(env, code))


def run(args):
    repo = Path(__file__).resolve().parents[1]
    if os.geteuid() != 0 or Path.cwd().resolve() != repo:
        raise ValueError("run as root from the reviewed repository checkout")
    commit = _command(("git", "rev-parse", "HEAD"), os.environ.copy())
    if _command(("git", "status", "--porcelain"), os.environ.copy()):
        raise ValueError("checkout must be clean")
    _protected(args.authorization, 0o400, 0)
    _protected(args.activation, 0o400, 0)
    approval = json.loads(args.authorization.read_text())
    activation = json.loads(args.activation.read_text())
    validate_authorization(approval, activation, commit, datetime.now(UTC))
    if (
        urlsplit(args.origin).scheme != "https"
        or urlsplit(args.origin).hostname is None
    ):
        raise ValueError("an approved HTTPS origin is required")
    if args.state_root.exists() and args.state_root.is_symlink():
        raise ValueError("state root cannot be a symlink")
    args.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.aggregate_ledger.exists() or args.aggregate_ledger.is_symlink():
        _protected(args.aggregate_ledger, 0o600, 0)
    run_id = uuid4().hex
    socket_dir = args.state_root / "runs" / run_id / "ipc"
    worker_dir = args.state_root / "worker"
    control_dir = args.state_root / "control"
    secret_dir = args.state_root / "secrets"
    app_dir = args.state_root / "runs" / run_id / "app"
    _protected(secret_dir / "openai-api-key", 0o400, WORKER_UID)
    for path, uid, gid, mode in (
        (socket_dir, WORKER_UID, 11020, 0o710),
        (worker_dir, WORKER_UID, WORKER_UID, 0o700),
        (control_dir, WORKER_UID, WORKER_UID, 0o700),
        (app_dir, APP_UID, APP_UID, 0o700),
    ):
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        os.chown(path, uid, gid)
        path.chmod(mode)
    activation_target = control_dir / "activation.json"
    if activation_target.exists():
        _protected(activation_target, 0o400, WORKER_UID)
        if activation_target.read_bytes() != args.activation.read_bytes():
            raise ValueError("existing allowance is bound to another activation")
    else:
        activation_target.write_bytes(args.activation.read_bytes())
        os.chown(activation_target, WORKER_UID, WORKER_UID)
        activation_target.chmod(0o400)
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
        _compose(env, "build")
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
            "import sys; from app.private_web.auth import AuthStore; AuthStore('/var/lib/pilot-app/auth.db').provision_account('synthetic-a',sys.stdin.readline().rstrip('\\n'),'synthetic-participant-a')",
            participant_password + "\n",
        )
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
        if _remaining(env) < 3:
            raise ValueError("worker allowance has fewer than three operations")
        usage_before = _usage(env)
        _owner_change(env, owner_password, False)
        _stop_file(stop, False)
        _compose(env, "up", "-d", "provider-worker", "private-app")
        participant = HTTPSParticipant(args.origin)
        _wait_http_ready(participant)
        participant.login(participant_password)
        participant.request(
            "POST", "/api/privacy/notice", {"version": "privacy-notice-ru-v1"}
        )

        def send(message_id, content):
            return participant.request(
                "POST",
                "/api/messages",
                {"message_id": run_id + "-" + message_id, "content": content},
            )

        def history():
            return participant.request("GET", "/api/history")["messages"]

        def restart_app():
            _compose(env, "restart", "private-app")
            _wait_http_ready(participant)

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
            rows = _usage(env)
            current = rows[len(usage_before) :] if "usage_before" in locals() else rows
            report["provider_attempts"] = len(current)
            report["input_tokens"] = sum(row[2] or 0 for row in current)
            report["output_tokens"] = sum(row[3] or 0 for row in current)
            report["total_tokens"] = sum(row[4] or 0 for row in current)
            report["aggregate_input_tokens"] = sum(row[2] or 0 for row in rows)
            report["aggregate_output_tokens"] = sum(row[3] or 0 for row in rows)
            report["rejection_reasons"] = sorted({row[1] for row in current if row[1]})
            report["estimated_cost_total"] = str(
                sum(
                    (Decimal(row[6]) for row in current if row[6] is not None),
                    Decimal(0),
                )
            )
            report["cost_estimate_statuses"] = sorted({row[7] for row in current})
            report["remaining_allowance"] = _remaining(env)
            if (
                sum(row[2] or 0 for row in rows)
                > approval["max_aggregate_input_tokens"]
                or sum(row[3] or 0 for row in rows)
                > approval["max_aggregate_output_tokens"]
            ):
                report["token_limit_exceeded"] = True
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
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        args.report.chmod(0o600)
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
        "report",
    ):
        parser.add_argument(
            "--" + flag, required=True, type=str if flag == "origin" else Path
        )
    args = parser.parse_args(argv)
    try:
        run(args)
    except (ValueError, RuntimeError, OSError, KeyboardInterrupt) as error:
        print(
            f"Acceptance failed: {type(error).__name__}; inspect protected report",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
