"""An intrinsic preference selects timing only inside immutable owner bounds."""

from datetime import timedelta
import json

import pytest

from app.core.intellectual_state import validate_fields
from app.integration.public_window import PublicOperatingWindow
from test_agent_intellectual_state import proposal
from test_b11c_window import setup, spec, serve, finish


def test_cadence_preference_has_a_closed_integer_shape():
    validate_fields("cadence_preference", {"seconds": 30})
    for value in (True, 0, -1, 86401, "30", 1.5):
        with pytest.raises(ValueError):
            validate_fields("cadence_preference", {"seconds": value})


def test_actual_sdk_preference_changes_next_wakeup_and_survives_reopen(tmp_path):
    app, worker, sdk, http, clock = setup(
        tmp_path,
        {"decision": "no_action", "reason_category": "other"},
        agent_cadence_enabled=True,
    )
    create = sdk.create

    def choose(**kwargs):
        frame = json.loads(kwargs["input"][1]["content"])["intellectual_state"]
        source = next(s for s in frame["sources"] if s["kind"] == "capability_snapshot")
        sdk.proposal["intellectual"] = (
            proposal(
                source["handle"], kind="cadence_preference", fields={"seconds": 30}
            )
            if not sdk.calls
            else None
        )
        return create(**kwargs)

    sdk.create = choose
    thread = serve(worker, 2)
    try:
        result = PublicOperatingWindow(
            app, spec(max_model_calls=2, max_poll_pages=0, max_inbound_events=0)
        ).run(wait=lambda seconds: clock.advance(timedelta(seconds=seconds)))
        finish(thread)
        assert result["state"] == "exhausted" and len(sdk.calls) == 2
        assert clock.now() >= __import__("test_int24_runtime").NOW + timedelta(
            seconds=30
        )
        assert app.scheduler.repository.current_cadence(
            timedelta(seconds=1)
        ) == timedelta(seconds=30)
        assert app.scheduler.repository.active_agent_cadence() is not None
        assert http.calls == []
    finally:
        worker.close()


@pytest.fixture
def selected(tmp_path):
    app, worker, sdk, _, clock = setup(
        tmp_path,
        {"decision": "no_action", "reason_category": "other"},
        agent_cadence_enabled=True,
    )
    create = sdk.create

    def choose(**kwargs):
        frame = json.loads(kwargs["input"][1]["content"])["intellectual_state"]
        source = next(s for s in frame["sources"] if s["kind"] == "capability_snapshot")
        sdk.proposal["intellectual"] = proposal(
            source["handle"], kind="cadence_preference", fields={"seconds": 30}
        )
        return create(**kwargs)

    sdk.create = choose
    thread = serve(worker, 1)
    window = PublicOperatingWindow(
        app, spec(max_model_calls=1, max_poll_pages=0, max_inbound_events=0)
    )
    try:
        window.run(wait=lambda seconds: clock.advance(timedelta(seconds=seconds)))
        finish(thread)
        assert app.scheduler.repository.active_agent_cadence() is not None
        yield app, sdk, clock, window
    finally:
        worker.close()


def test_withdrawn_cadence_provenance_pauses_until_explicit_owner_reset(selected):
    from app.threads.service import PolicyDeniedError

    app, _, _, _ = selected
    with app.ledger._open() as db:
        app.ledger._increment_control_generation(db)
    with pytest.raises(PolicyDeniedError, match="cadence source withdrawn"):
        app.guard()
    app.scheduler.repository.select_cadence(timedelta(seconds=10))
    assert app.scheduler.repository.active_agent_cadence() is None
    app.guard()


def test_disabling_mode_does_not_reinterpret_cached_agent_cadence(selected):
    from app.integration.agent_cadence import AgentCadence
    from app.threads.service import PolicyDeniedError

    app, _, _, _ = selected
    app.agent_cadence = AgentCadence(app, False, app.scheduler.policy)
    with pytest.raises(PolicyDeniedError):
        app.guard()
    app.scheduler.repository.select_cadence(app.scheduler.policy.cadence)
    app.guard()


def test_cadence_effect_rechecks_source_after_admission(selected, monkeypatch):
    from app.threads.service import PolicyDeniedError

    app, _, _, window = selected
    operation = app.scheduler.repository.active_agent_cadence()
    payload = app.journal.read(operation)
    app.operating_window = window
    app.scheduler.repository.select_cadence(timedelta(seconds=10))
    monkeypatch.setattr(app, "source_valid", lambda _payload: False)
    try:
        with pytest.raises(PolicyDeniedError, match="source changed"):
            app.agent_cadence.apply(operation, payload)
        assert app.scheduler.repository.current_cadence(
            timedelta(seconds=1)
        ) == timedelta(seconds=10)
    finally:
        app.operating_window = None


