"""Credential-free Docker check of the fixed-IP acceptance app lifecycle."""

import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import pytest

from acceptance import private_pilot_live as live


ROOT = Path(__file__).resolve().parents[1]
APP_IMAGE = "almost-reasonable-private-pilot-app:local"


def _docker(*args, timeout=120):
    return subprocess.run(
        ("docker", *args),
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    ).stdout.strip()


def _root_state(root, code):
    return _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "0:0",
        "--mount",
        f"type=bind,source={root},target=/state",
        APP_IMAGE,
        "python",
        "-B",
        "-c",
        code,
    )


def _subnet():
    for second in (251, 252, 253):
        for third in range(240, 255):
            subnet = f"10.{second}.{third}.0/29"
            try:
                live._check_backend_conflicts(
                    live.ip_network(subnet), os.environ.copy()
                )
            except live.BackendConfigurationError:
                continue
            return subnet, f"10.{second}.{third}.3"
    pytest.fail("no unused Docker test subnet")


def test_fixed_ip_app_and_fake_worker_socket_lifecycle(tmp_path, monkeypatch):
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI unavailable")
    if subprocess.run(
        ("docker", "info"), capture_output=True, timeout=10, check=False
    ).returncode:
        pytest.skip("Docker daemon unavailable")
    monkeypatch.chdir(ROOT)
    subnet, address = _subnet()
    run_id = uuid4().hex
    state = tmp_path / ("long-acceptance-state-" * 5) / "runs" / run_id
    socket_dir = state / "ipc"
    app_dir = state / "app"
    worker_dir = state / "worker"
    control_dir = state / "control"
    secret_dir = state / "secrets"
    for path in (socket_dir, app_dir, worker_dir, control_dir, secret_dir):
        path.mkdir(parents=True)
    assert len(os.fsencode(socket_dir / "worker.sock")) > 107

    override = tmp_path / "fake-worker.yaml"
    override.write_text(
        "services:\n"
        "  provider-worker:\n"
        "    command:\n"
        "      - python\n"
        "      - -B\n"
        "      - -c\n"
        "      - |\n"
        "        import os, socket\n"
        "        p = '/run/pilot-ipc/worker.sock'\n"
        "        if os.path.exists(p): os.unlink(p)\n"
        "        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "        s.bind(p)\n"
        "        os.chown(p, -1, 11020)\n"
        "        os.chmod(p, 0o660)\n"
        "        s.listen(8)\n"
        "        while True:\n"
        "            connection, _ = s.accept()\n"
        "            connection.close()\n"
    )
    monkeypatch.setattr(live, "COMPOSE", (*live.COMPOSE, "-f", str(override)))
    env = os.environ.copy()
    env.update(
        COMPOSE_PROJECT_NAME="pr65-lifecycle-" + run_id[:12],
        PILOT_SOCKET_DIR=str(socket_dir),
        PILOT_APP_STATE_DIR=str(app_dir),
        PILOT_WORKER_STATE_DIR=str(worker_dir),
        PILOT_CONTROL_DIR=str(control_dir),
        PILOT_SECRET_DIR=str(secret_dir),
        PILOT_ALLOWED_HOST="acceptance.invalid",
        PILOT_ALLOWED_ORIGIN="https://acceptance.invalid",
        PILOT_APP_SUBNET=subnet,
        PILOT_APP_IP=address,
    )
    try:
        live._compose(env, "build", "private-app", "provider-worker", timeout=600)
        _root_state(
            state,
            "import os; "
            "os.chown('/state/ipc',11021,11020); os.chmod('/state/ipc',0o710); "
            "os.chown('/state/app',11022,11022); os.chmod('/state/app',0o700); "
            "os.chown('/state/worker',11021,11021); "
            "os.chown('/state/control',11021,11021); "
            "os.chown('/state/secrets',11021,11021)",
        )
        live._app_python(
            env,
            "from app.private_web.auth import AuthStore; "
            "from app.core.store import EventStore; "
            "from app.privacy.ledger import PrivacyLedger; "
            "from app.owner_control.store import OwnerStore; "
            "p='/var/lib/pilot-app/'; AuthStore(p+'auth.db'); "
            "EventStore(p+'events.db'); PrivacyLedger(p+'ledger.db'); "
            "EventStore(p+'ipc-state.db'); OwnerStore(p+'owner')",
        )
        password = "synthetic-test-password"
        live._app_python(
            env,
            "import sys; from app.owner_control.store import OwnerStore; "
            "OwnerStore('/var/lib/pilot-app/owner').provision('synthetic-operator',"
            "sys.stdin.readline().rstrip('\\n'))",
            password + "\n",
        )
        live._owner_change(env, password, True)
        live._compose(env, "up", "-d", "provider-worker", "private-app")
        live._wait_worker_ready(env)
        running = live._compose(
            env, "ps", "--status", "running", "--services"
        ).splitlines()
        assert len(running) == 2
        assert set(running) == {"private-app", "provider-worker"}
        live._owner_change(env, password, False, running=True)
        assert (
            live._app_python(
                env,
                "from app.owner_control.store import OwnerStore; "
                "print(OwnerStore('/var/lib/pilot-app/owner').active())",
                running=True,
            )
            == "False"
        )

        live._compose(env, "stop", "provider-worker")
        with pytest.raises(live.BackendReadinessError, match="worker socket"):
            live._wait_worker_ready(env)
        _root_state(
            state,
            "from pathlib import Path; "
            "Path('/state/control/emergency-stop.json').write_text('"
            '{"version":"private-pilot-emergency-stop-v1","stop":true}'
            "')",
        )
        live._owner_change(env, password, True, running=True)
        live._compose(env, "stop", "private-app", "provider-worker")
        assert live._compose(env, "ps", "--status", "running", "--services") == ""
        assert (
            live._app_python(
                env,
                "from app.owner_control.store import OwnerStore; "
                "print(OwnerStore('/var/lib/pilot-app/owner').active())",
            )
            == "True"
        )
        assert (
            json.loads((control_dir / "emergency-stop.json").read_text())["stop"]
            is True
        )
    finally:
        try:
            live._compose(env, "down", "--volumes", "--remove-orphans", timeout=120)
        finally:
            _root_state(
                state,
                "import os,shutil; "
                "[shutil.rmtree('/state/'+name) for name in os.listdir('/state')]",
            )
            shutil.rmtree(state.parent.parent)
