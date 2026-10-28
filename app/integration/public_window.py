"""Bounded supervision of the existing public runtime; no publication authority."""

from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json
import sqlite3
import time

from app.behavior.scheduler import WakeupEvent
from app.integration.threads_stage0 import MAX_INTENTS, _identity
from app.threads.domain import InboundKind, OutboxState, require_reference
from app.threads.service import PolicyDeniedError, PollingError
from app.threads.simulator import ProviderFailure
from app.threads.store import ThreadsStoreError


@dataclass(frozen=True, slots=True)
class WindowSpec:
    window_id: str
    expires_at: datetime
    max_model_calls: int
    max_poll_pages: int
    max_inbound_events: int
    poll_interval_seconds: int
    page_size: int = 10
    mission_evolution: bool = False

    def __post_init__(self):
        require_reference("operating window", self.window_id)
        if type(self.mission_evolution) is not bool:
            raise ValueError("invalid window mission mode")
        if (
            not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("window expiry must be timezone-aware")
        for value, minimum, maximum in (
            (self.max_model_calls, 1, MAX_INTENTS),
            (self.max_poll_pages, 0, MAX_INTENTS),
            (self.max_inbound_events, 0, MAX_INTENTS),
            (self.poll_interval_seconds, 1, 3600),
            (self.page_size, 1, 10),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("invalid finite window limit")

    def encoded(self):
        value = asdict(self) | {
            "expires_at": self.expires_at.astimezone(UTC).isoformat()
        }
        # Preserve the encoding of existing fixed-mission windows.
        if not self.mission_evolution:
            del value["mission_evolution"]
        return json.dumps(value, sort_keys=True)

    def maximum_work(self, *, research_limit=0):
        return {
            "model_calls": self.max_model_calls,
            "poll_pages": self.max_poll_pages,
            "poll_gets": self.max_poll_pages,
            "fetched_events": self.max_poll_pages * self.page_size,
            "inbound_deliberations": min(self.max_inbound_events, self.max_model_calls),
            "research_operations_separate_authority": min(
                self.max_model_calls, research_limit
            ),
            "publication_posts": 0,
        }


class WindowDenied(PolicyDeniedError):
    pass


class PublicOperatingWindow:
    """One single-host runner, using runtime claims, polling and worker fences.

    Budget reservation and the existing started intent share one transaction.
    Tick/poll admission commits before effects. No retry or lease takeover is
    needed: the journal's existing OS lock dies with the process, while all
    admitted identities and consumption survive it. No SQLite transaction spans
    a worker or HTTP call.
    """

    def __init__(self, app, spec):
        self.app, self.spec = app, spec
        if app.mission is None:
            raise ValueError("reviewed public mission is required")
        if spec.mission_evolution != (app.mission_evolution is not None):
            raise ValueError(
                "window mission mode requires matching runtime (fixed or evolving)"
            )

    def _initialize(self):
        now = self.app.clock.now().astimezone(UTC)
        with self.app.journal.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS public_windows("
                "id TEXT PRIMARY KEY, spec TEXT NOT NULL, controls TEXT NOT NULL, "
                "last_seen TEXT NOT NULL, next_wake TEXT NOT NULL, next_poll TEXT NOT NULL, "
                "ticks INTEGER NOT NULL, polls INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS public_window_work("
                "window TEXT NOT NULL, operation TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                "outcome TEXT NOT NULL)"
            )
            row = db.execute(
                "SELECT spec FROM public_windows WHERE id=?", (self.spec.window_id,)
            ).fetchone()
            if row is not None:
                if row[0] != self.spec.encoded():
                    raise ValueError("operating window configuration changed")
                self.snapshot = dict(self._row(db))
                return
            if self.spec.expires_at - now > timedelta(days=1):
                raise ValueError("operating window cannot exceed 24 hours")
            if (
                db.execute("SELECT COUNT(*) FROM public_windows").fetchone()[0]
                >= MAX_INTENTS
            ):
                raise ValueError("operating window journal capacity reached")
            controls = {
                "generation": self.app.ledger.control_generation(),
                "mission": self.app.mission.snapshot()[1],
                "research": self._research_binding(),
            }
            if self.spec.mission_evolution:
                controls["root"] = self.app.mission._root_snapshot()[1]
            db.execute(
                "INSERT INTO public_windows VALUES(?,?,?,?,?,?,0,0)",
                (
                    self.spec.window_id,
                    self.spec.encoded(),
                    json.dumps(controls),
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self.snapshot = dict(self._row(db))

    def _row(self, db):
        return db.execute(
            "SELECT * FROM public_windows WHERE id=?", (self.spec.window_id,)
        ).fetchone()

    def _research_binding(self):
        profile = self.app.current_research_profile()
        if self.app.research_enabled and profile is None:
            raise WindowDenied("controls_changed")
        return None if profile is None else profile.digest

    def _check(self, row):
        now = self.app.clock.now().astimezone(UTC)
        observed = max(now, datetime.fromisoformat(row["last_seen"]))
        if observed >= self.spec.expires_at:
            self.snapshot["last_seen"] = max(
                self.snapshot["last_seen"], observed.isoformat()
            )
            raise WindowDenied("expired")
        if now.isoformat() < row["last_seen"]:
            raise WindowDenied("clock_regressed")
        controls = json.loads(row["controls"])
        if self.app.ledger.control_generation() != controls["generation"]:
            raise WindowDenied("controls_changed")
        if self._research_binding() != controls["research"]:
            raise WindowDenied("controls_changed")
        try:
            if self.spec.mission_evolution and (
                self.app.mission._root_snapshot()[1] != controls["root"]
            ):
                raise ValueError("window mission root changed")
            self.app.mission.require(controls["mission"])
        except ValueError as error:
            raise WindowDenied("controls_changed") from error
        return now

    def guard(self):
        # The runner owns the journal's process lock. Authority stays bounded;
        # live mission/privacy checks still run here, including inside research
        # retention transactions. Persist the watermark at each admission and
        # loop boundary, without opening a nested journal write transaction.
        # Read committed bindings even after a transition committed but its
        # caller crashed. Never derive authority from the live mission alone.
        with closing(sqlite3.connect(self.app.journal.path)) as db:
            controls = db.execute(
                "SELECT controls FROM public_windows WHERE id=?",
                (self.spec.window_id,),
            ).fetchone()[0]
        now = self._check(self.snapshot | {"controls": controls})
        self.snapshot["last_seen"] = now.isoformat()

    def bind_transition(self, db, operation, payload, current):
        """Commit only this window's admitted transition alongside its intent."""
        row = self._row(db)
        self._check(row)
        admitted = db.execute(
            "SELECT 1 FROM public_window_work WHERE window=? AND operation=? "
            "AND kind IN ('wake','reply','mention')",
            (self.spec.window_id, operation),
        ).fetchone()
        controls = json.loads(row["controls"])
        if (
            not self.spec.mission_evolution
            or admitted is None
            or payload["mission"] != controls["mission"]
        ):
            raise WindowDenied("controls_changed")
        if current.version != controls["mission"]["version"]:
            controls["mission"] = self.app.mission.binding(current)
        db.execute(
            "UPDATE public_windows SET controls=? WHERE id=?",
            (json.dumps(controls), self.spec.window_id),
        )
        self._watermark(db)

    def _watermark(self, db):
        db.execute(
            "UPDATE public_windows SET last_seen=? WHERE id=?",
            (self.snapshot["last_seen"], self.spec.window_id),
        )

    def _counts(self, db):
        counts = Counter(
            row[0]
            for row in db.execute(
                "SELECT kind FROM public_window_work WHERE window=?",
                (self.spec.window_id,),
            )
        )
        return {
            "model_calls": counts["wake"] + counts["reply"] + counts["mention"],
            "poll_pages": counts["poll"],
            "inbound_events": counts["reply"] + counts["mention"],
        }

    def admit_deliberation(self, db, operation, origin):
        """Called inside Stage0Journal.start, atomically with its intent."""
        self._check(self._row(db))
        counts = self._counts(db)
        if counts["model_calls"] >= self.spec.max_model_calls or (
            origin["kind"] != "wake"
            and counts["inbound_events"] >= self.spec.max_inbound_events
        ):
            return False
        db.execute(
            "INSERT INTO public_window_work VALUES(?,?,?,?)",
            (self.spec.window_id, operation, origin["kind"], "started"),
        )
        self._watermark(db)
        return True

    def operations(self):
        with self.app.journal.transaction() as db:
            return tuple(
                row[0]
                for row in db.execute(
                    "SELECT operation FROM public_window_work WHERE window=? "
                    "AND kind IN ('wake','reply','mention')",
                    (self.spec.window_id,),
                )
            )

    def _due(self, lane):
        self.app.guard()
        # Respect the scheduler's selected cadence; missed intervals coalesce
        # into one offer at now, with the next offer a full interval later.
        cadence = self.app.scheduler.repository.current_cadence(
            self.app.scheduler.policy.cadence
        )
        with self.app.journal.transaction() as db:
            row = self._row(db)
            now = self._check(row)
            counts = self._counts(db)
            if counts["model_calls"] >= self.spec.max_model_calls:
                return None
            if lane == "poll" and counts["poll_pages"] >= self.spec.max_poll_pages:
                return None
            if now.isoformat() < row["next_" + lane]:
                return None
            sequence = row["ticks" if lane == "wake" else "polls"]
            identity = _identity(
                self.app.account, "window", self.spec.window_id, lane, sequence
            )
            interval = (
                cadence
                if lane == "wake"
                else timedelta(seconds=self.spec.poll_interval_seconds)
            )
            next_at = now + min(interval, self.spec.expires_at - now)
            if lane == "wake":
                db.execute(
                    "UPDATE public_windows SET ticks=ticks+1,next_wake=? WHERE id=?",
                    (next_at.isoformat(), self.spec.window_id),
                )
            else:
                db.execute(
                    "UPDATE public_windows SET polls=polls+1,next_poll=? WHERE id=?",
                    (next_at.isoformat(), self.spec.window_id),
                )
            db.execute(
                "INSERT INTO public_window_work VALUES(?,?,?,'started')",
                (self.spec.window_id, identity, "tick" if lane == "wake" else "poll"),
            )
            self._watermark(db)
            return identity, now, sequence

    def _outcome(self, identity, outcome):
        with self.app.journal.transaction() as db:
            db.execute(
                "UPDATE public_window_work SET outcome=? WHERE operation=?",
                (outcome, identity),
            )

    def _cycle(self):
        offered = self._due("wake")
        if offered is not None:
            identity, now, _ = offered
            result = self.app.scheduler.run(
                (WakeupEvent(identity, now, "bounded operating window"),)
            )
            self._outcome(identity, result.outcome.value)
            self.app.recover()
        offered = self._due("poll")
        if offered is not None:
            identity, _, sequence = offered
            streams = (
                (InboundKind.MENTION,)
                if self.app.simulator.reply_thread_id is None
                else (InboundKind.REPLY, InboundKind.MENTION)
            )
            self.app.poll(
                streams[sequence % len(streams)],
                correlation_id=identity,
                max_pages=1,
                max_events=self.spec.page_size,
                timeout_seconds=min(
                    10, (self.spec.expires_at - self.app.clock.now()).total_seconds()
                ),
                deadline_at=self.spec.expires_at,
            )
            self._outcome(identity, "completed")
        for stream in (InboundKind.REPLY, InboundKind.MENTION):
            self.app.guard()
            with self.app.journal.transaction() as db:
                counts = self._counts(db)
            limit = min(
                self.spec.max_model_calls - counts["model_calls"],
                self.spec.max_inbound_events - counts["inbound_events"],
            )
            if limit > 0:
                self.app.process_inbound(stream, limit=limit)

    def run(self, *, wait=time.sleep):
        self.app.operator.authenticate()
        try:
            with self.app.journal.handoff_lock("operating-window", blocking=False):
                self._initialize()
                self.app.operating_window = self
                state = "interrupted"
                try:
                    while True:
                        self.app.guard()
                        self.app.recover()
                        with self.app.journal.transaction() as db:
                            self._watermark(db)
                            if (
                                self._counts(db)["model_calls"]
                                >= self.spec.max_model_calls
                            ):
                                state = "exhausted"
                                break
                        self._cycle()
                        with self.app.journal.transaction() as db:
                            if (
                                self._counts(db)["model_calls"]
                                >= self.spec.max_model_calls
                            ):
                                state = "exhausted"
                                break
                        wait(0.25)
                except WindowDenied as error:
                    state = str(error)
                except PolicyDeniedError:
                    state = "controls_denied"
                except KeyboardInterrupt:
                    state = "interrupted"
                except (
                    OSError,
                    ValueError,
                    PollingError,
                    ProviderFailure,
                    ThreadsStoreError,
                ):
                    state = (
                        "expired"
                        if self.app.clock.now() >= self.spec.expires_at
                        else "failed"
                    )
                finally:
                    self.app.operating_window = None
                    if state == "expired":
                        self.snapshot["last_seen"] = max(
                            self.snapshot["last_seen"],
                            self.spec.expires_at.astimezone(UTC).isoformat(),
                        )
                    # Observing expiry is durable even when the next clock
                    # sample (or a restarted process) moves backwards.
                    self.snapshot["last_seen"] = max(
                        self.snapshot["last_seen"],
                        self.app.clock.now().astimezone(UTC).isoformat(),
                    )
                    with self.app.journal.transaction() as db:
                        self._watermark(db)
                return self.summary(state)
        except BlockingIOError:
            # The other process owns all admissions. Do not wait or touch its work.
            return {"window_id": self.spec.window_id, "state": "busy"}

    def summary(self, state):
        with self.app.journal.transaction() as db:
            row = self._row(db)
            consumed = self._counts(db)
            intents = db.execute(
                "SELECT i.operation,i.phase,i.payload FROM intents i "
                "JOIN public_window_work w ON w.operation=i.operation WHERE w.window=?",
                (self.spec.window_id,),
            ).fetchall()
            research = Counter(
                row[0]
                for row in db.execute(
                    "SELECT r.phase FROM public_research_ops r JOIN public_window_work w "
                    "ON w.operation=r.operation WHERE w.window=?",
                    (self.spec.window_id,),
                )
            )
            pending_mission = (
                self.app.mission_evolution.pending_in(db)
                if self.spec.mission_evolution
                else None
            )
            lanes = {"tick": Counter(), "poll": Counter()}
            for kind, outcome in db.execute(
                "SELECT kind,outcome FROM public_window_work WHERE window=? "
                "AND kind IN ('tick','poll')",
                (self.spec.window_id,),
            ):
                lanes[kind]["uncertain" if outcome == "started" else outcome] += 1
        outcomes, pending = Counter(), []
        for intent in intents:
            payload = json.loads(intent["payload"])
            phase = intent["phase"]
            outcomes[
                "uncertain"
                if phase == "started"
                else phase
                if phase in ("rejected", "held")
                else payload["decision"]
            ] += 1
            if "draft" in payload:
                try:
                    item = self.app.service.store.get(intent["operation"])
                except ThreadsStoreError:
                    continue
                if item.state is OutboxState.AWAITING_APPROVAL:
                    pending.append(
                        {"operation_id": item.operation_id, "draft_id": item.draft_id}
                    )
        return {
            "window_id": self.spec.window_id,
            "state": state,
            "expires_at": self.spec.expires_at.isoformat(),
            "consumed": consumed,
            "limits": json.loads(self.spec.encoded()),
            "next_due": {
                "wakeup": row["next_wake"],
                "poll": row["next_poll"]
                if consumed["poll_pages"] < self.spec.max_poll_pages
                else None,
            },
            "outcomes": dict(outcomes),
            "wakeup_outcomes": dict(lanes["tick"]),
            "poll_outcomes": dict(lanes["poll"]),
            "research": {
                "consumed": sum(research.values()),
                "outcomes": dict(research),
            },
            "mission": {
                "mode": "evolving" if self.spec.mission_evolution else "fixed",
                "active": json.loads(row["controls"])["mission"],
                "pending": None
                if pending_mission is None
                else pending_mission.proposal.proposal_id,
            },
            "pending_drafts": pending,
        }