def test_scheduler_selection_is_idempotent_stale_safe_and_owner_resettable(tmp_path):
    from app.behavior.durable_scheduler import SQLiteSchedulerRepository
    from test_int24_runtime import NOW

    repo = SQLiteSchedulerRepository(tmp_path / "scheduler.db")
    assert repo.select_agent_cadence("first", 30, NOW)
    assert not repo.select_agent_cadence("first", 10, NOW + timedelta(seconds=1))
    assert not repo.select_agent_cadence("older", 10, NOW - timedelta(seconds=1))
    assert repo.select_agent_cadence("second", 20, NOW + timedelta(seconds=1))
    repo = SQLiteSchedulerRepository(repo.path)
    assert repo.active_agent_cadence() == "second"
    assert repo.current_cadence(timedelta(seconds=1)) == timedelta(seconds=20)
    repo.select_cadence(timedelta(seconds=5))
    assert repo.active_agent_cadence() is None
    assert not repo.select_agent_cadence("first", 30, NOW)


@pytest.mark.parametrize("seconds", [True, 0, 86401])
def test_scheduler_cannot_accept_unbounded_preference(tmp_path, seconds):
    from app.behavior.durable_scheduler import SQLiteSchedulerRepository
    from test_int24_runtime import NOW

    repo = SQLiteSchedulerRepository(tmp_path / "scheduler.db")
    with pytest.raises(ValueError, match="bounded cadence"):
        repo.select_agent_cadence("invalid", seconds, NOW)
    assert repo.active_agent_cadence() is None


def test_cadence_journal_cannot_silently_reset_its_capacity(tmp_path, monkeypatch):
    import app.behavior.durable_scheduler as module
    from test_int24_runtime import NOW

    repo = module.SQLiteSchedulerRepository(tmp_path / "scheduler.db")
    assert repo.select_agent_cadence("first", 30, NOW)
    monkeypatch.setattr(module, "MAX_PROCESSED_EVENTS", 1)
    with pytest.raises(ValueError, match="capacity"):
        repo.select_agent_cadence("second", 20, NOW + timedelta(seconds=1))
    assert repo.active_agent_cadence() == "first"


@pytest.mark.parametrize("flag", [1, "true"])
def test_agent_cadence_mode_is_explicit_boolean(tmp_path, flag):
    with pytest.raises(ValueError, match="cadence mode"):
        setup(
            tmp_path,
            {"decision": "no_action", "reason_category": "other"},
            agent_cadence_enabled=flag,
        )


def test_cadence_policy_rejects_an_incompatible_owner_floor(tmp_path):
    from dataclasses import replace
    from app.integration.agent_cadence import AgentCadence

    app, worker, _, _, _ = setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    try:
        with pytest.raises(ValueError, match="owner cadence"):
            AgentCadence(
                app, True, replace(app.scheduler.policy, cadence=timedelta(days=2))
            )
    finally:
        worker.close()


def test_owner_policy_change_rejects_an_old_slower_or_faster_choice(selected):
    from dataclasses import replace
    from app.integration.agent_cadence import AgentCadence
    from app.threads.service import PolicyDeniedError

    app, _, _, window = selected
    operation = app.scheduler.repository.active_agent_cadence()
    app.agent_cadence = AgentCadence(
        app, True, replace(app.scheduler.policy, cadence=timedelta(seconds=60))
    )
    app.operating_window = window
    try:
        with pytest.raises(PolicyDeniedError, match="owner bounds"):
            app.agent_cadence.apply(operation, app.journal.read(operation))
    finally:
        app.operating_window = None


def test_other_intellectual_changes_do_not_become_scheduling_authority(tmp_path):
    app, worker, sdk, _, clock = setup(
        tmp_path,
        {
            "decision": "no_action",
            "reason_category": "other",
            "intellectual": proposal(),
        },
        agent_cadence_enabled=True,
    )
    thread = serve(worker, 1)
    try:
        PublicOperatingWindow(
            app, spec(max_model_calls=1, max_poll_pages=0, max_inbound_events=0)
        ).run(wait=lambda seconds: clock.advance(timedelta(seconds=seconds)))
        finish(thread)
        assert (
            len(sdk.calls) == 1
            and app.scheduler.repository.active_agent_cadence() is None
        )
    finally:
        worker.close()
