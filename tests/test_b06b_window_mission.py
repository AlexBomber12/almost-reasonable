"""Mission evolution admitted by bounded windows, using worker IPC and journals."""

from datetime import timedelta
import json

import pytest

from app.integration.public_window import PublicOperatingWindow
from test_b06a_public_mission import configured, restarted_evolution_runtime
from test_b05b_public_runtime import _attach_fake_research
from test_b11c_window import NO_ACTION, spec, serve, finish


def choice(action, purpose=None):
    return {"decision": "mission", "mission_action": action, "mission_purpose": purpose}


def enabled_spec(**kwargs):
    return spec(
        mission_evolution=True, max_poll_pages=0, max_inbound_events=0, **kwargs
    )


def test_confirm_restart_and_deliberate_under_new_mission(tmp_path):
    app, worker, sdk, http, clock = configured(tmp_path, choice("propose", "research"))
    worker.utc_now = clock.now
    research, research_worker, research_thread = attach_research_ipc(app, tmp_path)
    decisions = [
        choice("propose", "research"),
        {"decision": "research", "topic_id": "ocean"},
        choice("confirm"),
        NO_ACTION,
    ]
    create = sdk.create

    def choose(**kwargs):
        sdk.proposal = decisions[len(sdk.calls)]
        return create(**kwargs)

    sdk.create = choose
    window_spec = enabled_spec(max_model_calls=4)
    thread = serve(worker, 3)

    def interrupt_after_confirmation(_):
        if len(sdk.calls) == 3:
            raise KeyboardInterrupt
        clock.advance(timedelta(seconds=1))

    try:
        first = PublicOperatingWindow(app, window_spec).run(
            wait=interrupt_after_confirmation
        )
        finish(thread)
        assert first["state"] == "interrupted"
        assert first["mission"]["active"]["purpose"] == "research"
        assert first["consumed"]["model_calls"] == 3
        app = restarted_evolution_runtime(app, tmp_path)
        app.model.utc_now = clock.now
        worker.attest = app.attest_worker_request
        thread = serve(worker, 1)
        result = PublicOperatingWindow(app, window_spec).run(
            wait=lambda _: clock.advance(timedelta(seconds=1))
        )
        finish(thread)
        assert result["state"] == "exhausted"
        assert result["consumed"] == {
            "model_calls": 4,
            "poll_pages": 0,
            "inbound_events": 0,
        }
        assert result["limits"] == first["limits"]
        assert result["research"] == {"consumed": 1, "outcomes": {"completed": 1}}
        assert len(research) == 1
        assert len(sdk.calls) == 4
        assert "unverified" in sdk.calls[2]["input"][1]["content"]
        assert app.mission.snapshot()[0] in sdk.calls[3]["input"][1]["content"]
        assert (
            PublicOperatingWindow(app, window_spec).run()["consumed"]
            == result["consumed"]
        )
        assert http.calls == []
    finally:
        worker.close()
        finish(research_thread)
        research_worker.close()


@pytest.mark.parametrize("action", ["reject", "abandon"])
def test_terminal_choice_keeps_mission_and_consumed_research(
    tmp_path, monkeypatch, action
):
    app, worker, sdk, http, clock = configured(
        tmp_path, choice("propose", "reflection")
    )
    worker.utc_now = clock.now
    research = _attach_fake_research(app, tmp_path, monkeypatch)
    decisions = [
        choice("propose", "reflection"),
        {"decision": "research", "topic_id": "ocean"},
        choice(action),
    ]
    create = sdk.create

    def choose(**kwargs):
        sdk.proposal = decisions[len(sdk.calls)]
        return create(**kwargs)

    sdk.create = choose
    thread = serve(worker, 3)
    try:
        result = PublicOperatingWindow(app, enabled_spec(max_model_calls=3)).run(
            wait=lambda _: clock.advance(timedelta(seconds=1))
        )
        finish(thread)
        assert result["state"] == "exhausted"
        assert result["mission"]["active"]["version"] == "mission-v1"
        assert result["mission"]["pending"] is None
        assert app.mission_evolution.document().proposals[0].status.value == (
            "rejected" if action == "reject" else "abandoned"
        )
        assert len(research) == 1
        assert http.calls == []
    finally:
        worker.close()


