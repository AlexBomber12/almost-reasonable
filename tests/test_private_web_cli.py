from pathlib import Path
import getpass
import runpy
import socket
import sys

import pytest

from app.private_web.auth import AuthStore
from app.private_web.cli import _parser, main
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

    def server_factory(host, port, application, **options):
        from app.private_web.server import PrivateWSGIRequestHandler

        assert options["handler_class"] is PrivateWSGIRequestHandler
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


def test_worker_history_limit_accepts_the_memory_journey_bound():
    args = _parser().parse_args(
        [
            "serve",
            "--auth-db",
            "auth.db",
            "--event-db",
            "events.db",
            "--allowed-host",
            "chat.example",
            "--allowed-origin",
            "https://chat.example",
            "--worker-history-events",
            "5",
        ]
    )
    assert args.worker_history_events == 5


@pytest.mark.parametrize(
    ("listen_host", "allowed_origin"),
    [
        ("0.0.0.0", "http://127.0.0.1:8765"),
        ("127.0.0.1", "http://server.example:8765"),
        ("127.0.0.1", "https://127.0.0.1:8765"),
        ("127.0.0.1", "not-an-origin"),
        ("127.0.0.1", "http://[invalid"),
    ],
)
def test_insecure_http_rejects_non_loopback_bind_or_browser_origin(
    tmp_path, listen_host, allowed_origin
):
    database = tmp_path / "must-not-be-created.sqlite3"

    def server_factory(*_args):
        raise AssertionError("invalid insecure configuration reached the server")

    with pytest.raises(SystemExit, match="numeric loopback HTTP"):
        main(
            [
                "serve",
                "--auth-db",
                str(database),
                "--event-db",
                str(tmp_path / "events.sqlite3"),
                "--listen-host",
                listen_host,
                "--port",
                "8765",
                "--allowed-host",
                "127.0.0.1:8765",
                "--allowed-origin",
                allowed_origin,
                "--insecure-http",
            ],
            server_factory=server_factory,
        )
    assert not database.exists()


def test_secure_mode_may_bind_non_loopback(tmp_path):
    observed = {}

    class Server:
        def serve_forever(self):
            observed["served"] = True

    def server_factory(host, _port, application, **options):
        from app.private_web.server import PrivateWSGIRequestHandler

        assert options["handler_class"] is PrivateWSGIRequestHandler
        observed["host"] = host
        observed["application"] = application
        return Server()

    assert (
        main(
            [
                "serve",
                "--auth-db",
                str(tmp_path / "auth.sqlite3"),
                "--event-db",
                str(tmp_path / "events.sqlite3"),
                "--listen-host",
                "0.0.0.0",
                "--allowed-host",
                "chat.example",
                "--allowed-origin",
                "https://chat.example",
                "--trusted-proxy",
                "127.0.0.1",
            ],
            server_factory=server_factory,
        )
        == 0
    )
    assert observed["host"] == "0.0.0.0"
    assert observed["served"] is True
    assert observed["application"]._config.trusted_proxy_addresses == frozenset(
        {"127.0.0.1"}
    )


@pytest.mark.parametrize(
    "origin",
    ["http://chat.example", "http://127.0.0.1:8000", "https://[invalid"],
)
def test_secure_mode_requires_https_browser_origin(tmp_path, origin):
    with pytest.raises(SystemExit, match="HTTPS browser origin"):
        main(
            [
                "serve",
                "--auth-db",
                str(tmp_path / "auth.sqlite3"),
                "--event-db",
                str(tmp_path / "events.sqlite3"),
                "--allowed-host",
                "chat.example",
                "--allowed-origin",
                origin,
            ],
            server_factory=lambda *_args: (_ for _ in ()).throw(
                AssertionError("invalid origin reached server startup")
            ),
        )
    assert not (tmp_path / "auth.sqlite3").exists()


def test_ipv6_loopback_insecure_mode_is_accepted(tmp_path):
    observed = {}

    class Server:
        def serve_forever(self):
            observed["served"] = True

    def server_factory(host, _port, _application, **kwargs):
        observed["host"] = host
        observed.update(kwargs)
        return Server()

    assert (
        main(
            [
                "serve",
                "--auth-db",
                str(tmp_path / "auth.sqlite3"),
                "--event-db",
                str(tmp_path / "events.sqlite3"),
                "--listen-host",
                "::1",
                "--allowed-host",
                "[::1]:8000",
                "--allowed-origin",
                "http://[::1]:8000",
                "--insecure-http",
            ],
            server_factory=server_factory,
        )
        == 0
    )
    assert observed["host"] == "::1"
    assert observed["served"] is True
    assert observed["server_class"].address_family == socket.AF_INET6


def test_invalid_trusted_proxy_is_rejected_before_database_creation(tmp_path):
    database = tmp_path / "auth.sqlite3"
    with pytest.raises(ValueError, match="canonical IP"):
        main(
            [
                "serve",
                "--auth-db",
                str(database),
                "--event-db",
                str(tmp_path / "events.sqlite3"),
                "--allowed-host",
                "chat.example",
                "--allowed-origin",
                "https://chat.example",
                "--trusted-proxy",
                "proxy.example",
            ]
        )
    assert not database.exists()


def test_listener_must_be_a_numeric_address(tmp_path):
    with pytest.raises(SystemExit, match="numeric IP address"):
        main(
            [
                "serve",
                "--auth-db",
                str(tmp_path / "auth.sqlite3"),
                "--event-db",
                str(tmp_path / "events.sqlite3"),
                "--listen-host",
                "localhost",
                "--allowed-host",
                "chat.example",
                "--allowed-origin",
                "https://chat.example",
            ]
        )


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
