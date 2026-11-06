"""The fake worker must stay available through slow application preparation."""

from threading import Event, current_thread

from acceptance.guards import install
from test_agent_intellectual_state import private_setup, private_submit


def test_slow_preparation_does_not_outlive_the_fixture_accept_loop(
    tmp_path, monkeypatch
):
    install(monkeypatch, local_ipc=True)
    app, a, b, _events, _ledger, _gate, sdk, server = private_setup(tmp_path)
    # Shorten only the synthetic listener's idle wait. Production IO/admission
    # deadlines are unchanged; the request intentionally arrives after this wait.
    server._listener.settimeout(0.01)
    idle = Event()
    worker_threads = []
    serve_once = server.serve_once

    def observe_accept():
        worker_threads[:] = [current_thread()]
        accepted = serve_once()
        if not accepted:
            idle.set()
        return accepted

    monkeypatch.setattr(server, "serve_once", observe_accept)

    def prepare(point):
        if point == "before_worker_admission":
            assert idle.wait(timeout=2)
            worker_threads[0].join(timeout=0.05)
            assert worker_threads[0].is_alive(), "fixture exited on an idle accept"

    app._conversations._checkpoint = prepare
    try:
        first = private_submit(
            server, a, "slow-a", "FIXTURE_A_CANARY Обсудим архитектуру."
        )
        assert first["model_state"] == "completed", first
        assert len(sdk.calls) == 1
        idle.clear()
        second = private_submit(server, b, "slow-b", "Обсудим причинные объяснения.")
        assert second["model_state"] == "completed", second
        assert len(sdk.calls) == 2
        assert "FIXTURE_A_CANARY" not in sdk.calls[-1]["input"]
    finally:
        server.close()
