"""Connected bounded public windows with real journals and fake providers."""

from dataclasses import replace
from datetime import timedelta
import json
from threading import Thread

import pytest

from app.integration.public_window import PublicOperatingWindow, WindowSpec
from app.threads.domain import OutboxState
from test_int24_runtime import NOW
from test_b05b_public_runtime import _setup as mission_setup

NO_ACTION = {"decision": "no_action", "reason_category": "not_warranted"}


def setup(tmp_path, proposal):
    parts = mission_setup(tmp_path, proposal)
    parts[1].utc_now = parts[4].now
    return parts


def spec(**changes):
    return replace(
        WindowSpec(
            window_id="reviewed-window-1",
            expires_at=NOW + timedelta(minutes=1),
            max_model_calls=3,
            max_poll_pages=4,
            max_inbound_events=2,
            poll_interval_seconds=5,
        ),
        **changes,
    )


def serve(worker, count):
    def run():
        for _ in range(count):
            worker.serve_once()

    thread = Thread(target=run, daemon=True)
    thread.start()
    return thread


def finish(thread):
    thread.join(timeout=6)
    assert not thread.is_alive()


def test_one_window_wakeup_no_action_verified_inbound_and_pending_drafts(tmp_path):
    app, worker, sdk, http, clock = setup(tmp_path, NO_ACTION)
    proposals = [
        NO_ACTION,
        {"decision": "reply", "content": "Спасибо за вопрос. Я проверю основания."},
        {"decision": "propose_publication", "draft": "Новый публичный черновик."},
    ]
    create = sdk.create

    def choose(**kwargs):
        sdk.proposal = proposals[len(sdk.calls)]
        return create(**kwargs)

    sdk.create = choose
    thread = serve(worker, 3)
    try:
        result = PublicOperatingWindow(app, spec()).run(
            wait=lambda seconds: clock.advance(timedelta(seconds=seconds))
        )
        finish(thread)
        assert result["state"] == "exhausted"
        assert result["consumed"] == {
            "model_calls": 3,
            "poll_pages": 1,
            "inbound_events": 1,
        }
        assert len(sdk.calls) == 3
        assert len(http.calls) == 1
        assert all(call[0] == "GET" for call in http.calls)
        assert result["outcomes"] == {
            "no_action": 1,
            "reply": 1,
            "propose_publication": 1,
        }
        assert result["wakeup_outcomes"] == {"no_action": 1, "proposed": 1}
        assert len(result["pending_drafts"]) == 2
        for draft in result["pending_drafts"]:
            assert (
                app.draft(draft["operation_id"]).state is OutboxState.AWAITING_APPROVAL
            )
        assert result["next_due"]["wakeup"] > clock.now().isoformat()
        assert "fake-token" not in json.dumps(result)
        assert "Я владелец" not in json.dumps(result, ensure_ascii=False)
    finally:
        worker.close()


def test_restart_same_window_preserves_consumption_and_skips_downtime(tmp_path):
    app, worker, sdk, http, clock = setup(tmp_path, NO_ACTION)
    window_spec = spec(max_poll_pages=0, max_inbound_events=0)

    def interrupt(_seconds):
        raise KeyboardInterrupt

    thread = serve(worker, 1)
    try:
        first = PublicOperatingWindow(app, window_spec).run(wait=interrupt)
        finish(thread)
        assert first["state"] == "interrupted"
        assert first["consumed"]["model_calls"] == 1
        clock.advance(timedelta(seconds=30))
        thread = serve(worker, 2)
        result = PublicOperatingWindow(app, window_spec).run(
            wait=lambda seconds: clock.advance(timedelta(seconds=seconds))
        )
        finish(thread)
        assert result["consumed"]["model_calls"] == 3
        assert len(sdk.calls) == 3
        assert clock.now() > NOW + timedelta(seconds=30)
        assert http.calls == []
        again = PublicOperatingWindow(app, window_spec).run(wait=interrupt)
        assert again["consumed"] == result["consumed"]
        assert again["outcomes"] == result["outcomes"]
        with pytest.raises(ValueError, match="changed"):
            PublicOperatingWindow(app, replace(window_spec, max_model_calls=4)).run()
    finally:
        worker.close()


@pytest.mark.parametrize("denial", ["expiry", "stop", "spend"])
def test_expired_or_stopped_windows_admit_no_calls(tmp_path, monkeypatch, denial):
    app, worker, sdk, http, clock = setup(tmp_path, NO_ACTION)
    if denial == "expiry":
        clock.advance(timedelta(minutes=1))
    elif denial == "stop":
        with app.owner._connection() as db:
            db.execute("UPDATE owner_stop SET active=1")
    else:
        monkeypatch.setattr(app.provider, "is_spend_suspended", lambda: True)
    try:
        result = PublicOperatingWindow(app, spec()).run()
        assert result["state"] == (
            "expired" if denial == "expiry" else "controls_denied"
        )
        assert result["consumed"] == {
            "model_calls": 0,
            "poll_pages": 0,
            "inbound_events": 0,
        }
        assert sdk.calls == http.calls == []
    finally:
        worker.close()


