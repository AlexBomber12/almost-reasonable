"""Application lifecycle, current-account checks and sanitized supervisor errors."""

from threading import Event
from types import SimpleNamespace
import sqlite3

import pytest

from app.private_web.auth import AuthStore
from app.private_web.memory_maintenance import MemoryMaintenanceRunner


def test_supervisor_recovers_without_logging_private_error_details(capsys):
    recovered = Event()
    attempts = []

    def drain():
        attempts.append(True)
        if len(attempts) <= 2:
            raise RuntimeError("PRIVATE_MAINTENANCE_ERROR_CANARY")
        recovered.set()

    runner = MemoryMaintenanceRunner(
        SimpleNamespace(drain_memory_maintenance=drain), interval=0.01
    )
    runner.start()
    try:
        assert recovered.wait(3)
    finally:
        runner.close()
    assert runner.status == "stopped" and not runner._thread.is_alive()
    output = capsys.readouterr().out
    assert "delayed by an internal failure" in output
    assert "PRIVATE_MAINTENANCE_ERROR_CANARY" not in output
    assert output.count("delayed by an internal failure") == 1


@pytest.mark.parametrize("interval", [0, 61, True, "1", float("nan")])
def test_supervisor_intervals_are_bounded(interval):
    with pytest.raises(ValueError):
        MemoryMaintenanceRunner(None, interval=interval)


def test_account_availability_is_rechecked_without_requiring_an_old_session(
    tmp_path, monkeypatch
):
    auth = AuthStore(tmp_path / "auth.db")
    auth.provision_account("synthetic", "synthetic test password", "participant-a")
    assert auth.principal_enabled("participant-a")
    assert not auth.principal_enabled("participant-b")
    auth.disable_account("synthetic")
    assert not auth.principal_enabled("participant-a")
    connection = sqlite3.connect(":memory:")
    connection.close()
    monkeypatch.setattr(auth, "_connect", lambda: connection)
    with pytest.raises(RuntimeError, match="authentication store operation failed"):
        auth.principal_enabled("participant-a")


def test_cli_closes_maintenance_when_the_http_server_fails(tmp_path, monkeypatch):
    import os
    from app.private_web.cli import main
    from app.owner_control.store import OwnerStore
    from test_int19_worker_runtime import _app

    auth, events, ledger, _, _, _, _ = _app(tmp_path, tmp_path / "worker.sock")
    OwnerStore(tmp_path / "owner")
    observed = []

    class Runner:
        def __init__(self, runtime):
            assert runtime._defer_memory_maintenance
            assert runtime._memory_maintenance.principal_enabled("participant-a")

        def start(self):
            observed.append("started")

        def close(self):
            observed.append("closed")

    monkeypatch.setattr("app.private_web.cli.MemoryMaintenanceRunner", Runner)

    class Server:
        def serve_forever(self):
            raise RuntimeError("synthetic HTTP shutdown")

    args = [
        "serve",
        "--auth-db",
        str(auth.database_path),
        "--event-db",
        str(events.database_path),
        "--privacy-ledger",
        str(ledger.path),
        "--ipc-state-db",
        str(tmp_path / "ipc-state.db"),
        "--worker-socket",
        str(tmp_path / "worker.sock"),
        "--worker-uid",
        str(os.getuid()),
        "--owner-directory",
        str(tmp_path / "owner"),
        "--enable-memory-evolution",
        "--defer-memory-maintenance",
        "--allowed-host",
        "chat.test",
        "--allowed-origin",
        "https://chat.test",
    ]
    with pytest.raises(RuntimeError, match="synthetic HTTP shutdown"):
        main(args, server_factory=lambda *_args, **_kwargs: Server())
    assert observed == ["started", "closed"]


def test_close_does_not_claim_to_cancel_an_inflight_request():
    entered, release = Event(), Event()

    def drain():
        entered.set()
        assert release.wait(5)

    runner = MemoryMaintenanceRunner(
        SimpleNamespace(drain_memory_maintenance=drain), interval=0.01
    )
    runner.start()
    try:
        assert entered.wait(3)
        runner.close()
        assert runner._thread.is_alive()
    finally:
        release.set()
        runner._thread.join(3)
    assert runner.status == "stopped"
