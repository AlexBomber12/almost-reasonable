"""Offline command contracts for the protected synthetic inspection entry point."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/serve_b03e_synthetic.sh"
IMAGE = "sha256:" + "a" * 64
COMMIT = "b" * 40
RUN_ID = "c" * 32


def _host_runner():
    if os.geteuid() == 0:
        return []
    if (
        shutil.which("unshare")
        and subprocess.run(
            ["unshare", "-Ur", "true"], check=False, capture_output=True
        ).returncode
        == 0
    ):
        return [shutil.which("unshare"), "-Ur"]
    if (
        shutil.which("sudo")
        and subprocess.run(
            ["sudo", "-n", "true"], check=False, capture_output=True
        ).returncode
        == 0
    ):
        return [shutil.which("sudo"), "-n", "env"]
    pytest.skip("root entry point requires passwordless sudo on this host")


@pytest.mark.parametrize(
    "condition,expected_up",
    [
        ("success", True),
        ("moved_tag", True),
        ("commit_mismatch", False),
        ("missing_docker", False),
        ("image_missing", False),
        ("ps_failed", False),
        ("worker_running", False),
        ("unknown_service", False),
        ("owner_stop_off", False),
    ],
)
def test_synthetic_serve_entrypoint_fails_closed(tmp_path, condition, expected_up):
    runner = _host_runner()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("bash", "dirname", "mktemp", "rm"):
        (bin_dir / name).symlink_to(shutil.which(name))
    (bin_dir / "python3").symlink_to(Path(sys.executable).resolve())
    git = bin_dir / "git"
    git.write_text(
        "#!/bin/bash\n"
        "if [[ $* == *rev-parse* ]]; then printf '%s\\n' \"$FAKE_COMMIT\"; fi\n"
    )
    git.chmod(0o755)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_LOG"\n'
        "if [[ $1 == image && $2 == inspect ]]; then\n"
        "  [[ $FAKE_CONDITION == image_missing ]] && exit 1\n"
        "  if [[ $FAKE_CONDITION == moved_tag && ${@: -1} == almost-reasonable-private-pilot-app:local ]]; then\n"
        "    printf 'sha256:%064d\\n' 0\n"
        "    exit 0\n"
        "  fi\n"
        "  printf '%s\\n' \"${@: -1}\"\n"
        "elif [[ $1 == compose ]]; then\n"
        "  override=''\n"
        "  for ((i=1; i<=$#; i++)); do\n"
        "    if [[ ${!i} == -f ]]; then j=$((i+1)); override=${!j}; fi\n"
        "  done\n"
        "  if [[ $* == *' ps '* ]]; then\n"
        "    case $FAKE_CONDITION in\n"
        "      ps_failed) exit 42 ;;\n"
        "      worker_running) printf '%s\\n' provider-worker ;;\n"
        "      unknown_service) printf '%s\\n' unexpected ;;\n"
        "    esac\n"
        "  elif [[ $* == *' run '* ]]; then\n"
        "    [[ $FAKE_CONDITION == owner_stop_off ]] && echo False || echo True\n"
        "  elif [[ $* == *' up '* ]]; then\n"
        '    while IFS= read -r line; do printf \'override:%s\\n\' "$line" >> "$FAKE_LOG"; done < "$override"\n'
        "  fi\n"
        "fi\n"
    )
    docker.chmod(0o755)
    if condition == "missing_docker":
        docker.unlink()
    assert shutil.which("python", path=str(bin_dir)) is None
    state = tmp_path / "state"
    (state / "control").mkdir(parents=True)
    (state / "runs" / RUN_ID / "app").mkdir(parents=True)
    (state / "control" / "emergency-stop.json").write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": True})
    )
    (state / "runs" / RUN_ID / "app" / "narrative-permission.json").write_text(
        json.dumps({"version": "private-narrative-permission-v2", "pilot_id": "pilot"})
    )
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "status": "passed",
                "technical_journey": "passed",
                "worker_stop": True,
                "owner_stop": True,
                "run_id": RUN_ID,
                "compose_project": "almost-reasonable-ship-" + RUN_ID[:12],
                "synthetic_username": "synthetic-a-" + RUN_ID[:12],
                "commit": "d" * 40 if condition == "commit_mismatch" else COMMIT,
                "images": {"almost-reasonable-private-pilot-app:local": IMAGE},
            }
        )
    )
    log = tmp_path / "docker.log"
    environment = {
        "PATH": str(bin_dir),
        "FAKE_COMMIT": COMMIT,
        "FAKE_CONDITION": condition,
        "FAKE_LOG": str(log),
    }
    command = [
        *runner,
        *(
            [f"{key}={value}" for key, value in environment.items()]
            if runner and runner[-1] == "env"
            else []
        ),
        "/bin/bash",
        str(SCRIPT),
        str(report),
        str(state),
        "https://example.test",
        "172.31.0.0/24",
        "172.31.0.2",
    ]
    result = subprocess.run(
        command,
        env={**os.environ, **environment},
        capture_output=True,
        text=True,
        check=False,
    )
    calls = log.read_text() if log.exists() else ""
    assert (result.returncode == 0) is expected_up, result.stderr
    assert (" up -d " in calls) is expected_up
    if expected_up:
        assert "override:    image: " + IMAGE in calls
        assert "override:    build: !reset null" in calls
        assert "override:    pull_policy: never" in calls
        assert "--no-build --pull never private-app" in calls
        assert "image inspect --format {{.Id}} " + IMAGE in calls
        assert (
            "image inspect --format {{.Id}} almost-reasonable-private-pilot-app:local"
            not in calls
        )
        assert "Synthetic username:" in result.stdout
    else:
        assert " up -d " not in calls
    if condition in {"ps_failed", "worker_running", "unknown_service"}:
        assert " run --rm " not in calls