@pytest.mark.parametrize("kind", ["wake", "reply"])
def test_competing_instances_do_not_duplicate_any_effect(tmp_path, kind):
    from datetime import time
    from threading import Event
    from app.integration.threads_runtime import ThreadsRuntime
    from app.behavior.worker_client import PublicBehaviorClient
    import os

    app, worker, sdk, http, clock = setup(tmp_path, NO_ACTION)
    if kind == "reply":
        from app.threads.domain import InboundKind

        app.poll(InboundKind.REPLY, correlation_id="preload")
        app.scheduler.policy = replace(
            app.scheduler.policy, active_start=time(13), active_end=time(14)
        )
    entered, release = Event(), Event()
    create = sdk.create

    def hold(**kwargs):
        entered.set()
        assert release.wait(5)
        return create(**kwargs)

    sdk.create = hold
    rival = ThreadsRuntime(
        tmp_path / "journal.sqlite",
        tmp_path / "scheduler.sqlite",
        tmp_path / "threads.sqlite",
        model=PublicBehaviorClient(tmp_path / "worker.sock", expected_uid=os.geteuid()),
        transport=app.simulator,
        policy=app.policy,
        owner=app.owner,
        provider=app.provider,
        ledger=app.ledger,
        clock=clock,
        wakeup_policy=app.scheduler.policy,
        operator=app.operator,
        mission=app.mission,
        enabled=True,
        live_enabled=True,
    )
    results = []
    window_spec = spec(max_model_calls=1, max_poll_pages=0)
    worker_thread = serve(worker, 1)
    runner = Thread(
        target=lambda: results.append(PublicOperatingWindow(app, window_spec).run())
    )
    runner.start()
    try:
        assert entered.wait(5)
        assert PublicOperatingWindow(rival, window_spec).run()["state"] == "busy"
        # A different window is fenced by the same account/journal lock too.
        assert (
            PublicOperatingWindow(
                rival, replace(window_spec, window_id="other-window")
            ).run()["state"]
            == "busy"
        )
        release.set()
        finish(runner)
        finish(worker_thread)
        assert results[0]["consumed"]["model_calls"] == 1
        assert results[0]["consumed"]["inbound_events"] == (kind == "reply")
        assert results[0]["wakeup_outcomes"] == {
            "no_action" if kind == "wake" else "outside_active_hours": 1
        }
        assert len(sdk.calls) == 1
        assert len(http.calls) == (kind == "reply")
        from app.integration.threads_stage0 import _identity

        expected = (
            app.wakeup_operation(
                _identity(app.account, "window", window_spec.window_id, "wake", 0)
            )
            if kind == "wake"
            else _identity(app.account, "reply", "reply-1", "initial")
        )
        assert PublicOperatingWindow(app, window_spec).operations() == (expected,)
        assert (
            PublicOperatingWindow(rival, window_spec).run()["consumed"]["model_calls"]
            == 1
        )
        assert len(sdk.calls) == 1
    finally:
        release.set()
        runner.join(timeout=6)
        worker.close()


@pytest.mark.parametrize(
    "point", ["after_started", "after_intent", "after_research_intent"]
)
def test_interrupted_admissions_are_not_replaced_or_resent(
    tmp_path, monkeypatch, point
):
    from test_b05b_public_runtime import _attach_fake_research

    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    research = _attach_fake_research(app, tmp_path, monkeypatch)

    def crash(where):
        if where == point:
            raise KeyboardInterrupt

    app.checkpoint = crash
    window_spec = spec(max_model_calls=1, max_poll_pages=0, max_inbound_events=0)
    thread = None if point == "after_started" else serve(worker, 1)
    try:
        first = PublicOperatingWindow(app, window_spec).run()
        if thread is not None:
            finish(thread)
        assert first["state"] == "interrupted"
        assert first["consumed"]["model_calls"] == 1
        assert len(sdk.calls) == (0 if point == "after_started" else 1)
        assert research == []
        with app.journal.transaction() as db:
            before = [
                tuple(row)
                for row in db.execute(
                    "SELECT operation,kind FROM public_window_work ORDER BY operation"
                )
            ]
        app.checkpoint = lambda _: None
        result = PublicOperatingWindow(app, window_spec).run()
        assert result["consumed"]["model_calls"] == 1
        assert research == []
        with app.journal.transaction() as db:
            assert [
                tuple(row)
                for row in db.execute(
                    "SELECT operation,kind FROM public_window_work ORDER BY operation"
                )
            ] == before
        assert http.calls == []
        if point == "after_research_intent":
            assert result["research"] == {"consumed": 1, "outcomes": {"uncertain": 1}}
    finally:
        worker.close()