def test_window_mode_is_explicit_and_durable(tmp_path):
    from dataclasses import replace

    app, worker, sdk, http, _ = configured(tmp_path, NO_ACTION)
    try:
        with pytest.raises(ValueError, match="mode"):
            PublicOperatingWindow(app, spec())
        window = enabled_spec()
        runner = PublicOperatingWindow(app, window)
        runner._initialize()
        with pytest.raises(ValueError, match="changed"):
            PublicOperatingWindow(app, replace(window, max_model_calls=4)).run()
        assert json.loads(runner.snapshot["spec"])["mission_evolution"] is True
        assert sdk.calls == http.calls == []
    finally:
        worker.close()


@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
def test_transition_and_binding_recover_together_without_inference_replay(
    tmp_path, monkeypatch, point
):
    app, worker, sdk, http, clock = configured(tmp_path, choice("propose", "research"))
    worker.utc_now = clock.now
    research = _attach_fake_research(app, tmp_path, monkeypatch)
    decisions = [
        choice("propose", "research"),
        {"decision": "research", "topic_id": "ocean"},
        choice("confirm"),
        NO_ACTION,
    ]
    create = sdk.create

    def choose(**kwargs):
        sdk.proposal = decisions[len(sdk.calls)]
        return create(**kwargs)

    sdk.create = choose
    window_spec = enabled_spec(max_model_calls=4)
    runner = PublicOperatingWindow(app, window_spec)
    bind = runner.bind_transition
    record = app._record_mission_outcome

    def crash_before(*args):
        bind(*args)
        if len(sdk.calls) == 3:
            raise KeyboardInterrupt

    def crash_after(*args):
        if len(sdk.calls) == 3:
            raise KeyboardInterrupt
        record(*args)

    if point == "before_commit":
        runner.bind_transition = crash_before
    else:
        app._record_mission_outcome = crash_after
    thread = serve(worker, 3)
    try:
        first = runner.run(wait=lambda _: clock.advance(timedelta(seconds=1)))
        finish(thread)
        assert first["state"] == "interrupted"
        assert first["mission"]["active"]["purpose"] == (
            "general_inquiry" if point == "before_commit" else "research"
        )
        app = restarted_evolution_runtime(app, tmp_path)
        app.model.utc_now = clock.now
        worker.attest = app.attest_worker_request
        thread = serve(worker, 1)
        result = PublicOperatingWindow(app, window_spec).run(
            wait=lambda _: clock.advance(timedelta(seconds=1))
        )
        finish(thread)
        assert result["state"] == "exhausted"
        assert result["mission"]["active"]["purpose"] == "research"
        assert result["mission"]["pending"] is None
        assert len(app.mission_evolution.document().proposals) == 1
        assert result["consumed"]["model_calls"] == 4
        assert len(sdk.calls) == 4
        assert len(research) == 1
        assert http.calls == []
    finally:
        worker.close()


@pytest.mark.parametrize("research_outcome", ["denied", "uncertain", "crash"])
def test_abandon_after_consumed_unsuccessful_research(
    tmp_path, monkeypatch, research_outcome
):
    app, worker, sdk, http, clock = configured(tmp_path, choice("propose", "research"))
    worker.utc_now = clock.now
    calls = _attach_fake_research(app, tmp_path, monkeypatch)

    def unsuccessful(request):
        calls.append(request)
        return {
            "outcome": research_outcome,
            "attempts": int(research_outcome == "uncertain"),
        }

    app.research_client.execute = unsuccessful
    if research_outcome == "crash":

        def crash(point):
            if point == "after_research_intent":
                raise KeyboardInterrupt

        app.checkpoint = crash
    decisions = [
        choice("propose", "research"),
        {"decision": "research", "topic_id": "ocean"},
        choice("abandon"),
    ]
    create = sdk.create

    def choose(**kwargs):
        sdk.proposal = decisions[len(sdk.calls)]
        return create(**kwargs)

    sdk.create = choose
    thread = serve(worker, 3 if research_outcome != "crash" else 2)
    window_spec = enabled_spec(max_model_calls=3)
    try:
        result = PublicOperatingWindow(app, window_spec).run(
            wait=lambda _: clock.advance(timedelta(seconds=1))
        )
        finish(thread)
        if research_outcome == "crash":
            assert result["mission"]["pending"] is not None
            app = restarted_evolution_runtime(app, tmp_path)
            app.model.utc_now = clock.now
            worker.attest = app.attest_worker_request
            thread = serve(worker, 1)
            result = PublicOperatingWindow(app, window_spec).run(
                wait=lambda _: clock.advance(timedelta(seconds=1))
            )
            finish(thread)
        assert result["state"] == "exhausted"
        assert result["mission"]["pending"] is None
        assert result["mission"]["active"]["version"] == "mission-v1"
        assert result["research"]["consumed"] == 1
        assert result["research"]["outcomes"] == {
            "failed" if research_outcome == "denied" else "uncertain": 1
        }
        assert len(calls) == (0 if research_outcome == "crash" else 1)
        assert len(sdk.calls) == 3
        assert http.calls == []
    finally:
        worker.close()


