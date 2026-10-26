"""Configuration preparation never touches production stores or authority."""

from copy import deepcopy
import json
import runpy
import sys

import pytest

from app.ops.private_research_config import extension_command, main


def base_config():
    return {
        "services": {
            "private-app": {
                "command": [
                    "python",
                    "-B",
                    "-m",
                    "app.private_web.cli",
                    "serve",
                    "--worker-socket",
                    "/run/pilot-ipc/worker.sock",
                    "--trusted-proxy",
                    "172.24.0.1",
                    "--enable-typed-memory",
                ],
                "user": "11022:11022",
            },
            "provider-worker": {
                "command": [
                    "python",
                    "-B",
                    "-m",
                    "app.trusted.private_pilot_worker",
                    "--provider-database",
                    "/var/lib/pilot-worker/provider.sqlite3",
                    "--allowance-database",
                    "/var/lib/pilot-worker/historical.sqlite3",
                ],
                "user": "11021:11021",
            },
        }
    }


@pytest.mark.parametrize("narrative", [False, True])
def test_command_preserves_protected_flags_and_never_replaces_worker(narrative):
    config = base_config()
    if narrative:
        config["services"]["private-app"]["command"].extend(
            [
                "--enable-memory-evolution",
                "--enable-narrative",
                "--narrative-permission",
                "/protected/narrative.json",
                "--narrative-pilot-id",
                "existing-pilot",
            ]
        )
    before = deepcopy(config)
    result = extension_command(config, "ordinary-private")
    assert config == before
    assert set(result["services"]) == {"private-app"}
    command = result["services"]["private-app"]["command"]
    original = config["services"]["private-app"]["command"]
    assert command[: len(original)] == original
    assert command[len(original) :] == [
        "--enable-public-research",
        "--public-research-socket",
        "/run/pilot-research-ipc/research.sock",
        "--public-research-worker-uid",
        "11021",
        "--public-research-profile",
        "/run/pilot-research-application/profile.json",
        "--public-research-scope",
        "ordinary-private",
    ]


@pytest.mark.parametrize(
    "change",
    [
        "shape",
        "service",
        "entrypoint",
        "string",
        "module",
        "no-worker",
        "research",
        "app-user",
        "worker-user",
        "provider",
        "scope",
    ],
)
def test_incompatible_configuration_fails_without_output(change, tmp_path, monkeypatch):
    config = base_config()
    app = config["services"]["private-app"]
    worker = config["services"]["provider-worker"]
    scope = "ordinary-private"
    if change == "shape":
        config = []
    elif change == "service":
        del config["services"]["private-app"]
    elif change == "entrypoint":
        app["entrypoint"] = ["custom"]
    elif change == "string":
        app["command"] = "python something"
    elif change == "module":
        app["command"][3] = "different.module"
    elif change == "no-worker":
        app["command"].remove("--worker-socket")
    elif change == "research":
        app["command"].append("--public-research-scope")
    elif change == "app-user":
        app["user"] = "root"
    elif change == "worker-user":
        worker["user"] = "root"
    elif change == "provider":
        worker["command"][-3] = "/other/provider.sqlite3"
    else:
        scope = "invalid scope"
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(config)))
    output = tmp_path / "new.json"
    with pytest.raises((ValueError, SystemExit)):
        main(["--scope", scope, "--output", str(output)])
    assert not output.exists()


def test_prepare_writes_private_new_file_only(tmp_path, monkeypatch):
    source = json.dumps(base_config())
    output = tmp_path / "extension.json"
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(source))
    assert main(["--scope", "ordinary-private", "--output", str(output)]) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text()) == extension_command(
        base_config(), "ordinary-private"
    )
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(source))
    with pytest.raises(FileExistsError):
        main(["--scope", "ordinary-private", "--output", str(output)])
    assert list(tmp_path.iterdir()) == [output]
    monkeypatch.setattr(sys, "argv", ["private_research_config"])
    with pytest.raises(SystemExit):
        runpy.run_module("app.ops.private_research_config", run_name="__main__")