def test_lost_paid_response_is_consumed_across_restart(tmp_path):
    app, worker, sdk, http, _ = setup(tmp_path, NO_ACTION)
    create = sdk.create

    def lost(**kwargs):
        create(**kwargs)
        raise TimeoutError("PRIVATE-CANARY-LOST-RESPONSE")

    sdk.create = lost
    window_spec = spec(max_model_calls=1)
    thread = serve(worker, 1)
    try:
        result = PublicOperatingWindow(app, window_spec).run()
        finish(thread)
        again = PublicOperatingWindow(app, window_spec).run()
        assert result["consumed"] == again["consumed"]
        assert result["outcomes"] == {"uncertain": 1}
        assert len(sdk.calls) == 1
        assert http.calls == []
        assert "PRIVATE-CANARY" not in json.dumps(result)
    finally:
        worker.close()


@pytest.mark.parametrize("change", ["expiry", "stop", "privacy", "mission", "clock"])
def test_idle_control_change_exits_before_any_new_calls(tmp_path, monkeypatch, change):
    app, worker, sdk, http, clock = setup(tmp_path, NO_ACTION)
    window_spec = spec(max_poll_pages=0, max_inbound_events=0)
    original_generation = app.ledger.control_generation()

    def changed(_seconds):
        if change == "expiry":
            clock.advance(timedelta(minutes=1))
        elif change == "clock":
            clock.advance(timedelta(seconds=-1))
        elif change == "stop":
            with app.owner._connection() as db:
                db.execute("UPDATE owner_stop SET active=1")
        elif change == "privacy":
            monkeypatch.setattr(
                app.ledger, "control_generation", lambda: original_generation + 1
            )
        else:
            value = json.loads(app.mission.path.read_text())
            value["version"] = "mission-v2"
            value["parent_version"] = "mission-v1"
            app.mission.path.write_text(json.dumps(value))

    thread = serve(worker, 1)
    try:
        result = PublicOperatingWindow(app, window_spec).run(wait=changed)
        finish(thread)
        assert (
            result["state"]
            == {
                "expiry": "expired",
                "clock": "clock_regressed",
                "stop": "controls_denied",
                "privacy": "controls_changed",
                "mission": "controls_changed",
            }[change]
        )
        assert result["consumed"]["model_calls"] == 1
        assert len(sdk.calls) == 1
        assert http.calls == []
        # Even a fresh runner cannot bind this same window to the changed controls.
        assert (
            PublicOperatingWindow(app, window_spec).run()["consumed"]["model_calls"]
            == 1
        )
    finally:
        worker.close()


def test_polls_are_bounded_duplicate_verified_inbound_is_processed_once(tmp_path):
    from app.threads.transport import HTTPResponse

    app, worker, sdk, http, clock = setup(tmp_path, NO_ACTION)
    request = http.request

    def pages(method, url, headers, body, timeout):
        if "/mentions?" in url:
            http.calls.append((method, url, body))
            return HTTPResponse(200, b'{"data":[],"paging":{}}')
        return request(method, url, headers, body, timeout)

    http.request = pages
    # Long cadence lets repeated polling exercise duplicate suppression while
    # each page and the total admitted inbound work remain bounded.
    app.scheduler.repository.select_cadence(timedelta(minutes=10))
    window_spec = spec(max_poll_pages=3, max_inbound_events=1, page_size=1)
    thread = serve(worker, 2)
    try:
        result = PublicOperatingWindow(app, window_spec).run(
            wait=lambda seconds: clock.advance(timedelta(seconds=seconds))
        )
        finish(thread)
        assert result["state"] == "expired"
        assert result["consumed"] == {
            "model_calls": 2,
            "poll_pages": 3,
            "inbound_events": 1,
        }
        assert len(sdk.calls) == 2
        assert len(http.calls) == 3
        assert sum("/replies?" in call[1] for call in http.calls) == 2
        assert sum("/mentions?" in call[1] for call in http.calls) == 1
        assert all("limit=1" in call[1] for call in http.calls)
        assert all(call[0] == "GET" for call in http.calls)
        assert result["poll_outcomes"] == {"completed": 3}
    finally:
        worker.close()


