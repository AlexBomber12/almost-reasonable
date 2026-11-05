"""Authenticated request timings contain fixed stage names and durations only."""

import time
from threading import Thread

import pytest

from acceptance.guards import install
from test_agent_intellectual_state import private_setup
from test_private_web_http import Browser


@pytest.mark.parametrize("model_error", [False, True])
def test_request_timings_separate_model_context_and_history_without_payload(
    tmp_path, monkeypatch, model_error
):
    install(monkeypatch, local_ipc=True)
    app, a, _b, _events, _ledger, _gate, sdk, server = private_setup(tmp_path)

    def respond():
        time.sleep(0.015)
        if model_error:
            raise RuntimeError("PRIVATE_TIMING_ERROR_CANARY")

    sdk.before_result = respond
    thread = Thread(target=server.serve_once)
    try:
        thread.start()
        status, headers, body = a.submit("timed-input", "PRIVATE_TIMING_INPUT_CANARY")
        assert status == "201 Created"
        header = dict(headers)["Server-Timing"]
        values = {
            name: float(duration)
            for name, duration in (
                part.strip().split(";dur=") for part in header.split(",")
            )
        }
        assert set(values) == {"app", "context", "model_stage"}
        assert values["model_stage"] >= 10
        assert values["context"] >= 0
        assert values["app"] >= values["model_stage"]
        assert "CANARY" not in repr(headers)
        assert b"timed-input" not in header.encode()
        assert body
        history_status, history_headers, _ = a.request("GET", "/api/history")
        assert history_status == "200 OK"
        assert dict(history_headers)["Server-Timing"].startswith("app;dur=")
        assert "," not in dict(history_headers)["Server-Timing"]
        if not model_error:
            replay_status, replay_headers, _ = a.submit(
                "timed-input", "PRIVATE_TIMING_INPUT_CANARY"
            )
            assert replay_status == "200 OK"
            assert "model_stage" not in dict(replay_headers)["Server-Timing"]
            assert len(sdk.calls) == 1
        for client, path, expected in (
            (Browser(app), "/api/history", "401 Unauthorized"),
            (a, "/chat", "200 OK"),
            (a, "/assets/chat.js", "200 OK"),
        ):
            result, hidden_headers, _ = client.request("GET", path)
            assert result == expected
            assert "Server-Timing" not in dict(hidden_headers)
    finally:
        thread.join(timeout=5)
        server.close()
    assert not thread.is_alive()


def test_timing_contexts_are_isolated_across_concurrent_requests():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.private_web.timing import timed_request, timed_stage

    barrier = Barrier(2)

    class Application:
        @timed_request
        def __call__(self, environ, respond):
            @timed_stage(environ["stage"])
            def work(*, value):
                barrier.wait(timeout=5)
                return value

            assert work(value="synthetic result") == "synthetic result"
            respond("200 OK", [])
            return [b"ok"]

    def request(stage):
        captured = []
        assert Application()(
            {"PATH_INFO": "/api/messages", "stage": stage},
            lambda status, headers: captured.extend(headers),
        ) == [b"ok"]
        return dict(captured)["Server-Timing"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        context, model = pool.map(request, ("context", "model_stage"))
    assert "context;dur=" in context and "model_stage" not in context
    assert "model_stage;dur=" in model and "context" not in model


def test_timing_preserves_errors_and_does_not_record_outside_requests(monkeypatch):
    from app.private_web import timing

    class Failure(Exception):
        pass

    @timing.timed_stage("model_stage")
    def fail():
        raise Failure("synthetic")

    class Application:
        @timing.timed_request
        def __call__(self, environ, respond):
            fail()

    with pytest.raises(Failure, match="synthetic"):
        Application()({}, lambda *args: None)

    def unexpected_clock():
        raise AssertionError("out-of-request timing leaked")

    monkeypatch.setattr(timing, "perf_counter", unexpected_clock)
    with pytest.raises(Failure, match="synthetic"):
        fail()
    with pytest.raises(ValueError, match="unknown private timing stage"):
        timing.timed_stage("untrusted-label")
