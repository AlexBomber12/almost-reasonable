"""Deterministic autonomous wake-up decisions; no permanent scheduler or effects."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
import math
from typing import Protocol

from app.core.agent_proposals import AgentDecision


SCHEDULER_VERSION = "behavior-scheduler-v1"
MAX_PENDING_WAKEUPS = 1024
MAX_PROCESSED_EVENTS = 4096
MAX_ACTION_HISTORY = 4096
MAX_WAKEUP_EVENT_ID_UTF8_BYTES = 256
MAX_WAKEUP_TRIGGER_UTF8_BYTES = 1024


class SchedulerOutcome(StrEnum):
    PROPOSED = "proposed"
    NO_ACTION = "no_action"
    NOTHING_DUE = "nothing_due"
    DUPLICATE = "duplicate"
    OUTSIDE_ACTIVE_HOURS = "outside_active_hours"
    COOLDOWN = "cooldown"
    SKIPPED_STOP = "skipped_stop"
    SKIPPED_SPEND = "skipped_spend"
    RATE_LIMITED = "rate_limited"
    EXECUTION_LIMIT = "execution_limit"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WakeupPolicy:
    active_start: time
    active_end: time
    cadence: timedelta
    cooldown: timedelta
    duplicate_window: timedelta
    max_iterations: int
    max_execution_seconds: float
    per_action_limits: tuple[tuple[AgentDecision, int], ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.active_start, time)
            or not isinstance(self.active_end, time)
            or self.active_start.tzinfo is not None
            or self.active_end.tzinfo is not None
        ):
            raise ValueError("active hours must be naive time values")
        if type(self.max_iterations) is not int or self.max_iterations < 1:
            raise ValueError("max_iterations must be positive")
        if (
            type(self.max_execution_seconds) not in (int, float)
            or not _is_finite_number(self.max_execution_seconds)
            or self.max_execution_seconds <= 0
        ):
            raise ValueError("max_execution_seconds must be positive")
        if self.cadence <= timedelta(0) or self.cooldown < timedelta(0):
            raise ValueError("cadence and cooldown are invalid")
        if self.duplicate_window <= timedelta(0):
            raise ValueError("duplicate_window must be positive")
        if type(self.per_action_limits) is not tuple:
            raise ValueError("per_action_limits are invalid")
        decisions = [item[0] for item in self.per_action_limits]
        if (
            any(
                not isinstance(decision, AgentDecision)
                or type(limit) is not int
                or limit < 1
                for decision, limit in self.per_action_limits
            )
            or len(decisions) != len(set(decisions))
            or sum(limit for _, limit in self.per_action_limits) > MAX_ACTION_HISTORY
        ):
            raise ValueError("per_action_limits are invalid")


@dataclass(frozen=True, slots=True)
class WakeupEvent:
    event_id: str
    scheduled_at: datetime
    trigger: str

    def __post_init__(self) -> None:
        if not _valid_event_text(
            self.event_id, MAX_WAKEUP_EVENT_ID_UTF8_BYTES
        ) or not _valid_event_text(self.trigger, MAX_WAKEUP_TRIGGER_UTF8_BYTES):
            raise ValueError(
                "event_id and trigger must be non-empty bounded UTF-8 text"
            )
        if self.scheduled_at.tzinfo is None:
            raise ValueError("scheduled_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SchedulerState:
    processed_events: tuple[tuple[str, datetime], ...] = ()
    last_deliberation_at: datetime | None = None
    action_history: tuple[tuple[AgentDecision, datetime], ...] = ()

    @property
    def processed_event_ids(self) -> tuple[str, ...]:
        return tuple(item[0] for item in self.processed_events)


class SchedulerStore(Protocol):
    def load(self) -> SchedulerState: ...

    def save(self, state: SchedulerState) -> None: ...


class InMemorySchedulerStore:
    """Small fake persistence adapter used by deterministic tests/integration."""

    def __init__(self, state: SchedulerState | None = None):
        self._state = state or SchedulerState()

    def load(self) -> SchedulerState:
        return self._state

    def save(self, state: SchedulerState) -> None:
        self._state = state


class Clock(Protocol):
    def now(self) -> datetime: ...


class MutableClock:
    def __init__(self, current: datetime):
        if current.tzinfo is None:
            raise ValueError("clock must be timezone-aware")
        self._current = current

    def now(self) -> datetime:
        return self._current

    def advance(self, duration: timedelta) -> None:
        self._current += duration


@dataclass(frozen=True, slots=True)
class SchedulerRun:
    outcome: SchedulerOutcome
    selected_event_id: str | None
    coalesced_count: int
    iterations: int
    decision: AgentDecision | None


class TestScheduler:
    """Single-run scheduler harness with injected clock, store, and fake model."""

    __test__ = False

    def __init__(
        self,
        policy: WakeupPolicy,
        store: SchedulerStore,
        clock: Clock,
        deliberate: Callable[[WakeupEvent, float], AgentDecision],
    ):
        self._policy = policy
        self._store = store
        self._clock = clock
        self._deliberate = deliberate

    def run(
        self,
        pending: tuple[WakeupEvent, ...],
        *,
        emergency_stop: bool = False,
        spend_suspended: bool = False,
    ) -> SchedulerRun:
        if type(pending) is not tuple or any(
            not isinstance(item, WakeupEvent) for item in pending
        ):
            raise TypeError("pending must contain WakeupEvent values")
        if len(pending) > MAX_PENDING_WAKEUPS:
            raise ValueError("pending wake-ups exceed the coalescing bound")
        if not pending:
            return SchedulerRun(SchedulerOutcome.NOTHING_DUE, None, 0, 0, None)

        now = self._clock.now()
        now_instant = _instant(now)
        state = self._pruned_state(self._store.load(), now)
        due = tuple(
            item for item in pending if _instant(item.scheduled_at) <= now_instant
        )
        if not due:
            self._store.save(state)
            return SchedulerRun(SchedulerOutcome.NOTHING_DUE, None, 0, 0, None)
        processed = set(state.processed_event_ids)
        unprocessed_due = tuple(item for item in due if item.event_id not in processed)
        if not unprocessed_due:
            selected = max(due, key=lambda item: _instant(item.scheduled_at))
            self._store.save(state)
            return SchedulerRun(
                SchedulerOutcome.DUPLICATE,
                selected.event_id,
                len(due) - 1,
                0,
                None,
            )
        selected = max(unprocessed_due, key=lambda item: _instant(item.scheduled_at))
        coalesced = len(unprocessed_due) - 1

        if state.last_deliberation_at is not None and now_instant < _instant(
            state.last_deliberation_at
        ):
            return SchedulerRun(
                SchedulerOutcome.EXECUTION_LIMIT,
                selected.event_id,
                coalesced,
                0,
                None,
            )
        if emergency_stop:
            return self._skip(
                state,
                unprocessed_due,
                selected,
                coalesced,
                now,
                SchedulerOutcome.SKIPPED_STOP,
            )
        if spend_suspended:
            return self._skip(
                state,
                unprocessed_due,
                selected,
                coalesced,
                now,
                SchedulerOutcome.SKIPPED_SPEND,
            )
        if not _inside_active_hours(now.timetz().replace(tzinfo=None), self._policy):
            return self._skip(
                state,
                unprocessed_due,
                selected,
                coalesced,
                now,
                SchedulerOutcome.OUTSIDE_ACTIVE_HOURS,
            )
        if (
            state.last_deliberation_at is not None
            and now_instant - _instant(state.last_deliberation_at)
            < self._policy.cooldown
        ):
            return self._skip(
                state,
                unprocessed_due,
                selected,
                coalesced,
                now,
                SchedulerOutcome.COOLDOWN,
            )

        started_at = now
        try:
            decision = self._deliberate(selected, self._policy.max_execution_seconds)
        except TimeoutError:
            finished_at = self._clock.now()
            outcome = SchedulerOutcome.EXECUTION_LIMIT
            valid_decision = None
        except (OSError, ValueError):
            finished_at = self._clock.now()
            outcome = (
                SchedulerOutcome.EXECUTION_LIMIT
                if _execution_limit_exceeded(
                    started_at, finished_at, self._policy.max_execution_seconds
                )
                else SchedulerOutcome.FAILED
            )
            valid_decision = None
        else:
            finished_at = self._clock.now()
            if _execution_limit_exceeded(
                started_at, finished_at, self._policy.max_execution_seconds
            ):
                outcome = SchedulerOutcome.EXECUTION_LIMIT
                valid_decision = None
            elif not isinstance(decision, AgentDecision):
                outcome = SchedulerOutcome.FAILED
                valid_decision = None
            elif self._rate_limited(state, decision, finished_at):
                outcome = SchedulerOutcome.RATE_LIMITED
                valid_decision = decision
            else:
                outcome = (
                    SchedulerOutcome.NO_ACTION
                    if decision is AgentDecision.NO_ACTION
                    else SchedulerOutcome.PROPOSED
                )
                valid_decision = decision

        recorded_at = max((started_at, finished_at), key=_instant)
        state = self._pruned_state(state, recorded_at)
        history = state.action_history
        if outcome in (
            SchedulerOutcome.PROPOSED,
            SchedulerOutcome.NO_ACTION,
        ) and valid_decision in dict(self._policy.per_action_limits):
            history = (*history, (valid_decision, finished_at))
        updated = replace(
            state,
            processed_events=_append_processed(state, unprocessed_due, recorded_at),
            last_deliberation_at=recorded_at,
            action_history=history,
        )
        self._store.save(updated)
        return SchedulerRun(outcome, selected.event_id, coalesced, 1, valid_decision)

    def _skip(
        self,
        state: SchedulerState,
        pending: tuple[WakeupEvent, ...],
        selected: WakeupEvent,
        coalesced: int,
        now: datetime,
        outcome: SchedulerOutcome,
    ) -> SchedulerRun:
        self._store.save(
            replace(state, processed_events=_append_processed(state, pending, now))
        )
        return SchedulerRun(outcome, selected.event_id, coalesced, 0, None)

    def _pruned_state(self, state: SchedulerState, now: datetime) -> SchedulerState:
        duplicate_cutoff = _retention_cutoff(now, self._policy.duplicate_window)
        action_cutoff = _retention_cutoff(now, self._policy.cadence)
        limited_decisions = dict(self._policy.per_action_limits)
        processed_events = tuple(
            item
            for item in state.processed_events
            if _instant(item[1]) > duplicate_cutoff
        )[-MAX_PROCESSED_EVENTS:]
        return replace(
            state,
            processed_events=processed_events,
            action_history=tuple(
                item
                for item in state.action_history
                if item[0] in limited_decisions and _instant(item[1]) > action_cutoff
            )[-MAX_ACTION_HISTORY:],
        )

    def _rate_limited(
        self, state: SchedulerState, decision: AgentDecision, now: datetime
    ) -> bool:
        limits = dict(self._policy.per_action_limits)
        limit = limits.get(decision)
        cutoff = _retention_cutoff(now, self._policy.cadence)
        return (
            limit is not None
            and sum(
                prior is decision and _instant(recorded_at) > cutoff
                for prior, recorded_at in state.action_history
            )
            >= limit
        )


def _is_finite_number(value: int | float) -> bool:
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _execution_limit_exceeded(
    started_at: datetime, finished_at: datetime, max_seconds: float
) -> bool:
    return (
        _instant(finished_at) < _instant(started_at)
        or (_instant(finished_at) - _instant(started_at)).total_seconds() > max_seconds
    )


def _valid_event_text(value: object, max_bytes: int) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= max_bytes
    except UnicodeEncodeError:
        return False


def _append_processed(
    state: SchedulerState,
    pending: tuple[WakeupEvent, ...],
    now: datetime,
) -> tuple[tuple[str, datetime], ...]:
    known = set(state.processed_event_ids)
    additions = []
    for item in pending:
        if item.event_id not in known:
            additions.append((item.event_id, now))
            known.add(item.event_id)
    return (*state.processed_events, *additions)[-MAX_PROCESSED_EVENTS:]


def _retention_cutoff(now: datetime, duration: timedelta) -> datetime:
    now_instant = _instant(now)
    try:
        return now_instant - duration
    except OverflowError:
        return datetime.min.replace(tzinfo=UTC)


def _instant(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _inside_active_hours(current: time, policy: WakeupPolicy) -> bool:
    if policy.active_start == policy.active_end:
        return True
    if policy.active_start < policy.active_end:
        return policy.active_start <= current < policy.active_end
    return current >= policy.active_start or current < policy.active_end