def test_poll_interruption_consumes_identity_and_does_not_repoll_on_resume(tmp_path):
    app, worker, sdk, http, clock = setup(tmp_path, NO_ACTION)
    app.scheduler.repository.select_cadence(timedelta(minutes=10))
    original = http.request

    def lost(method, url, headers, body, timeout):
        original(method, url, headers, body, timeout)
        raise KeyboardInterrupt

    http.request = lost
    window_spec = spec(max_poll_pages=1)
    thread = serve(worker, 1)
    try:
        first = PublicOperatingWindow(app, window_spec).run()
        finish(thread)
        assert first["state"] == "interrupted"
        assert first["consumed"]["poll_pages"] == 1
        assert first["poll_outcomes"] == {"uncertain": 1}
        with app.journal.transaction() as db:
            identity = db.execute(
                "SELECT operation FROM public_window_work WHERE kind='poll'"
            ).fetchone()[0]
        result = PublicOperatingWindow(app, window_spec).run(
            wait=lambda _: clock.advance(timedelta(minutes=1))
        )
        assert result["state"] == "expired"
        assert len(sdk.calls) == len(http.calls) == 1
        with app.journal.transaction() as db:
            assert [
                row[0]
                for row in db.execute(
                    "SELECT operation FROM public_window_work WHERE kind='poll'"
                )
            ] == [identity]
    finally:
        worker.close()


def test_controls_rechecked_between_admission_and_worker_or_poll(tmp_path):
    app, worker, sdk, http, _ = setup(tmp_path, NO_ACTION)

    def stop(point):
        if point == "after_started":
            with app.owner._connection() as db:
                db.execute("UPDATE owner_stop SET active=1")

    app.checkpoint = stop
    try:
        result = PublicOperatingWindow(app, spec()).run()
        assert result["state"] == "controls_denied"
        assert result["consumed"]["model_calls"] == 1
        assert sdk.calls == http.calls == []
    finally:
        worker.close()


def test_window_research_uses_canonical_request_and_later_evidence(tmp_path):
    import os
    from app.core.provider_spend import ProviderStateStore
    from app.core.store import EventStore
    from app.core.public_research_client import PublicResearchClient
    from app.core.public_research_profile import load_research_profile
    from app.trusted.openai_worker_server import WorkerServerConfig
    from app.trusted.public_research import PublicResearchBoundary, PublicResearchServer
    from app.trusted.public_research_transport import PublicResearchTransport
    from app.trusted.research_authority_journal import AuthorityJournal
    from test_b05b_public_runtime import _profile
    from test_int20_public_research import SDK, raw_response

    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    application_path, worker_path = _profile(tmp_path)
    ipc = tmp_path / "ipc"
    ipc.mkdir(mode=0o700)
    authority_path = ipc / "authority.sqlite"
    authority_path.touch(mode=0o600)
    search_sdk = SDK([raw_response()])
    search_store = ProviderStateStore(EventStore(tmp_path / "search-events.sqlite"))
    boundary = PublicResearchBoundary(
        store=search_store,
        transport=PublicResearchTransport(search_sdk),
        enabled=True,
        utcnow=clock.now,
    )
    boundary.install_profile(
        load_research_profile(worker_path, role="worker"),
        profile_path=worker_path,
        journal=AuthorityJournal(authority_path),
    )
    research_worker = PublicResearchServer(
        WorkerServerConfig(ipc / "research.sock", frozenset({os.geteuid()})), boundary
    )
    app.research_enabled = True
    app.research_scope = "public-test"
    app.research_profile_path = application_path
    app.research_client = PublicResearchClient(
        ipc / "research.sock", expected_worker_uid=os.geteuid()
    )
    research_worker.start()
    research_thread = Thread(target=lambda: research_worker.serve(max_connections=1))
    research_thread.start()
    create = sdk.create

    def later(**kwargs):
        if sdk.calls:
            sdk.proposal = NO_ACTION
        return create(**kwargs)

    sdk.create = later
    thread = serve(worker, 2)
    try:
        window_spec = spec(max_model_calls=2, max_poll_pages=0, max_inbound_events=0)
        result = PublicOperatingWindow(app, window_spec).run(
            wait=lambda seconds: clock.advance(timedelta(seconds=seconds))
        )
        finish(thread)
        finish(research_thread)
        assert result["consumed"]["model_calls"] == 2
        assert result["research"] == {"consumed": 1, "outcomes": {"completed": 1}}
        assert len(search_sdk.calls) == 1
        assert (
            json.loads(search_sdk.calls[0]["input"])["question"]
            == "How do deep ocean currents form?"
        )
        assert search_sdk.calls[0]["max_tool_calls"] == 1
        assert "unverified" in sdk.calls[1]["input"][1]["content"]
        assert "not_evaluated" in sdk.calls[1]["input"][1]["content"]
        assert "authorization" not in sdk.calls[1]["input"][1]["content"]
        assert (
            PublicOperatingWindow(app, window_spec).run()["research"]
            == result["research"]
        )
        assert len(search_sdk.calls) == 1
        assert http.calls == []
    finally:
        worker.close()
        research_worker.close()


