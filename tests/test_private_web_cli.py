from pathlib import Path
import getpass
import runpy
import sys

import pytest

from app.private_web.auth import AuthStore
from app.private_web.cli import main
from app.private_web.wsgi import PrivateWebApplication


def test_operator_can_provision_without_putting_password_on_command_line(
    tmp_path, capsys
):
    database = tmp_path / "auth.sqlite3"
    prompts = iter(["synthetic password one", "synthetic password one"])

    result = main(
        [
            "provision",
            "--auth-db",
            str(database),
            "--username",
            "alice",
            "--participant-id",
            "participant-alice",
        ],
        password_reader=lambda _prompt: next(prompts),
    )

    assert result == 0
    assert "alice" in capsys.readouterr().out
    assert (
        AuthStore(database)
        .login("alice", "synthetic password one", "verification-client")
        .session_token
        is not None
    )


def test_password_confirmation_must_match(tmp_path):
    prompts = iter(["synthetic password one", "synthetic password two"])
    with pytest.raises(SystemExit, match="password confirmation does not match"):
        main(
            [
                "provision",
                "--auth-db",
                str(tmp_path / "auth.sqlite3"),
                "--username",
                "alice",
                "--participant-id",
                "participant-alice",
            ],
            password_reader=lambda _prompt: next(prompts),
        )


def test_serve_builds_disabled_local_application(tmp_path):
    auth_path = tmp_path / "auth.sqlite3"
    event_path = tmp_path / "events.sqlite3"
    AuthStore(auth_path)
    observed = {}

    class Server:
        def serve_forever(self):
            observed["served"] = True

    def server_factory(host, port, application):
        observed.update(host=host, port=port, application=application)
        return Server()

    result = main(
        [
            "serve",
            "--auth-db",
            str(auth_path),
            "--event-db",
            str(event_path),
            "--listen-host",
            "127.0.0.1",
            "--port",
            "8765",
            "--allowed-host",
            "127.0.0.1:8765",
            "--allowed-origin",
            "http://127.0.0.1:8765",
            "--insecure-http",
        ],
        server_factory=server_factory,
    )

    assert result == 0
    assert observed["host"] == "127.0.0.1"
    assert observed["port"] == 8765
    assert observed["served"] is True
    assert isinstance(observed["application"], PrivateWebApplication)
    assert Path(event_path).exists()


def test_module_entrypoint_invokes_main(tmp_path, monkeypatch):
    database = tmp_path / "entrypoint.sqlite3"
    answers = iter(["synthetic password one", "synthetic password one"])
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: next(answers))
    monkeypatch.delitem(sys.modules, "app.private_web.cli")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "private-web",
            "provision",
            "--auth-db",
            str(database),
            "--username",
            "entrypoint",
            "--participant-id",
            "participant-entrypoint",
        ],
    )
    with pytest.raises(SystemExit) as result:
        runpy.run_module("app.private_web.cli", run_name="__main__")
    assert result.value.code == 0