@pytest.mark.parametrize(
    "change", ["stop", "expiry", "clock", "generation", "profile", "root", "evidence"]
)
def test_confirmed_window_rechecks_authority_before_more_work(
    tmp_path, monkeypatch, change
):
    app, worker, sdk, http, clock = configured(tmp_path, choice("propose", "research"))
    worker.utc_now = clock.now
    _attach_fake_research(app, tmp_path, monkeypatch)
    decisions = [
        choice("propose", "research"),
        {"decision": "research", "topic_id": "ocean"},
        choice("confirm"),
    ]
    create = sdk.create

    def choose(**kwargs):
        sdk.proposal = decisions[len(sdk.calls)]
        return create(**kwargs)

    sdk.create = choose
    window_spec = enabled_spec(max_model_calls=4)
    generation = app.ledger.control_generation()

    def mutate(_):
        if len(sdk.calls) < 3:
            clock.advance(timedelta(seconds=1))
        elif change == "stop":
            with app.owner._connection() as db:
                db.execute("UPDATE owner_stop SET active=1")
        elif change == "expiry":
            clock.advance(timedelta(minutes=1))
        elif change == "clock":
            clock.advance(timedelta(seconds=-1))
        elif change == "generation":
            monkeypatch.setattr(
                app.ledger, "control_generation", lambda: generation + 1
            )
        elif change == "profile":
            app.research_profile_path.unlink()
        elif change == "root":
            value = json.loads(app.mission.path.read_text())
            value["version"] = "replacement-root"
            app.mission.path.write_text(json.dumps(value))
        else:
            with app.journal.transaction() as db:
                db.execute("DELETE FROM public_research_completions")

    thread = serve(worker, 3)
    try:
        result = PublicOperatingWindow(app, window_spec).run(wait=mutate)
        finish(thread)
        assert result["state"] == {
            "stop": "controls_denied",
            "expiry": "expired",
            "clock": "clock_regressed",
        }.get(change, "controls_changed")
        assert result["consumed"]["model_calls"] == 3
        assert len(sdk.calls) == 3
        assert http.calls == []
    finally:
        worker.close()


def test_transition_must_belong_to_this_window(tmp_path):
    from app.integration.public_window import WindowDenied

    app, worker, sdk, http, _ = configured(tmp_path, NO_ACTION)
    runner = PublicOperatingWindow(app, enabled_spec())
    try:
        runner._initialize()
        with app.journal.transaction() as db:
            with pytest.raises(WindowDenied, match="controls_changed"):
                runner.bind_transition(
                    db,
                    "foreign-operation",
                    {"mission": app.mission.snapshot()[1]},
                    app.mission_evolution.root,
                )
            db.execute(
                "INSERT INTO public_window_work VALUES(?,?,?,'started')",
                (runner.spec.window_id, "admitted", "wake"),
            )
            with pytest.raises(WindowDenied, match="controls_changed"):
                runner.bind_transition(
                    db, "admitted", {"mission": {}}, app.mission_evolution.root
                )
        assert sdk.calls == http.calls == []
    finally:
        worker.close()


@pytest.mark.parametrize("value", [1, None, "true"])
def test_window_mode_requires_boolean(value):
    with pytest.raises(ValueError, match="mode"):
        spec(mission_evolution=value)