def test_window_does_not_recover_an_unrelated_research_intent(tmp_path, monkeypatch):
    from test_b05b_public_runtime import _attach_fake_research
    from app.threads.domain import InboundKind

    app, worker, sdk, http, _ = setup(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    research = _attach_fake_research(app, tmp_path, monkeypatch)
    app.poll(InboundKind.REPLY, correlation_id="before-window")
    event = app.service.store.pending_inbound_events(
        app.account, InboundKind.REPLY, now=NOW
    )[0]
    from app.integration.threads_stage0 import _identity

    operation = _identity(app.account, "reply", event.event_id, "initial")
    thread = serve(worker, 1)
    try:
        app._decide(operation, app._origin(event), (event.content,))
        finish(thread)
        assert app.journal.read(operation)["phase"] == "intent"
        sdk.proposal = NO_ACTION
        thread = serve(worker, 1)
        result = PublicOperatingWindow(app, spec(max_model_calls=1)).run()
        finish(thread)
        assert result["outcomes"] == {"no_action": 1}
        assert app.journal.read(operation)["phase"] == "intent"
        assert research == []
        assert len(sdk.calls) == 2
        assert len(http.calls) == 1
    finally:
        worker.close()


def test_window_plan_is_credential_free_and_activation_requires_review(
    tmp_path, monkeypatch, capsys
):
    import signal
    from app.ops import int24_threads_runtime as module

    config = {
        "operating_window_reviewed": True,
        "public_mission_path": "/reviewed/mission.json",
        "operating_window": json.loads(spec().encoded()),
    }
    monkeypatch.setattr(module, "_config", lambda _: config)
    monkeypatch.setattr(
        module, "_runtime", lambda _: pytest.fail("planning read a credential")
    )
    assert module.main(["window-plan", "--config", "/reviewed"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["active"] is False
    assert plan["maximum_work"] == {
        "model_calls": 3,
        "poll_pages": 4,
        "poll_gets": 4,
        "fetched_events": 40,
        "inbound_deliberations": 2,
        "research_operations_separate_authority": 0,
        "publication_posts": 0,
    }
    with pytest.raises(SystemExit):
        module.main(["window-plan"])
    with pytest.raises(SystemExit):
        module.main(["run-window", "--config", "/reviewed"])
    with pytest.raises(ValueError, match="reviewed"):
        module._window_spec({})
    with pytest.raises(ValueError, match="fixed"):
        module._window_spec(config | {"enable_public_mission_evolution": True})
    with pytest.raises(KeyboardInterrupt):
        module._interrupt(signal.SIGTERM, None)


def test_operator_run_window_routes_reviewed_spec_and_restores_signal(
    monkeypatch, capsys
):
    import signal
    from app.ops import int24_threads_runtime as module

    config = {
        "operating_window_reviewed": True,
        "public_mission_path": "/reviewed/mission.json",
        "operating_window": json.loads(spec().encoded()),
    }
    monkeypatch.setattr(module, "_config", lambda _: config)
    app = object()
    monkeypatch.setattr(module, "_runtime", lambda _: app)
    previous = signal.getsignal(signal.SIGTERM)

    class Runner:
        def __init__(self, runtime, specification):
            assert runtime is app
            assert specification == spec()

        def run(self):
            assert signal.getsignal(signal.SIGTERM) is module._interrupt
            return {"state": "interrupted"}

    monkeypatch.setattr(module, "PublicOperatingWindow", Runner)
    assert module.main(["run-window", "--config", "/reviewed", "--activate-live"]) == 0
    assert json.loads(capsys.readouterr().out) == {"state": "interrupted"}
    assert signal.getsignal(signal.SIGTERM) is previous


def test_plan_research_is_bounded_by_existing_public_authority(tmp_path):
    from app.ops.int24_threads_runtime import _window_plan
    from test_b05b_public_runtime import _profile

    path, _ = _profile(tmp_path)
    config = {
        "enable_public_research": True,
        "public_research_profile": path,
        "public_research_scope": "public-test",
        "account_ref": "account-1",
    }
    assert (
        _window_plan(config, spec())["maximum_work"][
            "research_operations_separate_authority"
        ]
        == 1
    )
    for changed in ({"account_ref": "other"}, {"public_research_scope": "other"}):
        with pytest.raises(ValueError, match="mismatch"):
            _window_plan(config | changed, spec())


@pytest.mark.parametrize(
    "changes",
    [
        {"expires_at": NOW.replace(tzinfo=None)},
        {"expires_at": "tomorrow"},
        {"max_model_calls": 0},
        {"max_model_calls": True},
        {"max_model_calls": 4097},
        {"max_poll_pages": -1},
        {"max_inbound_events": -1},
        {"poll_interval_seconds": 0},
        {"poll_interval_seconds": float("inf")},
        {"page_size": 11},
        {"window_id": ""},
    ],
)
def test_invalid_window_has_no_authority(changes):
    with pytest.raises(ValueError):
        spec(**changes)


def test_research_authority_change_stops_the_same_window(tmp_path, monkeypatch):
    from test_b05b_public_runtime import _attach_fake_research

    app, worker, sdk, http, clock = setup(tmp_path, NO_ACTION)
    _attach_fake_research(app, tmp_path, monkeypatch)

    def change(_):
        value = json.loads(app.research_profile_path.read_text())
        value["profile_id"] = "c" * 64
        app.research_profile_path.write_text(json.dumps(value))

    thread = serve(worker, 1)
    try:
        result = PublicOperatingWindow(app, spec(max_poll_pages=0)).run(wait=change)
        finish(thread)
        assert result["state"] == "controls_changed"
        assert (
            PublicOperatingWindow(app, spec(max_poll_pages=0)).run()["state"]
            == "controls_changed"
        )
        assert len(sdk.calls) == 1
        assert http.calls == []
        app.research_profile_path.unlink()
        assert app.current_research_profile() is None
        with pytest.raises(Exception, match="controls_changed"):
            PublicOperatingWindow(app, spec(window_id="invalid-research")).run()
    finally:
        worker.close()


def test_window_cannot_exceed_finite_horizon_or_journal_capacity(tmp_path, monkeypatch):
    import app.integration.public_window as module

    app, worker, sdk, http, _ = setup(tmp_path, NO_ACTION)
    window_spec = spec()
    try:
        with pytest.raises(ValueError, match="24 hours"):
            PublicOperatingWindow(app, spec(expires_at=NOW + timedelta(days=2))).run()
        monkeypatch.setattr(module, "MAX_INTENTS", 0)
        with pytest.raises(ValueError, match="capacity"):
            PublicOperatingWindow(app, window_spec).run()
        app.mission_evolution = object()
        with pytest.raises(ValueError, match="fixed"):
            PublicOperatingWindow(app, window_spec)
        assert sdk.calls == http.calls == []
    finally:
        worker.close()


def test_budget_admission_fence_denies_extra_model_work(tmp_path):
    app, worker, sdk, http, _ = setup(tmp_path, NO_ACTION)

    def extra(point):
        if point == "after_intent":
            with pytest.raises(ValueError, match="admission unavailable"):
                app._decide(
                    "extra-operation",
                    {
                        "kind": "wake",
                        "event": "extra",
                        "correlation": "extra-operation",
                    },
                    (),
                )

    app.checkpoint = extra
    thread = serve(worker, 1)
    try:
        result = PublicOperatingWindow(app, spec(max_model_calls=1)).run()
        finish(thread)
        assert result["consumed"]["model_calls"] == len(sdk.calls) == 1
        with pytest.raises(ValueError, match="not found"):
            app.journal.read("extra-operation")
        assert http.calls == []
    finally:
        worker.close()


def test_inbound_admission_fence_denies_oversized_batch(tmp_path):
    from app.threads.domain import InboundKind

    app, worker, sdk, http, clock = setup(tmp_path, NO_ACTION)
    http.reply_rows = [
        {
            "id": f"reply-{i}",
            "text": "Публичный вопрос.",
            "timestamp": NOW.isoformat(),
            "root_post": {"id": "root-1"},
            "replied_to": {"id": "root-1"},
            "is_reply_owned_by_me": False,
        }
        for i in range(1, 4)
    ]

    def excess(_):
        assert app.process_inbound(InboundKind.REPLY, limit=10) == ()
        raise KeyboardInterrupt

    thread = serve(worker, 2)
    try:
        result = PublicOperatingWindow(app, spec(max_inbound_events=1)).run(wait=excess)
        finish(thread)
        assert result["state"] == "interrupted"
        assert result["consumed"] == {
            "model_calls": 2,
            "poll_pages": 1,
            "inbound_events": 1,
        }
        assert len(sdk.calls) == 2
        assert len(http.calls) == 1
    finally:
        worker.close()


@pytest.mark.parametrize("mode", ["rejected", "unlinked", "approved", "held"])
def test_summary_classifies_durable_drafts_without_content_or_effects(tmp_path, mode):
    app, worker, sdk, http, _ = setup(
        tmp_path,
        {
            "decision": "propose_publication",
            "draft": "English only." if mode == "rejected" else "Публичный черновик.",
        },
    )

    def interrupt(point):
        if point == "after_intent":
            raise KeyboardInterrupt

    if mode in ("unlinked", "held"):
        app.checkpoint = interrupt
    window_spec = spec(max_model_calls=1)
    thread = serve(worker, 1)
    try:
        runner = PublicOperatingWindow(app, window_spec)
        result = runner.run()
        finish(thread)
        if mode == "approved":
            operation = result["pending_drafts"][0]["operation_id"]
            app.approve(operation, expires_at=NOW + timedelta(seconds=30))
            result = runner.run()
        elif mode == "held":
            operation = runner.operations()[0]
            app.journal.hold_intent(operation)
            result = runner.run()
        assert result["pending_drafts"] == []
        if mode in ("rejected", "held"):
            assert result["outcomes"] == {mode: 1}
        assert len(sdk.calls) == 1
        assert http.calls == []
        assert "Публичный черновик" not in json.dumps(result, ensure_ascii=False)
    finally:
        worker.close()


def test_poll_failure_is_reported_without_automatic_retry(tmp_path):
    from app.threads.transport import HTTPResponse

    app, worker, sdk, http, _ = setup(tmp_path, NO_ACTION)
    request = http.request

    def failed(*args):
        request(*args)
        return HTTPResponse(500, b"{}")

    http.request = failed
    thread = serve(worker, 1)
    try:
        result = PublicOperatingWindow(app, spec()).run()
        finish(thread)
        assert result["state"] == "failed"
        assert result["consumed"] == {
            "model_calls": 1,
            "poll_pages": 1,
            "inbound_events": 0,
        }
        assert len(http.calls) == len(sdk.calls) == 1
    finally:
        worker.close()


def test_selected_cadence_saturates_at_bounded_expiry(tmp_path):
    app, worker, sdk, http, _ = setup(tmp_path, NO_ACTION)
    app.scheduler.repository.select_cadence(timedelta.max)
    window_spec = spec(max_model_calls=1)
    thread = serve(worker, 1)
    try:
        result = PublicOperatingWindow(app, window_spec).run()
        finish(thread)
        assert result["state"] == "exhausted"
        assert result["next_due"]["wakeup"] == window_spec.expires_at.isoformat()
        assert len(sdk.calls) == 1
        assert http.calls == []
    finally:
        worker.close()


def test_verified_mention_in_window_becomes_reply_draft(tmp_path):
    from app.threads.transport import HTTPResponse

    app, worker, sdk, http, clock = setup(tmp_path, NO_ACTION)
    app.scheduler.repository.select_cadence(timedelta(minutes=10))

    def request(method, url, headers, body, timeout):
        http.calls.append((method, url, body))
        rows = []
        if "/mentions?" in url:
            rows.append(
                {
                    "id": "mention-1",
                    "text": "Публичный вопрос.",
                    "timestamp": NOW.isoformat(),
                }
            )
        return HTTPResponse(200, json.dumps({"data": rows, "paging": {}}).encode())

    http.request = request
    create = sdk.create

    def choose(**kwargs):
        if sdk.calls:
            sdk.proposal = {"decision": "reply", "content": "Спасибо за вопрос."}
        return create(**kwargs)

    sdk.create = choose
    thread = serve(worker, 2)
    try:
        result = PublicOperatingWindow(
            app, spec(max_model_calls=2, max_poll_pages=2)
        ).run(wait=lambda seconds: clock.advance(timedelta(seconds=seconds)))
        finish(thread)
        assert result["state"] == "exhausted"
        assert result["consumed"] == {
            "model_calls": 2,
            "poll_pages": 2,
            "inbound_events": 1,
        }
        assert len(sdk.calls) == 2
        assert len(http.calls) == 2
        assert all(call[0] == "GET" for call in http.calls)
        draft = app.draft(result["pending_drafts"][0]["operation_id"])
        assert draft.parent_message_id == "mention-1"
        assert draft.state is OutboxState.AWAITING_APPROVAL
    finally:
        worker.close()


def test_window_rejects_missing_reviewed_mission_before_any_work(tmp_path, monkeypatch):
    from test_int24_runtime import setup as without_mission
    from app.ops import int24_threads_runtime as module

    app, worker, sdk, http, _ = without_mission(tmp_path, NO_ACTION)
    config = {
        "operating_window_reviewed": True,
        "operating_window": json.loads(spec().encoded()),
    }
    monkeypatch.setattr(module, "_config", lambda _: config)
    monkeypatch.setattr(
        module,
        "_runtime",
        lambda _: pytest.fail("missing mission loaded runtime credentials"),
    )
    try:
        with pytest.raises(ValueError, match="mission"):
            PublicOperatingWindow(app, spec())
        with pytest.raises(ValueError, match="mission"):
            module.main(["run-window", "--config", "/reviewed", "--activate-live"])
        assert sdk.calls == http.calls == []
    finally:
        worker.close()


@pytest.mark.parametrize(
    "delay_point", ["after_started", "reservation", "worker_reservation"]
)
def test_absolute_window_deadline_survives_admission_and_dispatch_delays(
    tmp_path, delay_point
):
    app, worker, sdk, http, clock = setup(tmp_path, NO_ACTION)
    window_spec = spec(expires_at=NOW + timedelta(seconds=4), max_model_calls=1)
    if delay_point == "after_started":
        app.checkpoint = (
            lambda point: clock.advance(timedelta(seconds=3))
            if point == "after_started"
            else None
        )
    elif delay_point == "reservation":
        evidence = app.model.reservation_evidence

        def delayed(request):
            result = evidence(request)
            clock.advance(timedelta(seconds=3))
            return result

        app.model.reservation_evidence = delayed
    else:
        reserve = worker.provider_store.reserve_attempt

        def delayed(*args, **kwargs):
            result = reserve(*args, **kwargs)
            clock.advance(timedelta(seconds=3))
            return result

        worker.provider_store.reserve_attempt = delayed
    thread = serve(worker, 1)
    try:
        result = PublicOperatingWindow(app, window_spec).run()
        finish(thread)
        assert result["state"] == "exhausted"
        assert len(sdk.calls) == 1
        assert sdk.calls[0]["timeout"] == pytest.approx(1)
        payload = app.journal.read(
            PublicOperatingWindow(app, window_spec).operations()[0]
        )
        assert payload["deadline_at"] == window_spec.expires_at.isoformat()
        assert http.calls == []
    finally:
        worker.close()


@pytest.mark.parametrize("delay_point", ["reservation", "worker_reservation"])
def test_expiry_during_dispatch_never_starts_a_paid_request(tmp_path, delay_point):
    app, worker, sdk, http, clock = setup(tmp_path, NO_ACTION)
    window_spec = spec(expires_at=NOW + timedelta(seconds=4), max_model_calls=1)
    if delay_point == "reservation":
        evidence = app.model.reservation_evidence

        def delayed(request):
            result = evidence(request)
            clock.advance(timedelta(seconds=5))
            return result

        app.model.reservation_evidence = delayed
        thread = None
    else:
        reserve = worker.provider_store.reserve_attempt

        def delayed(*args, **kwargs):
            result = reserve(*args, **kwargs)
            clock.advance(timedelta(seconds=5))
            return result

        worker.provider_store.reserve_attempt = delayed
        thread = serve(worker, 1)
    try:
        result = PublicOperatingWindow(app, window_spec).run()
        if thread is not None:
            finish(thread)
        assert result["state"] == "expired"
        assert result["consumed"]["model_calls"] == 1
        assert (
            PublicOperatingWindow(app, window_spec).run()["consumed"]
            == result["consumed"]
        )
        assert sdk.calls == http.calls == []
    finally:
        worker.close()


@pytest.mark.parametrize("wire_deadline", [None, 42, "tomorrow", "2026-10-17T12:00:00"])
def test_public_request_rejects_invalid_absolute_deadlines(wire_deadline):
    from app.core.public_behavior import PublicBehaviorRequest, decode_public_request

    base = PublicBehaviorRequest(
        "operation-1", "operation-1", "autonomous_wakeup", (), (), 1
    )
    value = json.loads(base.encode()) | {"deadline_at": wire_deadline}
    with pytest.raises(ValueError):
        decode_public_request(json.dumps(value).encode())
    with pytest.raises(ValueError):
        replace(base, deadline_at=NOW.replace(tzinfo=None)).encode()
    bounded = replace(base, deadline_at=NOW)
    assert decode_public_request(bounded.encode()) == bounded
    assert "deadline_at" not in json.loads(base.encode())


def test_request_deadline_is_bound_to_the_durable_operation(tmp_path):
    from datetime import datetime
    from app.core.public_behavior import PublicBehaviorRequest

    app, worker, sdk, http, _ = setup(tmp_path, NO_ACTION)
    runner = PublicOperatingWindow(app, spec(max_model_calls=1))

    def check(point):
        if point == "after_started":
            operation = runner.operations()[0]
            payload = app.journal.read(operation)
            deadline = datetime.fromisoformat(payload["deadline_at"])
            request = PublicBehaviorRequest(
                operation,
                operation,
                "autonomous_wakeup",
                (),
                tuple(payload["mission_text"]),
                1,
                deadline_at=deadline,
            )
            assert app.attest_worker_request(request)
            assert not app.attest_worker_request(
                replace(request, deadline_at=deadline + timedelta(seconds=1))
            )
            raise KeyboardInterrupt

    app.checkpoint = check
    try:
        assert runner.run()["state"] == "interrupted"
        assert sdk.calls == http.calls == []
    finally:
        worker.close()
