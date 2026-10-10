from datetime import UTC, datetime, time, timedelta

import pytest

from app.behavior.deliberation import AgentDecision
from app.behavior.scheduler import (
    SCHEDULER_VERSION,
    InMemorySchedulerStore,
    MutableClock,
    SchedulerOutcome,
    TestScheduler,
    WakeupEvent,
    WakeupPolicy,
)


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def event(event_id: str, scheduled_at: datetime = NOW) -> WakeupEvent:
    return WakeupEvent(event_id, scheduled_at, "periodic")


def policy(**changes: object) -> WakeupPolicy:
    values = {
        "active_start": time(8),
        "active_end": time(20),
        "cadence": timedelta(hours=1),
        "cooldown": timedelta(minutes=10),
        "duplicate_window": timedelta(days=1),
        "max_iterations": 1,
        "max_execution_seconds": 2.0,
        "per_action_limits": ((AgentDecision.RESEARCH, 1),),
    }
    values.update(changes)
    return WakeupPolicy(**values)  # type: ignore[arg-type]


def test_scheduler_coalesces_missed_wakeups_and_allows_inactivity() -> None:
    store = InMemorySchedulerStore()
    clock = MutableClock(NOW)
    calls: list[str] = []

    def deliberate(wakeup: WakeupEvent, timeout_seconds: float) -> AgentDecision:
        assert timeout_seconds == 2.0
        calls.append(wakeup.event_id)
        return AgentDecision.NO_ACTION

    result = TestScheduler(policy(), store, clock, deliberate).run(
        (
            event("old", NOW - timedelta(hours=3)),
            event("new", NOW - timedelta(minutes=1)),
        )
    )

    assert SCHEDULER_VERSION == "behavior-scheduler-v1"
    assert result.outcome is SchedulerOutcome.NO_ACTION
    assert result.selected_event_id == "new"
    assert result.coalesced_count == 1
    assert result.iterations == 1
    assert calls == ["new"]
    assert store.load().processed_event_ids == ("old", "new")


@pytest.mark.parametrize(
    ("stop", "spend", "hour", "expected"),
    [
        (True, False, 12, SchedulerOutcome.SKIPPED_STOP),
        (False, True, 12, SchedulerOutcome.SKIPPED_SPEND),
        (False, False, 3, SchedulerOutcome.OUTSIDE_ACTIVE_HOURS),
    ],
)
def test_scheduler_operational_skips_do_not_call_model(
    stop: bool, spend: bool, hour: int, expected: SchedulerOutcome
) -> None:
    clock = MutableClock(NOW.replace(hour=hour))

    def forbidden(_: WakeupEvent, timeout_seconds: float) -> AgentDecision:
        assert timeout_seconds == 2.0
        raise AssertionError("model must not run")

    result = TestScheduler(policy(), InMemorySchedulerStore(), clock, forbidden).run(
        (event("e", clock.now()),), emergency_stop=stop, spend_suspended=spend
    )
    assert result.outcome is expected
    assert result.iterations == 0


def test_duplicate_cooldown_rate_limit_and_later_eligibility() -> None:
    store = InMemorySchedulerStore()
    clock = MutableClock(NOW)
    decisions = iter(
        (
            AgentDecision.RESEARCH,
            AgentDecision.RESEARCH,
            AgentDecision.PROPOSE_PUBLICATION,
        )
    )
    scheduler = TestScheduler(
        policy(), store, clock, lambda _, timeout: next(decisions)
    )

    first = scheduler.run((event("one"),))
    duplicate = scheduler.run((event("one"),))
    clock.advance(timedelta(minutes=11))
    rate_limited = scheduler.run((event("two", clock.now()),))
    clock.advance(timedelta(hours=1))
    later = scheduler.run((event("three", clock.now()),))

    assert first.outcome is SchedulerOutcome.PROPOSED
    assert duplicate.outcome is SchedulerOutcome.DUPLICATE
    assert rate_limited.outcome is SchedulerOutcome.RATE_LIMITED
    assert later.outcome is SchedulerOutcome.PROPOSED