def test_operator_plan_evolving_window_is_inactive_and_credential_free(
    monkeypatch, capsys
):
    from app.ops import int24_threads_runtime as module

    config = {
        "operating_window_reviewed": True,
        "public_mission_path": "/reviewed/mission.json",
        "operating_window": json.loads(enabled_spec().encoded()),
        "enable_public_mission_evolution": True,
    }
    monkeypatch.setattr(module, "_config", lambda _: config)
    monkeypatch.setattr(
        module, "_runtime", lambda _: pytest.fail("plan loaded credentials")
    )
    assert module.main(["window-plan", "--config", "/reviewed"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["active"] is False
    assert plan["window"]["mission_evolution"] is True
    assert plan["maximum_work"]["publication_posts"] == 0
    with pytest.raises(ValueError, match="mode"):
        module._window_spec(config | {"enable_public_mission_evolution": False})


def attach_research_ipc(app, tmp_path):
    """Real research worker and authority journal; only the paid SDK is fake."""
    import os
    from threading import Thread
    from app.core.provider_spend import ProviderStateStore
    from app.core.store import EventStore
    from app.core.public_research_client import PublicResearchClient
    from app.core.public_research_profile import load_research_profile
    from app.trusted.openai_worker_server import WorkerServerConfig
    from app.trusted.public_research import PublicResearchBoundary, PublicResearchServer
    from app.trusted.public_research_transport import PublicResearchTransport
    from app.trusted.research_authority_journal import AuthorityJournal
    from test_int20_public_research import SDK, raw_response

    ipc = tmp_path / "research-ipc"
    ipc.mkdir(mode=0o700)
    authority_path = ipc / "authority.sqlite"
    authority_path.touch(mode=0o600)
    sdk = SDK([raw_response()])
    boundary = PublicResearchBoundary(
        store=ProviderStateStore(EventStore(tmp_path / "search-events.sqlite")),
        transport=PublicResearchTransport(sdk),
        enabled=True,
        utcnow=app.clock.now,
    )
    profile_path = tmp_path / "worker.json"
    boundary.install_profile(
        load_research_profile(profile_path, role="worker"),
        profile_path=profile_path,
        journal=AuthorityJournal(authority_path),
    )
    worker = PublicResearchServer(
        WorkerServerConfig(ipc / "research.sock", frozenset({os.geteuid()})), boundary
    )
    app.research_client = PublicResearchClient(
        ipc / "research.sock", expected_worker_uid=os.geteuid()
    )
    worker.start()
    thread = Thread(target=lambda: worker.serve(max_connections=1), daemon=True)
    thread.start()
    return sdk.calls, worker, thread


def test_external_confirmed_transition_is_not_adopted_on_resume(tmp_path, monkeypatch):
    from test_int24_runtime import NOW, run_once
    from test_b06a_public_mission import research_after_proposal, decide_wakeup

    app, worker, sdk, http, clock = configured(tmp_path, choice("propose", "research"))
    worker.utc_now = clock.now
    _attach_fake_research(app, tmp_path, monkeypatch)
    window_spec = enabled_spec(
        max_model_calls=4, expires_at=NOW + timedelta(minutes=10)
    )
    runner = PublicOperatingWindow(app, window_spec)
    try:
        runner._initialize()
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        sdk.proposal = choice("confirm")
        clock.advance(timedelta(seconds=1))
        decide_wakeup(app, worker, clock)
        assert app.mission.snapshot()[1]["purpose"] == "research"
        # These ordinary operations were never admitted to the window.
        result = PublicOperatingWindow(app, window_spec).run()
        assert result["state"] == "controls_changed"
        assert result["consumed"]["model_calls"] == 0
        assert len(sdk.calls) == 3
        assert all(call[0] == "GET" for call in http.calls)
    finally:
        worker.close()


@pytest.mark.parametrize(
    "final",
    [NO_ACTION, {"decision": "propose_publication", "draft": "Публичный черновик."}],
)
def test_discontinued_channel_excludes_publication_in_later_window_decisions(
    tmp_path, monkeypatch, final
):
    app, worker, sdk, http, clock = configured(
        tmp_path, choice("propose", "discontinue_optional_channel")
    )
    worker.utc_now = clock.now
    _attach_fake_research(app, tmp_path, monkeypatch)
    decisions = [
        choice("propose", "discontinue_optional_channel"),
        {"decision": "research", "topic_id": "ocean"},
        choice("confirm"),
        final,
    ]
    create = sdk.create

    def choose(**kwargs):
        sdk.proposal = decisions[len(sdk.calls)]
        return create(**kwargs)

    sdk.create = choose
    thread = serve(worker, 4)
    try:
        result = PublicOperatingWindow(app, enabled_spec(max_model_calls=4)).run(
            wait=lambda _: clock.advance(timedelta(seconds=1))
        )
        finish(thread)
        assert result["state"] == "exhausted"
        assert not app.mission.publication_allowed()
        assert result["pending_drafts"] == []
        assert result["outcomes"].get("rejected", 0) == int(final != NO_ACTION)
        assert len(sdk.calls) == 4
        assert http.calls == []
    finally:
        worker.close()


def test_stale_window_proposal_is_held_after_competing_proposal(tmp_path):
    from app.threads.domain import InboundKind

    app, worker, sdk, http, clock = configured(
        tmp_path, choice("propose", "reflection")
    )
    worker.utc_now = clock.now
    window_spec = enabled_spec(max_model_calls=2)
    rival = restarted_evolution_runtime(app, tmp_path)
    rival.model.utc_now = clock.now
    create = sdk.create
    decisions = [
        choice("propose", "reflection"),
        choice("propose", "research"),
        choice("abandon"),
    ]

    def choose(**kwargs):
        sdk.proposal = decisions[len(sdk.calls)]
        return create(**kwargs)

    sdk.create = choose

    def compete(point):
        if point == "after_intent" and len(sdk.calls) == 1:
            clock.advance(timedelta(seconds=1))
            worker.attest = rival.attest_worker_request
            rival.poll(InboundKind.REPLY, correlation_id="competing-proposal")
            rival.process_inbound(InboundKind.REPLY)
            worker.attest = app.attest_worker_request

    app.checkpoint = compete
    thread = serve(worker, 3)
    try:
        result = PublicOperatingWindow(app, window_spec).run(
            wait=lambda _: clock.advance(timedelta(seconds=1))
        )
        finish(thread)
        assert result["state"] == "exhausted"
        assert result["outcomes"] == {"held": 1, "revise_mission": 1}
        assert result["consumed"]["model_calls"] == 2
        assert result["mission"]["active"]["version"] == "mission-v1"
        records = app.mission_evolution.document().proposals
        assert len(records) == 1
        assert records[0].proposal.purpose.value == "research"
        assert records[0].status.value == "abandoned"
        assert len(sdk.calls) == 3  # includes the explicitly competing ordinary call
        assert len(http.calls) == 1
        assert http.calls[0][0] == "GET"
    finally:
        worker.close()


def test_resume_cannot_change_persisted_window_mode(tmp_path):
    from dataclasses import replace
    from test_b11c_window import setup

    app, worker, sdk, http, _ = setup(tmp_path, NO_ACTION)
    fixed = spec()
    try:
        PublicOperatingWindow(app, fixed)._initialize()
        app.mission_evolution = object()
        with pytest.raises(ValueError, match="configuration changed"):
            PublicOperatingWindow(app, replace(fixed, mission_evolution=True)).run()
        # Even a forged call into the transition hook cannot evolve a fixed window.
        app.mission_evolution = None
        runner = PublicOperatingWindow(app, fixed)
        runner._initialize()
        from app.integration.public_window import WindowDenied

        with app.journal.transaction() as db:
            with pytest.raises(WindowDenied, match="controls_changed"):
                runner.bind_transition(
                    db,
                    "unadmitted",
                    {"mission": app.mission.snapshot()[1]},
                    app.mission.root_mission(),
                )
        assert sdk.calls == http.calls == []
    finally:
        worker.close()


def test_window_preserves_existing_pending_proposal_without_unrelated_acknowledgement(
    tmp_path,
):
    from app.threads.domain import InboundKind

    app, worker, sdk, http, _ = configured(tmp_path, choice("propose", "research"))
    app.poll(InboundKind.REPLY, correlation_id="ordinary-poll")
    record = app._record_mission_outcome
    app._record_mission_outcome = lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
    thread = serve(worker, 1)
    try:
        with pytest.raises(KeyboardInterrupt):
            app.process_inbound(InboundKind.REPLY)
        finish(thread)
        with app.journal.transaction() as db:
            operation = db.execute(
                "SELECT operation FROM intents WHERE phase='linked'"
            ).fetchone()[0]
        assert not app.service.store.conversation_outcome_recorded(operation)
        pending = app.mission_evolution.pending().proposal.proposal_id
        app._record_mission_outcome = record
        sdk.proposal = NO_ACTION
        worker.utc_now = app.clock.now
        thread = serve(worker, 1)
        result = PublicOperatingWindow(app, enabled_spec(max_model_calls=1)).run()
        finish(thread)
        assert result["state"] == "exhausted"
        assert result["mission"]["pending"] == pending
        assert not app.service.store.conversation_outcome_recorded(operation)
        app.recover()
        assert app.service.store.conversation_outcome_recorded(operation)
        assert len(sdk.calls) == 2
        assert len(http.calls) == 1
        assert http.calls[0][0] == "GET"
    finally:
        worker.close()
