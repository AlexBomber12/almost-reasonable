"""Effect-free durable wake-ups in a dedicated SQLite database.

A committed ``started`` marker is an at-most-once fence, not proof of success.
Expired started work is uncertain and is never automatically called again.
"""

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Protocol
from uuid import uuid4

from app.behavior.scheduler import (
    MAX_PENDING_WAKEUPS,
    MAX_PROCESSED_EVENTS,
    Clock,
    InMemorySchedulerStore,
    SchedulerOutcome,
    SchedulerRun,
    SchedulerState,
    TestScheduler,
    WakeupEvent,
    WakeupPolicy,
    _instant,
    _retention_cutoff,
)
from app.core.agent_proposals import AgentDecision


class StopSource(Protocol):
    def active(self) -> bool: ...


class SpendSource(Protocol):
    def is_spend_suspended(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class WakeupClaim:
    token: str
    events: tuple[WakeupEvent, ...]
    state: SchedulerState
    cadence: timedelta | None


class SQLiteSchedulerRepository:
    """One scheduler lane per database; separate connections serialize writers.

    Use a private application-owned path, never the EventStore database. WAL and
    FULL synchronous commits protect the claim fence. No connection survives a
    method call and no transaction spans deliberation. Event metadata is trusted
    application input and must contain no participant text or credentials.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS scheduler_schema(version INTEGER)")
            versions = db.execute("SELECT version FROM scheduler_schema").fetchall()
            if versions and versions != [(1,)]:
                raise ValueError("unsupported scheduler schema")
            if not versions:
                db.execute("INSERT INTO scheduler_schema VALUES(1)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS scheduler_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL, cadence TEXT, watermark TEXT)"
            )
            db.execute(
                "INSERT OR IGNORE INTO scheduler_state VALUES(1, ?, NULL, NULL)",
                (_encode_state(SchedulerState()),),
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS scheduler_agent_cadence(operation TEXT PRIMARY KEY,seconds INTEGER NOT NULL,at TEXT NOT NULL,active INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS scheduler_events(id TEXT PRIMARY KEY, scheduled TEXT NOT NULL, trigger TEXT NOT NULL, status TEXT NOT NULL, recorded TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS scheduler_claim(singleton INTEGER PRIMARY KEY CHECK(singleton=1), token TEXT NOT NULL, phase TEXT NOT NULL, expires TEXT NOT NULL, started TEXT, ids TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS scheduler_outcomes(sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
            )

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if tables and "scheduler_schema" not in tables:
                raise ValueError("database belongs to another component")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def load(self) -> SchedulerState:
        with self._transaction() as db:
            return _decode_state(
                db.execute("SELECT payload FROM scheduler_state").fetchone()[0]
            )

    def select_cadence(self, cadence: timedelta) -> None:
        """Application accepts an agent-selected interval, never an activity quota."""
        if not isinstance(cadence, timedelta) or cadence <= timedelta(0):
            raise ValueError("cadence must be positive")
        microseconds = (
            cadence.days * 86400 + cadence.seconds
        ) * 1000000 + cadence.microseconds
        with self._transaction() as db:
            db.execute("UPDATE scheduler_state SET cadence=?", (str(microseconds),))
            db.execute("UPDATE scheduler_agent_cadence SET active=0")

    def select_agent_cadence(self, operation, seconds, at):
        from app.threads.domain import require_reference, require_aware_time

        require_reference("cadence operation", operation)
        require_aware_time("cadence decision", at)
        if type(seconds) is not int or not 1 <= seconds <= 86400:
            raise ValueError("invalid bounded cadence")
        decided = at.astimezone(UTC).isoformat()
        with self._transaction() as db:
            if db.execute(
                "SELECT 1 FROM scheduler_agent_cadence WHERE operation=?", (operation,)
            ).fetchone():
                return False
            latest = db.execute(
                "SELECT MAX(at) FROM scheduler_agent_cadence"
            ).fetchone()[0]
            if latest is not None and decided <= latest:
                return False
            if (
                db.execute("SELECT COUNT(*) FROM scheduler_agent_cadence").fetchone()[0]
                >= MAX_PROCESSED_EVENTS
            ):
                raise ValueError("cadence journal capacity reached")
            db.execute("UPDATE scheduler_agent_cadence SET active=0")
            db.execute(
                "INSERT INTO scheduler_agent_cadence VALUES(?,?,?,1)",
                (operation, seconds, decided),
            )
            db.execute(
                "UPDATE scheduler_state SET cadence=?", (str(seconds * 1000000),)
            )
        return True

    def active_agent_cadence(self):
        with self._transaction() as db:
            row = db.execute(
                "SELECT operation FROM scheduler_agent_cadence WHERE active=1"
            ).fetchone()
        return None if row is None else row[0]

    def current_cadence(self, default: timedelta) -> timedelta:
        with self._transaction() as db:
            value = db.execute("SELECT cadence FROM scheduler_state").fetchone()[0]
        return default if value is None else timedelta(microseconds=int(value))

    def outcomes(self) -> tuple[SchedulerRun, ...]:
        with self._transaction() as db:
            return tuple(
                _decode_run(row[0])
                for row in db.execute(
                    "SELECT payload FROM scheduler_outcomes ORDER BY sequence"
                )
            )

    def claim(
        self,
        pending: tuple[WakeupEvent, ...],
        now: datetime,
        lease: timedelta,
        duplicate_window: timedelta = timedelta(days=1),
    ) -> WakeupClaim | SchedulerRun:
        if type(pending) is not tuple or any(
            not isinstance(item, WakeupEvent) for item in pending
        ):
            raise TypeError("pending must contain WakeupEvent values")
        if len(pending) > MAX_PENDING_WAKEUPS:
            raise ValueError("pending wake-ups exceed the coalescing bound")
        if lease <= timedelta(0):
            raise ValueError("lease must be positive")
        now_instant = _instant(now)
        timestamp = now_instant.isoformat()
        with self._transaction() as db:
            payload, cadence, watermark = db.execute(
                "SELECT payload, cadence, watermark FROM scheduler_state"
            ).fetchone()
            state = _decode_state(payload)
            db.execute(
                "DELETE FROM scheduler_events WHERE status='done' AND recorded<=?",
                (_retention_cutoff(now, duplicate_window).isoformat(),),
            )
            if watermark is not None and timestamp < watermark:
                self._admit(db, pending)
                return _empty(SchedulerOutcome.EXECUTION_LIMIT)
            db.execute("UPDATE scheduler_state SET watermark=?", (timestamp,))
            active = db.execute(
                "SELECT token, phase, expires, started, ids FROM scheduler_claim"
            ).fetchone()
            busy = active is not None and active[2] > timestamp
            if active is not None and not busy:
                ids = json.loads(active[4])
                if active[1] == "started":
                    # A provider may have been called, even if no result survived.
                    selected_id = db.execute(
                        "SELECT id FROM scheduler_events WHERE status='claimed' ORDER BY scheduled DESC, id LIMIT 1"
                    ).fetchone()[0]
                    db.executemany(
                        "UPDATE scheduler_events SET status='uncertain', recorded=? WHERE id=?",
                        ((timestamp, identity) for identity in ids),
                    )
                    state = replace(
                        state, last_deliberation_at=datetime.fromisoformat(active[3])
                    )
                    db.execute(
                        "UPDATE scheduler_state SET payload=?", (_encode_state(state),)
                    )
                    self._record(
                        db,
                        SchedulerRun(
                            SchedulerOutcome.FAILED,
                            selected_id,
                            len(ids) - 1,
                            1,
                            None,
                        ),
                    )
                else:
                    db.executemany(
                        "UPDATE scheduler_events SET status='pending' WHERE id=?",
                        ((identity,) for identity in ids),
                    )
                db.execute("DELETE FROM scheduler_claim")
            self._admit(db, pending)
            if busy:
                return _empty(SchedulerOutcome.EXECUTION_LIMIT)
            rows = db.execute(
                "SELECT id, scheduled, trigger FROM scheduler_events WHERE status='pending' AND scheduled<=? ORDER BY scheduled, id",
                (timestamp,),
            ).fetchall()
            if not rows:
                due_duplicates = tuple(
                    item
                    for item in pending
                    if _instant(item.scheduled_at) <= _instant(now)
                )
                if due_duplicates:
                    selected = max(
                        due_duplicates, key=lambda item: _instant(item.scheduled_at)
                    )
                    return SchedulerRun(
                        SchedulerOutcome.DUPLICATE,
                        selected.event_id,
                        len(set(item.event_id for item in due_duplicates)) - 1,
                        0,
                        None,
                    )
                return _empty(SchedulerOutcome.NOTHING_DUE)
            events = tuple(
                WakeupEvent(row[0], datetime.fromisoformat(row[1]), row[2])
                for row in rows
            )
            token = uuid4().hex
            db.execute(
                "INSERT INTO scheduler_claim VALUES(1,?,'claimed',?,NULL,?)",
                (
                    token,
                    (
                        now_instant
                        + min(lease, datetime.max.replace(tzinfo=UTC) - now_instant)
                    ).isoformat(),
                    json.dumps([item.event_id for item in events]),
                ),
            )
            db.executemany(
                "UPDATE scheduler_events SET status='claimed' WHERE id=?",
                ((item.event_id,) for item in events),
            )
            return WakeupClaim(
                token,
                events,
                state,
                None if cadence is None else timedelta(microseconds=int(cadence)),
            )

    @staticmethod
    def _admit(db: sqlite3.Connection, pending: tuple[WakeupEvent, ...]) -> None:
        for item in pending:
            db.execute(
                "INSERT OR IGNORE INTO scheduler_events VALUES(?,?,?,'pending',NULL)",
                (
                    item.event_id,
                    _instant(item.scheduled_at).isoformat(),
                    item.trigger,
                ),
            )
        if (
            db.execute(
                "SELECT count(*) FROM scheduler_events WHERE status IN ('pending','claimed')"
            ).fetchone()[0]
            > MAX_PENDING_WAKEUPS
        ):
            raise ValueError("pending wake-ups exceed the coalescing bound")

    def begin(self, claim: WakeupClaim, now: datetime) -> bool:
        timestamp = _instant(now).isoformat()
        with self._transaction() as db:
            accepted = (
                db.execute(
                    "UPDATE scheduler_claim SET phase='started', started=? WHERE token=? AND phase='claimed' AND expires>? AND (SELECT watermark FROM scheduler_state)<=?",
                    (timestamp, claim.token, timestamp, timestamp),
                ).rowcount
                == 1
            )
            if accepted:
                db.execute("UPDATE scheduler_state SET watermark=?", (timestamp,))
            return accepted

    def complete(
        self,
        claim: WakeupClaim,
        state: SchedulerState,
        result: SchedulerRun,
        now: datetime,
    ) -> bool:
        timestamp = _instant(now).isoformat()
        with self._transaction() as db:
            active = db.execute(
                "SELECT expires, phase, ids FROM scheduler_claim WHERE token=?",
                (claim.token,),
            ).fetchone()
            if active is None or active[0] <= timestamp:
                return False
            if json.loads(active[2]) != [item.event_id for item in claim.events] or (
                (
                    result.iterations > 0
                    or result.outcome
                    in (SchedulerOutcome.PROPOSED, SchedulerOutcome.NO_ACTION)
                )
                and active[1] != "started"
            ):
                return False
            watermark = db.execute("SELECT watermark FROM scheduler_state").fetchone()[
                0
            ]
            if (
                result.outcome
                in (SchedulerOutcome.PROPOSED, SchedulerOutcome.NO_ACTION)
                and timestamp < watermark
            ):
                return False
            recorded = max(timestamp, watermark)
            if state.last_deliberation_at is not None:
                recorded = max(
                    recorded, _instant(state.last_deliberation_at).isoformat()
                )
            if active[1] == "started":
                state = replace(
                    state, last_deliberation_at=datetime.fromisoformat(recorded)
                )
            db.execute(
                "UPDATE scheduler_state SET payload=?, watermark=?",
                (_encode_state(state), recorded),
            )
            db.executemany(
                "UPDATE scheduler_events SET status='done', recorded=? WHERE id=?",
                ((recorded, item.event_id) for item in claim.events),
            )
            db.execute(
                "DELETE FROM scheduler_events WHERE status='done' AND id NOT IN (SELECT id FROM scheduler_events WHERE status='done' ORDER BY recorded DESC, rowid DESC LIMIT ?)",
                (MAX_PROCESSED_EVENTS,),
            )
            self._record(db, result)
            db.execute("DELETE FROM scheduler_claim WHERE token=?", (claim.token,))
            return True

    @staticmethod
    def _record(db: sqlite3.Connection, result: SchedulerRun) -> None:
        db.execute(
            "INSERT INTO scheduler_outcomes(payload) VALUES(?)",
            (
                json.dumps(
                    [
                        result.outcome,
                        result.selected_event_id,
                        result.coalesced_count,
                        result.iterations,
                        result.decision,
                    ]
                ),
            ),
        )
        db.execute(
            "DELETE FROM scheduler_outcomes WHERE sequence NOT IN (SELECT sequence FROM scheduler_outcomes ORDER BY sequence DESC LIMIT 4096)"
        )


class _GuardChanged(Exception):
    def __init__(self, stop: bool, spend: bool):
        self.stop, self.spend = stop, spend


class _ClaimLost(Exception):
    pass


class DurableScheduler:
    """Application composition, without a daemon, dispatch or mission mutation.

    Compose owner=OwnerStore and provider=ProviderStateStore. Deliberation must
    enforce the supplied timeout and return only a decision; it has no effect
    grant. Unknown/programmer exceptions leave started work uncertain.
    """

    def __init__(
        self,
        policy: WakeupPolicy,
        repository: SQLiteSchedulerRepository,
        clock: Clock,
        deliberate: Callable[[WakeupEvent, float], AgentDecision],
        *,
        owner: StopSource,
        provider: SpendSource,
    ):
        self.policy, self.repository, self.clock = policy, repository, clock
        self.deliberate, self.owner, self.provider = deliberate, owner, provider

    def _guards(self) -> tuple[bool, bool]:
        stop, spend = self.owner.active(), self.provider.is_spend_suspended()
        if type(stop) is not bool or type(spend) is not bool:
            raise TypeError("control sources must return booleans")
        return stop, spend

    def run(self, pending: tuple[WakeupEvent, ...] = ()) -> SchedulerRun:
        stop, spend = self._guards()
        claim = self.repository.claim(
            pending,
            self.clock.now(),
            _execution_lease(self.policy.max_execution_seconds),
            self.policy.duplicate_window,
        )
        if isinstance(claim, SchedulerRun):
            return claim
        policy = (
            self.policy
            if claim.cadence is None
            else replace(self.policy, cadence=claim.cadence)
        )
        memory = InMemorySchedulerStore(claim.state)

        def deliberate(event: WakeupEvent, timeout: float) -> AgentDecision:
            current_stop, current_spend = self._guards()
            if current_stop or current_spend:
                raise _GuardChanged(current_stop, current_spend)
            if not self.repository.begin(claim, self.clock.now()):
                raise _ClaimLost
            return self.deliberate(event, timeout)

        harness = TestScheduler(policy, memory, self.clock, deliberate)
        try:
            result = harness.run(
                claim.events, emergency_stop=stop, spend_suspended=spend
            )
        except _GuardChanged as changed:
            result = harness.run(
                claim.events, emergency_stop=changed.stop, spend_suspended=changed.spend
            )
        except _ClaimLost:
            return _empty(SchedulerOutcome.EXECUTION_LIMIT)
        current_stop, current_spend = self._guards()
        if result.outcome in (
            SchedulerOutcome.PROPOSED,
            SchedulerOutcome.NO_ACTION,
        ) and (current_stop or current_spend):
            result = replace(
                result,
                outcome=SchedulerOutcome.SKIPPED_STOP
                if current_stop
                else SchedulerOutcome.SKIPPED_SPEND,
                decision=None,
            )
            memory.save(
                replace(memory.load(), action_history=claim.state.action_history)
            )
        if not self.repository.complete(claim, memory.load(), result, self.clock.now()):
            return _empty(SchedulerOutcome.EXECUTION_LIMIT)
        return result


def _execution_lease(max_seconds: int | float) -> timedelta:
    # Preserve the accepted timeout and grace rather than imposing a new cap.
    # Saturation only applies beyond datetime's representable clock domain.
    try:
        return timedelta(seconds=max_seconds) + timedelta(seconds=5)
    except OverflowError:
        return timedelta.max


def _empty(outcome: SchedulerOutcome) -> SchedulerRun:
    return SchedulerRun(outcome, None, 0, 0, None)


def _encode_state(state: SchedulerState) -> str:
    return json.dumps(
        {
            "processed": [
                (identity, _instant(at).isoformat())
                for identity, at in state.processed_events
            ],
            "last": None
            if state.last_deliberation_at is None
            else _instant(state.last_deliberation_at).isoformat(),
            "history": [
                (decision, _instant(at).isoformat())
                for decision, at in state.action_history
            ],
        }
    )


def _decode_state(payload: str) -> SchedulerState:
    data = json.loads(payload)
    return SchedulerState(
        tuple(
            (identity, datetime.fromisoformat(at)) for identity, at in data["processed"]
        ),
        None if data["last"] is None else datetime.fromisoformat(data["last"]),
        tuple(
            (AgentDecision(decision), datetime.fromisoformat(at))
            for decision, at in data["history"]
        ),
    )


def _decode_run(payload: str) -> SchedulerRun:
    outcome, identity, coalesced, iterations, decision = json.loads(payload)
    return SchedulerRun(
        SchedulerOutcome(outcome),
        identity,
        coalesced,
        iterations,
        None if decision is None else AgentDecision(decision),
    )