def test_cooldown_empty_input_invalid_decision_and_execution_bound() -> None:
    store = InMemorySchedulerStore()
    clock = MutableClock(NOW)
    scheduler = TestScheduler(
        policy(), store, clock, lambda _, timeout: AgentDecision.NO_ACTION
    )
    assert scheduler.run(()).outcome is SchedulerOutcome.NOTHING_DUE
    assert scheduler.run((event("one"),)).outcome is SchedulerOutcome.NO_ACTION
    assert scheduler.run((event("two", NOW),)).outcome is SchedulerOutcome.COOLDOWN

    clock.advance(timedelta(minutes=11))
    invalid = TestScheduler(
        policy(),
        store,
        clock,
        lambda _, timeout: "reply",  # type: ignore[arg-type]
    )
    assert (
        invalid.run((event("three", clock.now()),)).outcome is SchedulerOutcome.FAILED
    )

    def slow(_: WakeupEvent, timeout_seconds: float) -> AgentDecision:
        assert timeout_seconds == 2.0
        clock.advance(timedelta(seconds=3))
        return AgentDecision.REPLY

    clock.advance(timedelta(minutes=11))
    assert (
        TestScheduler(policy(), store, clock, slow)
        .run((event("four", clock.now()),))
        .outcome
        is SchedulerOutcome.EXECUTION_LIMIT
    )

    timeout_clock = MutableClock(NOW)

    def timed_out(_: WakeupEvent, timeout_seconds: float) -> AgentDecision:
        assert timeout_seconds == 2.0
        raise TimeoutError

    timeout_result = TestScheduler(
        policy(), InMemorySchedulerStore(), timeout_clock, timed_out
    ).run((event("timeout"),))
    assert timeout_result.outcome is SchedulerOutcome.EXECUTION_LIMIT
    assert timeout_result.decision is None


def test_overnight_active_hours_expired_duplicate_and_validation() -> None:
    overnight = policy(active_start=time(22), active_end=time(6))
    clock = MutableClock(NOW.replace(hour=23))
    store = InMemorySchedulerStore()
    scheduler = TestScheduler(
        overnight, store, clock, lambda _, timeout: AgentDecision.NO_ACTION
    )
    assert (
        scheduler.run((event("night", clock.now()),)).outcome
        is SchedulerOutcome.NO_ACTION
    )
    clock.advance(timedelta(days=2))
    assert (
        scheduler.run((event("night", clock.now()),)).outcome
        is SchedulerOutcome.NO_ACTION
    )
    with pytest.raises(ValueError, match="max_iterations"):
        policy(max_iterations=0)
    with pytest.raises(ValueError, match="event_id"):
        WakeupEvent("", NOW, "periodic")


def test_scheduler_rejects_invalid_policy_event_clock_and_pending() -> None:
    invalid_policy_changes = (
        {"max_execution_seconds": 0},
        {"max_execution_seconds": float("nan")},
        {"cadence": timedelta(0)},
        {"duplicate_window": timedelta(0)},
        {"per_action_limits": (("research", 1),)},
    )
    for changes in invalid_policy_changes:
        with pytest.raises(ValueError):
            policy(**changes)
    with pytest.raises(ValueError, match="scheduled_at"):
        WakeupEvent("e", NOW.replace(tzinfo=None), "periodic")
    with pytest.raises(ValueError, match="clock"):
        MutableClock(NOW.replace(tzinfo=None))
    scheduler = TestScheduler(
        policy(active_start=time(0), active_end=time(0)),
        InMemorySchedulerStore(),
        MutableClock(NOW),
        lambda _, timeout: AgentDecision.NO_ACTION,
    )
    assert scheduler.run((event("all-day"),)).outcome is SchedulerOutcome.NO_ACTION
    with pytest.raises(TypeError, match="pending"):
        scheduler.run([event("bad")])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="coalescing bound"):
        scheduler.run(tuple(event(f"event-{index}") for index in range(1025)))


def test_scheduler_never_runs_or_discards_future_wakeups() -> None:
    store = InMemorySchedulerStore()
    clock = MutableClock(NOW)
    calls: list[str] = []
    scheduler = TestScheduler(
        policy(),
        store,
        clock,
        lambda wakeup, timeout: calls.append(wakeup.event_id)
        or AgentDecision.NO_ACTION,
    )

    future = event("future", NOW + timedelta(hours=1))
    assert scheduler.run((future,)).outcome is SchedulerOutcome.NOTHING_DUE
    assert store.load().processed_event_ids == ()

    due = event("due", NOW - timedelta(minutes=1))
    mixed = scheduler.run((due, future))
    assert mixed.outcome is SchedulerOutcome.NO_ACTION
    assert mixed.selected_event_id == "due"
    assert mixed.coalesced_count == 0
    assert calls == ["due"]
    assert store.load().processed_event_ids == ("due",)

    clock.advance(timedelta(hours=1, minutes=1))
    later = scheduler.run((due, future))
    assert later.selected_event_id == "future"
    assert calls == ["due", "future"]


def test_rejected_decisions_do_not_refresh_rate_limit_window() -> None:
    store = InMemorySchedulerStore()
    clock = MutableClock(NOW)
    scheduler = TestScheduler(
        policy(), store, clock, lambda _, timeout: AgentDecision.RESEARCH
    )

    assert scheduler.run((event("accepted"),)).outcome is SchedulerOutcome.PROPOSED
    clock.advance(timedelta(minutes=11))
    assert (
        scheduler.run((event("limited", clock.now()),)).outcome
        is SchedulerOutcome.RATE_LIMITED
    )
    assert len(store.load().action_history) == 1

    clock.advance(timedelta(minutes=50))
    assert (
        scheduler.run((event("eligible-again", clock.now()),)).outcome
        is SchedulerOutcome.PROPOSED
    )
