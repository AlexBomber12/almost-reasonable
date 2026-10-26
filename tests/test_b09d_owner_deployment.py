"""Exercise the shipped Compose commands with disposable state and kernel IPC.

Docker validation builds Dockerfile.private-pilot-app when a daemon is available;
B09D_TEST_IMAGE can select an already built image. No production env file, SDK,
provider network or credentials is used.
"""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "ops/compose.owner-control.yaml"
SERVICES = ["owner-console", "worker-telemetry", "worker-stop"]


def run(*args, input=None, check=True):
    # Shell variables outrank --env-file in Compose. Never let a developer's
    # production paths or Compose options replace this test's synthetic inputs.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PILOT_", "COMPOSE_"))
    }
    result = subprocess.run(
        args,
        input=input,
        text=True,
        capture_output=True,
        timeout=120,
        cwd=ROOT,
        env=environment,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return (result.stdout + (result.stderr if not check else "")).strip()


@pytest.fixture
def deployment(tmp_path):
    if not shutil.which("docker"):
        pytest.skip("Docker CLI unavailable")
    if subprocess.run(
        ["docker", "compose", "version"], capture_output=True, timeout=10
    ).returncode:
        pytest.skip("Docker Compose unavailable")
    env = {
        "PILOT_OWNER_CONTROL_IMAGE": os.environ.get(
            "B09D_TEST_IMAGE", "almost-reasonable-private-pilot-app:b09d-test"
        ),
        "PILOT_APP_STATE_DIR": str(tmp_path / "app"),
        "PILOT_WORKER_STATE_DIR": str(tmp_path / "worker"),
        "PILOT_CONTROL_DIR": str(tmp_path / "control"),
        "PILOT_TELEMETRY_SOCKET_DIR": str(tmp_path / "telemetry"),
        "PILOT_STOP_SOCKET_DIR": str(tmp_path / "stop-ipc"),
        "PILOT_OWNER_ALLOWED_HOST": "owner.test",
        "PILOT_OWNER_ALLOWED_ORIGIN": "https://owner.test",
        "PILOT_OWNER_PARTICIPANT_ORIGIN": "https://chat.test",
        "PILOT_OWNER_ALLOWED_CLIENT": "127.0.0.1",
    }
    env_file = tmp_path / "deployment.env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()))
    command = [
        "docker",
        "compose",
        "--project-name",
        "b09d-test-" + uuid.uuid4().hex[:12],
        "--env-file",
        str(env_file),
        "-f",
        str(COMPOSE),
    ]
    return tmp_path, env, command


def test_compose_resolves_only_control_services_with_narrow_mounts(
    deployment, monkeypatch
):
    _, env, command = deployment
    monkeypatch.setenv("PILOT_APP_STATE_DIR", "/must-not-use-shell-state")
    monkeypatch.setenv("PILOT_OWNER_CONTROL_IMAGE", "must-not-use:shell-image")
    config = json.loads(run(*command, "config", "--format", "json"))
    services = config["services"]
    assert set(services) == set(SERVICES)
    assert not config.get("networks")
    expected_mounts = {
        "owner-console": {
            "/var/lib/pilot-app": ("PILOT_APP_STATE_DIR", False),
            "/run/pilot-worker-status": ("PILOT_TELEMETRY_SOCKET_DIR", True),
            "/run/pilot-worker-stop": ("PILOT_STOP_SOCKET_DIR", True),
        },
        "worker-telemetry": {
            "/var/lib/pilot-worker": ("PILOT_WORKER_STATE_DIR", True),
            "/run/almost-reasonable-private-pilot/control": ("PILOT_CONTROL_DIR", True),
            "/run/pilot-worker-status": ("PILOT_TELEMETRY_SOCKET_DIR", False),
        },
        "worker-stop": {
            "/run/almost-reasonable-private-pilot/control": (
                "PILOT_CONTROL_DIR",
                False,
            ),
            "/run/pilot-worker-stop": ("PILOT_STOP_SOCKET_DIR", False),
        },
    }
    for name, service in services.items():
        assert service["image"] == env["PILOT_OWNER_CONTROL_IMAGE"]
        assert "build" not in service and not service.get("depends_on")
        assert not service.get("ports") and not service.get("privileged")
        assert service["read_only"]
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["pids_limit"] == 64
        assert int(service["mem_limit"]) == 256 * 1024**2
        assert service["group_add"] == ["11020"]
        assert service["user"] == (
            "11022:11022" if name == "owner-console" else "11021:11021"
        )
        assert service["network_mode"] == (
            "host" if name == "owner-console" else "none"
        )
        assert len(service["volumes"]) == len(expected_mounts[name])
        for mount in service["volumes"]:
            key, read_only = expected_mounts[name][mount["target"]]
            assert mount["source"] == env[key]
            assert mount.get("read_only", False) == read_only
            assert mount["type"] == "bind"
            assert mount.get("bind", {}).get("create_host_path", False) is False
    owner = services["owner-console"]["command"]
    for option, value in {
        "--listen-host": "127.0.0.1",
        "--port": "18026",
        "--worker-telemetry-socket": "/run/pilot-worker-status/status.sock",
        "--worker-telemetry-uid": "11021",
        "--worker-stop-socket": "/run/pilot-worker-stop/stop.sock",
        "--worker-stop-uid": "11021",
    }.items():
        assert owner[owner.index(option) + 1] == value


# Keep a WAL writer open: the telemetry must see committed usage beyond the main
# database inode through its read-only directory mount. This process never runs
# the provider worker and has no provider configuration or network.
SEED = r"""
from datetime import UTC, datetime
from pathlib import Path
import json, os, sqlite3, time
from app.core.store import EventStore
from app.core.provider_spend import ProviderAttemptRecord, ProviderStateStore
from app.core.provider_contract import CostEstimateStatus
from app.owner_control.store import OwnerStore
from app.privacy.ledger import PrivacyLedger
p = Path('/seed')
os.chown(p, 0, 0)
p.chmod(0o755)
for name in ('app', 'worker', 'control', 'telemetry', 'stop-ipc'):
    (p / name).mkdir(mode=0o700)
a = p / 'app'
owner = OwnerStore(a / 'owner')
owner.provision('operator', 'synthetic-owner-password')
session = owner.login('operator', 'synthetic-owner-password', '127.0.0.1')
owner.change(session.session_token, owner.issue(session.session_token), False)
EventStore(a / 'events.db')
EventStore(a / 'ipc-state.db')
PrivacyLedger(a / 'ledger.db')
provider = EventStore(p / 'worker/provider.sqlite3')
connection = sqlite3.connect(provider.database_path)
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('PRAGMA wal_autocheckpoint=0')
state = ProviderStateStore(provider)
state.record_attempt(ProviderAttemptRecord(
    attempt_id='synthetic:1', correlation_id='synthetic-correlation',
    provider='openai', model_identifier='synthetic-model',
    provider_config_version='synthetic-config', prompt_version='synthetic-prompt',
    core_self_version='synthetic-self', provider_response_id=None,
    started_at=datetime(2026, 10, 19, tzinfo=UTC), duration_ms=1,
    input_tokens=10, cached_input_tokens=2, output_tokens=5, total_tokens=15,
    outcome='completed', retry_count=0, estimated_cost=None,
    pricing_version=None, currency=None,
    cost_estimate_status=CostEstimateStatus.PRICING_UNAVAILABLE,
    provider_error_code=None))
state.reserve_attempt('pending:1', 'pending-correlation', datetime(2026,10,19,tzinfo=UTC))
assert Path(str(provider.database_path) + '-wal').stat().st_size > 0
with sqlite3.connect(provider.database_path.as_uri() + '?immutable=1', uri=True) as main_only:
    assert main_only.execute('SELECT COUNT(*) FROM provider_usage_attempts').fetchone() == (0,)
stop = p / 'control/emergency-stop.json'
stop.write_text('{"version":"private-pilot-emergency-stop-v1","stop":false}')
stop.chmod(0o400)
# Sentinels prove startup/restart cannot reset or renew unrelated authority.
(p / 'worker/allowance-sentinel').write_bytes(b'expired-exhausted-synthetic')
(p / 'control/activation-sentinel').write_bytes(b'expired-synthetic')
for directory in ('app', 'worker', 'control', 'telemetry', 'stop-ipc'):
    uid = 11022 if directory == 'app' else 11021
    for entry in sorted((p / directory).rglob('*'), reverse=True):
        if entry.is_file() and entry != stop:
            entry.chmod(0o600)
        os.chown(entry, uid, uid)
    (p / directory).chmod(0o710 if directory in ('telemetry','stop-ipc') else 0o700)
    os.chown(p / directory, uid, 11020 if directory in ('telemetry','stop-ipc') else uid)
(p / 'ready').touch()
while True:
    time.sleep(1)
"""


def eventually(probe):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            value = probe()
            if value:
                return value
        except (OSError, AssertionError, ValueError):
            pass
        time.sleep(0.1)
    pytest.fail("disposable deployment did not become ready")


@pytest.fixture
def container_image(deployment):
    if subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode:
        pytest.skip("Docker daemon unavailable; real container validation not run")
    root, env, _ = deployment
    supplied = os.environ.get("B09D_TEST_IMAGE")
    image = supplied or "almost-reasonable-b09d-test:" + uuid.uuid4().hex
    if not supplied:
        run(
            "docker",
            "build",
            "-f",
            "ops/Dockerfile.private-pilot-app",
            "-t",
            image,
            ".",
        )
    env["PILOT_OWNER_CONTROL_IMAGE"] = image
    (root / "deployment.env").write_text(
        "".join(f"{key}={value}\n" for key, value in env.items())
    )
    try:
        yield image
    finally:
        if not supplied:
            run("docker", "image", "rm", image, check=False)


def test_real_compose_owner_telemetry_stop_and_restart(deployment, container_image):
    root, env, command = deployment
    image = container_image
    # Use a disposable port while retaining the shipped loopback-only bind.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    override = root / "test-port.json"
    config = json.loads(run(*command, "config", "--format", "json"))
    owner_command = config["services"]["owner-console"]["command"]
    owner_command[owner_command.index("--port") + 1] = str(port)
    override.write_text(
        json.dumps({"services": {"owner-console": {"command": owner_command}}})
    )
    command += ["-f", str(override)]
    seed = run(
        "docker",
        "run",
        "-d",
        "--network",
        "none",
        "--user",
        "0:0",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "DAC_OVERRIDE",
        "-v",
        f"{root}:/seed",
        "--entrypoint",
        "python",
        image,
        "-B",
        "-c",
        SEED,
    )

    def exec_python(service, code, *, user=None):
        options = ["--user", user] if user else []
        return run(
            *command, "exec", "-T", *options, service, "python", "-B", "-c", code
        )

    def request(path, payload=None, *, cookie="", csrf="", method="POST", **headers):
        request_headers = {
            "Host": "owner.test",
            "Origin": "https://owner.test",
            "Content-Type": "application/json",
            "Cookie": cookie,
            "X-CSRF-Token": csrf,
            "X-Forwarded-For": "127.0.0.1",
            **headers,
        }
        # Docker Desktop may put the daemon host in a VM. Probe its actual host
        # loopback from the owner's network namespace instead of assuming that
        # the development Python interpreter shares the daemon's network.
        code = f"""
import http.client, json
c = http.client.HTTPConnection('127.0.0.1', {port}, timeout=3)
c.request({method!r}, {path!r}, {json.dumps(payload or {})!r}, headers={request_headers!r})
r = c.getresponse()
print(json.dumps([r.status, json.loads(r.read()), r.getheaders()]))
c.close()
"""
        return tuple(json.loads(exec_python("owner-console", code)))

    def snapshot():
        return run(
            "docker",
            "exec",
            seed,
            "python",
            "-B",
            "-c",
            r"""
from pathlib import Path
from hashlib import sha256
p = Path('/seed')
print([(str(f.relative_to(p)), sha256(f.read_bytes()).hexdigest())
       for d in ('worker', 'control') for f in sorted((p / d).iterdir())
       if f.name != 'emergency-stop.json' and not f.name.endswith('-shm')])
""",
        )

    try:
        eventually(lambda: (root / "ready").exists())
        before = snapshot()
        run(
            *command,
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            *SERVICES,
        )
        eventually(lambda: request("/login", method="GET")[0] == 200)
        status, challenge, headers = request("/login", method="GET")
        assert status == 200
        cookie = next(v.split(";")[0] for k, v in headers if k == "Set-Cookie")
        status, session, headers = request(
            "/login",
            {
                "username": "operator",
                "password": "synthetic-owner-password",
                "csrf": challenge["csrf"],
            },
            cookie=cookie,
        )
        assert status == 200
        cookie = next(
            v.split(";")[0]
            for k, v in headers
            if k == "Set-Cookie" and v.startswith("ar_owner_session=")
        )
        csrf = session["csrf"]

        def observed():
            code, data, _ = request("/status", cookie=cookie)
            assert code == 200
            return data

        data = observed()
        assert data["application_stop"]["active"] is False
        telemetry = data["worker"]
        assert telemetry["provider"]["attempt_count"] == 1
        assert telemetry["provider"]["recorded_tokens"]["total_tokens"] == "15"
        assert telemetry["provider"]["unresolved_reservations"] == 1
        assert telemetry["stop"] == {"valid": True, "active": False}
        assert snapshot() == before
        assert set(
            run(*command, "ps", "--services", "--status", "running").splitlines()
        ) == set(SERVICES)
        # Kernel credentials, not just the socket filesystem mode, reject a peer
        # which can connect via IPC group but is not the allowed owner UID.
        for service, path, protocol in (
            (
                "worker-telemetry",
                "/run/pilot-worker-status/status.sock",
                "app.worker_telemetry.protocol",
            ),
            (
                "worker-stop",
                "/run/pilot-worker-stop/stop.sock",
                "app.worker_stop_protocol",
            ),
        ):
            code = f"""
import socket
from {protocol} import REQUEST
with socket.socket(socket.AF_UNIX) as s:
    s.settimeout(2)
    s.connect({path!r})
    try:
        s.sendall(REQUEST)
        s.shutdown(socket.SHUT_WR)
        assert not s.recv(4096)
    except ConnectionResetError:
        pass
"""
            exec_python(service, code, user="11023:11020")
        assert observed()["worker"]["stop"]["active"] is False
        for service, directory, name in (
            ("worker-telemetry", "/run/pilot-worker-status", "status.sock"),
            ("worker-stop", "/run/pilot-worker-stop", "stop.sock"),
        ):
            exec_python(
                service,
                f"""
import os, stat
from pathlib import Path
d = Path({directory!r}).stat()
s = (Path({directory!r}) / {name!r}).stat()
assert (d.st_uid, d.st_gid, stat.S_IMODE(d.st_mode)) == (11021, 11020, 0o710)
assert stat.S_ISSOCK(s.st_mode)
assert (s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)) == (11021, 11020, 0o660)
assert os.statvfs('/').f_flag & os.ST_RDONLY
""",
            )
        # HTTP checks still protect the emergency operation.
        assert request("/worker-stop-operation", cookie=cookie)[0] == 403
        assert (
            request(
                "/worker-stop-operation",
                cookie=cookie,
                csrf=csrf,
                Origin="https://chat.test",
            )[0]
            == 403
        )

        def emergency():
            code, operation, _ = request(
                "/worker-stop-operation", cookie=cookie, csrf=csrf
            )
            assert code == 200
            return request(
                "/emergency-stop",
                {"operation_id": operation["operation_id"]},
                cookie=cookie,
                csrf=csrf,
            )

        assert emergency()[:2] == (
            200,
            {"application": "confirmed", "worker": "confirmed"},
        )
        assert observed()["worker"]["stop"] == {"valid": True, "active": True}
        assert observed()["application_stop"]["active"] is True
        assert snapshot() == before
        run(*command, "restart", *SERVICES)
        eventually(lambda: request("/login", method="GET")[0] == 200)
        assert observed()["worker"]["stop"]["active"] is True
        assert observed()["application_stop"]["active"] is True
        assert snapshot() == before
        run(*command, "stop", "worker-telemetry")
        assert observed()["worker"]["available"] is False
        assert emergency()[:2] == (
            200,
            {"application": "confirmed", "worker": "confirmed"},
        )
        run(
            *command,
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "worker-telemetry",
        )
        eventually(lambda: observed()["worker"]["available"])
        run(*command, "stop", "worker-stop")
        assert observed()["worker"]["available"] is True
        assert emergency()[:2] == (
            200,
            {"application": "confirmed", "worker": "unavailable"},
        )
        run(
            *command,
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "worker-stop",
        )
        eventually(lambda: observed()["worker"]["available"])
        assert observed()["worker"]["stop"]["active"] is True
        assert snapshot() == before
        for service in ("worker-stop", "worker-telemetry"):
            exec_python(
                service,
                "import os; assert os.listdir('/sys/class/net') == ['lo']; assert os.getuid() == 11021; assert 11020 in os.getgroups()",
            )
        exec_python(
            "owner-console",
            "import os; assert os.getuid() == 11022; assert 11020 in os.getgroups()",
        )
        # Real mounts enforce the owner/Stop boundary and read-only telemetry.
        exec_python(
            "owner-console",
            "from pathlib import Path; assert not Path('/var/lib/pilot-worker').exists(); assert not Path('/run/secrets').exists(); assert not Path('/run/almost-reasonable-private-pilot/control').exists()",
        )
        exec_python(
            "worker-stop",
            "from pathlib import Path; assert not Path('/var/lib/pilot-worker').exists(); assert not Path('/run/secrets').exists()",
        )
        exec_python(
            "worker-telemetry",
            "from pathlib import Path\ntry:\n Path('/var/lib/pilot-worker/forbidden').touch()\nexcept OSError:\n pass\nelse:\n raise AssertionError('telemetry state mount is writable')",
        )
    except BaseException:
        print(run("docker", "logs", seed, check=False))
        print(run(*command, "logs", check=False))
        raise
    finally:
        run(*command, "down", "--timeout", "10", check=False)
        run("docker", "rm", "-f", seed, check=False)
        # Restore synthetic files for pytest's unprivileged temp cleanup only.
        run(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "DAC_OVERRIDE",
            "-v",
            f"{root}:/seed",
            "--entrypoint",
            "python",
            image,
            "-B",
            "-c",
            f"import os; from pathlib import Path; p=Path('/seed'); "
            f"[os.chown(f, {os.getuid()}, {os.getgid()}) for f in [p, *p.rglob('*')]]",
        )
