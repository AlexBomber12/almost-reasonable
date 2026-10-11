"""Restart and concurrency evidence using only local deterministic callbacks."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest

from app.behavior.durable_scheduler import DurableScheduler, SQLiteSchedulerRepository
from app.behavior.scheduler import MutableClock, SchedulerOutcome, WakeupEvent
from app.core.agent_proposals import AgentDecision
from tests.test_behavior_scheduler import NOW, event, policy


class Controls:
    stop = False
    suspended = False

    def active(self):
        return self.stop

    def is_spend_suspended(self):
        return self.suspended


def scheduler(path, clock, controls, callback):
    return DurableScheduler(
        policy(),
        SQLiteSchedulerRepository(path),
        clock,
        callback,
        owner=controls,
        provider=controls,
    )


def test_restart_future_metadata_no_action_and_later_value(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    clock, controls = MutableClock(NOW), Controls()
    first = scheduler(path, clock, controls, lambda *_: AgentDecision.NO_ACTION)
    future = event("later", NOW + timedelta(days=30))
    result = first.run((event("now"), future, event("now")))
    assert result.outcome is SchedulerOutcome.NO_ACTION
    assert result.coalesced_count == 0
    reopened = scheduler(path, clock, controls, lambda *_: AgentDecision.RESEARCH)
    assert reopened.run((event("now"),)).outcome is SchedulerOutcome.DUPLICATE
    clock.advance(timedelta(days=30))
    assert reopened.run().outcome is SchedulerOutcome.PROPOSED
    assert reopened.repository.load().last_deliberation_at == clock.now()
    assert reopened.repository.outcomes()[-1].decision is AgentDecision.RESEARCH


def test_concurrent_instances_do_not_call_competing_models(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    clock, controls = MutableClock(NOW), Controls()
    entered, release = Event(), Event()
    calls = []

    def deliberate(*_):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return AgentDecision.NO_ACTION

    first = scheduler(path, clock, controls, deliberate)
    second = scheduler(path, clock, controls, deliberate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(first.run, (event("race"),))
        assert entered.wait(5)
        assert second.run((event("race"),)).outcome is SchedulerOutcome.EXECUTION_LIMIT
        release.set()
        assert running.result().outcome is SchedulerOutcome.NO_ACTION
    assert calls == [1]


def test_interrupted_claim_before_start_recovers_but_started_never_replays(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    repo = SQLiteSchedulerRepository(path)
    clock, controls = MutableClock(NOW), Controls()
    claim = repo.claim((event("before"),), NOW, timedelta(seconds=5))
    clock.advance(timedelta(seconds=6))
    running = scheduler(path, clock, controls, lambda *_: AgentDecision.NO_ACTION)
    assert running.run().outcome is SchedulerOutcome.NO_ACTION
    assert not repo.begin(claim, clock.now())
    clock.advance(timedelta(minutes=11))
    claim = repo.claim(
        (event("uncertain", clock.now()),), clock.now(), timedelta(seconds=5)
    )
    assert repo.begin(claim, clock.now())
    clock.advance(timedelta(seconds=6))
    assert running.run().outcome is SchedulerOutcome.NOTHING_DUE
    assert repo.outcomes()[-1].outcome is SchedulerOutcome.FAILED
    assert (
        running.run((event("uncertain", clock.now()),)).outcome
        is SchedulerOutcome.DUPLICATE
    )
    assert not repo.complete(claim, repo.load(), repo.outcomes()[-1], clock.now())


@pytest.mark.parametrize(
    "signal, expected",
    [
        ("stop", SchedulerOutcome.SKIPPED_STOP),
        ("suspended", SchedulerOutcome.SKIPPED_SPEND),
    ],
)
def test_guards_rechecked_and_skips_survive_restart(tmp_path, signal, expected):
    path = tmp_path / "scheduler.sqlite"
    clock, controls = MutableClock(NOW), Controls()
    setattr(controls, signal, True)

    def forbidden(*_):
        pytest.fail("guarded callback")

    assert (
        scheduler(path, clock, controls, forbidden).run((event("stopped"),)).outcome
        is expected
    )
    reopened = scheduler(path, clock, controls, forbidden)
    assert reopened.run((event("stopped"),)).outcome is SchedulerOutcome.DUPLICATE
    setattr(controls, signal, False)
    assert reopened.run().outcome is SchedulerOutcome.NOTHING_DUE


def test_cadence_rate_history_and_expired_duplicates(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    clock, controls = MutableClock(NOW), Controls()
    running = scheduler(path, clock, controls, lambda *_: AgentDecision.RESEARCH)
    running.repository.select_cadence(timedelta(hours=2))
    assert running.run((event("one"),)).outcome is SchedulerOutcome.PROPOSED
    clock.advance(timedelta(minutes=11))
    reopened = scheduler(path, clock, controls, lambda *_: AgentDecision.RESEARCH)
    assert (
        reopened.run((event("two", clock.now()),)).outcome
        is SchedulerOutcome.RATE_LIMITED
    )
    clock.advance(timedelta(hours=1))
    assert (
        reopened.run((event("three", clock.now()),)).outcome
        is SchedulerOutcome.RATE_LIMITED
    )
    clock.advance(timedelta(hours=1))
    assert (
        reopened.run((event("four", clock.now()),)).outcome is SchedulerOutcome.PROPOSED
    )
    clock.advance(timedelta(days=2))
    assert (
        reopened.run((event("one", clock.now()),)).outcome is SchedulerOutcome.PROPOSED
    )
    for value in (None, timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError, match="cadence"):
            running.repository.select_cadence(value)


def test_repository_rejects_foreign_schema_and_bounded_queue(tmp_path):
    import sqlite3

    path = tmp_path / "scheduler.sqlite"
    repo = SQLiteSchedulerRepository(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE scheduler_schema SET version=2")
    with pytest.raises(ValueError, match="schema"):
        SQLiteSchedulerRepository(path)
    other = tmp_path / "foreign.sqlite"
    with sqlite3.connect(other) as db:
        db.execute("CREATE TABLE foreign_component(value)")
    with pytest.raises(ValueError, match="another component"):
        SQLiteSchedulerRepository(other)
    with sqlite3.connect(other) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("foreign_component",)]
    for pending in ([event("bad")], (None,)):
        with pytest.raises(TypeError, match="pending"):
            repo.claim(pending, NOW, timedelta(seconds=5))
    with pytest.raises(ValueError, match="bound"):
        repo.claim(tuple(event(str(i)) for i in range(1025)), NOW, timedelta(seconds=5))
    with pytest.raises(ValueError, match="lease"):
        repo.claim((), NOW, timedelta(0))
    path = tmp_path / "bounded.sqlite"
    repo = SQLiteSchedulerRepository(path)
    future = NOW + timedelta(days=1)
    pending = tuple(event(str(i), future) for i in range(1024))
    assert (
        repo.claim(pending, NOW, timedelta(seconds=5)).outcome
        is SchedulerOutcome.NOTHING_DUE
    )
    with pytest.raises(ValueError, match="bound"):
        repo.claim((event("overflow"),), NOW, timedelta(seconds=5))
    # Failed admission rolls back; the original bounded batch still coalesces once.
    claim = repo.claim((), future, timedelta(seconds=5))
    assert len(claim.events) == 1024
    assert repo.begin(claim, future)
    assert not repo.begin(claim, future)


def test_completion_requires_started_claim_and_matching_identity(tmp_path):
    from dataclasses import replace
    from app.behavior.scheduler import SchedulerRun

    repo = SQLiteSchedulerRepository(tmp_path / "scheduler.sqlite")
    claim = repo.claim((event("one"),), NOW, timedelta(seconds=5))
    result = SchedulerRun(
        SchedulerOutcome.NO_ACTION, "one", 0, 1, AgentDecision.NO_ACTION
    )
    assert not repo.complete(claim, claim.state, result, NOW)
    assert not repo.complete(claim, claim.state, replace(result, iterations=0), NOW)
    assert repo.begin(claim, NOW)
    assert not repo.complete(
        replace(claim, events=(event("forged"),)), claim.state, result, NOW
    )
    assert repo.complete(claim, claim.state, result, NOW)
    assert not repo.complete(claim, claim.state, result, NOW)
    expired = repo.claim((event("two"),), NOW, timedelta(seconds=5))
    assert not repo.complete(expired, expired.state, result, NOW + timedelta(seconds=5))


def test_guards_change_before_model_and_after_result(tmp_path):
    class ChangingControls(Controls):
        reads = 0

        def active(self):
            self.reads += 1
            return self.reads >= 2

    clock = MutableClock(NOW)
    running = scheduler(
        tmp_path / "before.sqlite",
        clock,
        ChangingControls(),
        lambda *_: pytest.fail("stop changed"),
    )
    assert running.run((event("one"),)).outcome is SchedulerOutcome.SKIPPED_STOP
    assert running.repository.outcomes()[-1].iterations == 0
    for signal, outcome in (
        ("stop", SchedulerOutcome.SKIPPED_STOP),
        ("suspended", SchedulerOutcome.SKIPPED_SPEND),
    ):
        controls = Controls()

        def deliberate(*_):
            setattr(controls, signal, True)
            return AgentDecision.RESEARCH

        running = scheduler(
            tmp_path / (signal + ".sqlite"), clock, controls, deliberate
        )
        assert running.run((event("one"),)).outcome is outcome
        assert running.repository.load().action_history == ()
        assert running.repository.outcomes()[-1].decision is None


def test_clock_rollback_and_expiry_during_call_are_fenced(tmp_path):
    clock, controls = MutableClock(NOW), Controls()
    running = scheduler(
        tmp_path / "rollback.sqlite",
        clock,
        controls,
        lambda *_: AgentDecision.NO_ACTION,
    )
    assert running.run((event("one"),)).outcome is SchedulerOutcome.NO_ACTION
    clock.advance(timedelta(seconds=-1))
    assert (
        running.run((event("two", clock.now()),)).outcome
        is SchedulerOutcome.EXECUTION_LIMIT
    )
    clock.advance(timedelta(minutes=11))
    assert (
        running.run((event("two", clock.now()),)).outcome is SchedulerOutcome.NO_ACTION
    )

    def late(*_):
        clock.advance(timedelta(seconds=8))
        return AgentDecision.REPLY

    expired = scheduler(tmp_path / "expired.sqlite", clock, controls, late)
    assert (
        expired.run((event("expired", clock.now()),)).outcome
        is SchedulerOutcome.EXECUTION_LIMIT
    )
    assert expired.repository.outcomes() == ()
    assert expired.run().outcome is SchedulerOutcome.NOTHING_DUE
    assert expired.repository.outcomes()[-1].outcome is SchedulerOutcome.FAILED


def test_stale_begin_and_untrusted_controls_fail_closed(tmp_path):
    class LosingRepository(SQLiteSchedulerRepository):
        def begin(self, claim, now):
            return False

    clock, controls = MutableClock(NOW), Controls()
    running = DurableScheduler(
        policy(),
        LosingRepository(tmp_path / "lost.sqlite"),
        clock,
        lambda *_: pytest.fail("lost claim"),
        owner=controls,
        provider=controls,
    )
    assert running.run((event("one"),)).outcome is SchedulerOutcome.EXECUTION_LIMIT
    controls.stop = None
    with pytest.raises(TypeError, match="booleans"):
        running.run((event("two"),))
    controls.stop = False
    controls.suspended = "false"
    with pytest.raises(TypeError, match="booleans"):
        running.run((event("two"),))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("scheduler attempted network, credentials, or an external effect")

    for name in (
        "socket.socket",
        "socket.create_connection",
        "socket.getaddrinfo",
        "app.trusted.openai_transport.OpenAIResponsesTransport.from_environment",
        "app.trusted.openai_credential_files.read_protected_worker_file",
        "app.trusted.openai_credential_files.build_worker_openai_transport",
    ):
        monkeypatch.setattr(name, forbidden)


@pytest.mark.parametrize(
    "failure, expected",
    [
        (TimeoutError, SchedulerOutcome.EXECUTION_LIMIT),
        (OSError, SchedulerOutcome.FAILED),
        (ValueError, SchedulerOutcome.FAILED),
    ],
)
def test_failures_never_become_no_action_or_retry(tmp_path, failure, expected):
    clock, controls = MutableClock(NOW), Controls()
    calls = []

    def fail(*_):
        calls.append(1)
        raise failure("synthetic error")

    path = tmp_path / "scheduler.sqlite"
    assert (
        scheduler(path, clock, controls, fail).run((event("one"),)).outcome is expected
    )
    assert (
        scheduler(path, clock, controls, fail).run((event("one"),)).outcome
        is SchedulerOutcome.DUPLICATE
    )
    assert calls == [1]


def test_programmer_interruption_after_decision_does_not_fabricate_success(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    clock, controls = MutableClock(NOW), Controls()
    calls = []

    class CrashOnCompletion(SQLiteSchedulerRepository):
        def complete(self, *args):
            raise RuntimeError("synthetic crash after deliberation")

    def deliberate(*_):
        calls.append(1)
        return AgentDecision.NO_ACTION

    running = DurableScheduler(
        policy(),
        CrashOnCompletion(path),
        clock,
        deliberate,
        owner=controls,
        provider=controls,
    )
    with pytest.raises(RuntimeError, match="after deliberation"):
        running.run((event("one"),))
    clock.advance(timedelta(seconds=8))
    controls.stop = True
    reopened = scheduler(path, clock, controls, deliberate)
    assert reopened.run().outcome is SchedulerOutcome.NOTHING_DUE
    assert reopened.repository.outcomes()[-1].outcome is SchedulerOutcome.FAILED
    controls.stop = False
    assert reopened.run((event("one"),)).outcome is SchedulerOutcome.DUPLICATE
    assert calls == [1]

    def contract_bug(*_):
        raise TypeError("synthetic adapter bug")

    clock.advance(timedelta(minutes=11))
    with pytest.raises(TypeError, match="adapter bug"):
        scheduler(path, clock, controls, contract_bug).run((event("two", clock.now()),))
    clock.advance(timedelta(seconds=8))
    assert reopened.run().outcome is SchedulerOutcome.NOTHING_DUE
    assert reopened.repository.outcomes()[-1].outcome is SchedulerOutcome.FAILED


def test_actual_owner_and_persistent_provider_suspension_composition(tmp_path):
    from app.owner_control.store import OwnerStore
    from app.core.provider_spend import ProviderStateStore
    from app.core.store import EventStore
    from tests.test_provider_spend import attempt

    path = tmp_path / "scheduler.sqlite"
    clock = MutableClock(NOW)
    owner = OwnerStore(tmp_path / "owner", clock=clock.now)
    owner.provision("synthetic-operator", "synthetic-owner-password")
    provider = ProviderStateStore(EventStore(tmp_path / "events.sqlite"))
    running = DurableScheduler(
        policy(),
        SQLiteSchedulerRepository(path),
        clock,
        lambda *_: pytest.fail("trusted guards"),
        owner=owner,
        provider=provider,
    )
    assert (
        running.run((event("stop-default"),)).outcome is SchedulerOutcome.SKIPPED_STOP
    )
    login = owner.login(
        "synthetic-operator", "synthetic-owner-password", "synthetic-client"
    )
    operation = owner.issue(login.session_token)
    assert owner.change(login.session_token, operation, False) == "applied"
    provider.record_attempt(
        attempt(outcome="confirmed_spend_limit"),
        confirmed_spend_limit_code="project_spend_limit_exceeded",
    )
    restarted = DurableScheduler(
        policy(),
        SQLiteSchedulerRepository(path),
        clock,
        lambda *_: pytest.fail("durable suspension"),
        owner=OwnerStore(owner.directory, clock=clock.now),
        provider=ProviderStateStore(EventStore(tmp_path / "events.sqlite")),
    )
    assert restarted.run((event("spend"),)).outcome is SchedulerOutcome.SKIPPED_SPEND
    assert restarted.run().outcome is SchedulerOutcome.NOTHING_DUE


def test_timezone_fold_and_overnight_window_survive_restart(tmp_path):
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("America/New_York")
    clock = MutableClock(datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0))
    future = event("fold", datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1))
    controls = Controls()
    path = tmp_path / "scheduler.sqlite"
    overnight = policy(active_start=time(22), active_end=time(6))
    running = DurableScheduler(
        overnight,
        SQLiteSchedulerRepository(path),
        clock,
        lambda *_: AgentDecision.NO_ACTION,
        owner=controls,
        provider=controls,
    )
    assert running.run((future,)).outcome is SchedulerOutcome.NOTHING_DUE
    # An absolute instant advances while the local wall clock moves backward.
    clock = MutableClock(datetime(2026, 11, 1, 1, 20, tzinfo=zone, fold=1))
    restarted = DurableScheduler(
        overnight,
        SQLiteSchedulerRepository(path),
        clock,
        lambda *_: AgentDecision.NO_ACTION,
        owner=controls,
        provider=controls,
    )
    assert restarted.run().outcome is SchedulerOutcome.NO_ACTION
    clock.advance(timedelta(hours=11))
    assert (
        restarted.run((event("day", clock.now()),)).outcome
        is SchedulerOutcome.OUTSIDE_ACTIVE_HOURS
    )


@pytest.mark.parametrize(
    "decision",
    [
        AgentDecision.PROPOSE_PUBLICATION,
        AgentDecision.REVISE_MISSION,
        AgentDecision.RESEARCH,
    ],
)
def test_hostile_decision_is_only_a_proposal_without_effects(tmp_path, decision):
    clock, controls = MutableClock(NOW), Controls()
    running = scheduler(
        tmp_path / "scheduler.sqlite", clock, controls, lambda *_: decision
    )
    result = running.run((event("hostile"),))
    assert result.outcome is SchedulerOutcome.PROPOSED
    assert result.decision is decision
    assert sorted(p.name for p in tmp_path.iterdir()) == ["scheduler.sqlite"]
    clock.advance(timedelta(minutes=11))
    invalid = scheduler(
        tmp_path / "scheduler.sqlite",
        clock,
        controls,
        lambda *_: {
            "decision": "propose_publication",
            "approved": True,
            "publish_now": True,
        },
    )
    assert (
        invalid.run((event("invalid", clock.now()),)).outcome is SchedulerOutcome.FAILED
    )


@pytest.mark.parametrize("phase", ["claimed", "started", "returned"])
def test_process_crash_recovery(tmp_path, phase):
    import subprocess
    import sys

    path = tmp_path / "scheduler.sqlite"
    script = """
import os, sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from app.behavior.durable_scheduler import SQLiteSchedulerRepository
from app.behavior.scheduler import WakeupEvent
from app.core.agent_proposals import AgentDecision
now = datetime(2026, 10, 10, 12, tzinfo=UTC)
repo = SQLiteSchedulerRepository(Path(sys.argv[1]))
claim = repo.claim((WakeupEvent("crash", now, "synthetic"),), now, timedelta(seconds=5))
if sys.argv[2] != "claimed":
    assert repo.begin(claim, now)
if sys.argv[2] == "returned":
    decision = AgentDecision.NO_ACTION
    assert decision is AgentDecision.NO_ACTION
os._exit(23)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), phase],
        check=False,
        capture_output=True,
    )
    assert result.returncode == 23, result.stderr.decode()
    clock, controls = MutableClock(NOW + timedelta(seconds=6)), Controls()
    calls = []

    def deliberate(*_):
        calls.append(1)
        return AgentDecision.NO_ACTION

    reopened = scheduler(path, clock, controls, deliberate)
    outcome = reopened.run().outcome
    if phase == "claimed":
        assert outcome is SchedulerOutcome.NO_ACTION
        assert calls == [1]
    else:
        assert outcome is SchedulerOutcome.NOTHING_DUE
        assert calls == []
        assert reopened.repository.outcomes()[-1].outcome is SchedulerOutcome.FAILED
        # Uncertain identity is retained beyond the normal duplicate window.
        clock.advance(timedelta(days=30))
        assert (
            reopened.run((event("crash", clock.now()),)).outcome
            is SchedulerOutcome.DUPLICATE
        )


def test_completion_watermark_includes_finish_and_rollback_preserves_pending(tmp_path):
    clock, controls = MutableClock(NOW), Controls()

    def deliberate(*_):
        clock.advance(timedelta(seconds=1))
        return AgentDecision.NO_ACTION

    path = tmp_path / "scheduler.sqlite"
    running = scheduler(path, clock, controls, deliberate)
    assert running.run((event("one"),)).outcome is SchedulerOutcome.NO_ACTION
    clock.advance(timedelta(seconds=-1))
    assert (
        scheduler(path, clock, controls, deliberate).run((event("two"),)).outcome
        is SchedulerOutcome.EXECUTION_LIMIT
    )
    clock.advance(timedelta(minutes=11))
    assert (
        running.run((event("two", clock.now()),)).outcome is SchedulerOutcome.NO_ACTION
    )


def test_bounded_missed_wakeups_and_stopped_recovery_coalesce(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    clock, controls = MutableClock(NOW), Controls()
    repo = SQLiteSchedulerRepository(path)
    events = tuple(
        event(f"event-{i}", NOW - timedelta(seconds=1024 - i)) for i in range(1024)
    )
    claim = repo.claim(events, NOW, timedelta(seconds=5))
    assert len(claim.events) == 1024
    clock.advance(timedelta(seconds=6))
    controls.stop = True
    running = scheduler(path, clock, controls, lambda *_: AgentDecision.NO_ACTION)
    skipped = running.run()
    assert skipped.outcome is SchedulerOutcome.SKIPPED_STOP
    assert skipped.coalesced_count == 1023
    assert len(running.repository.load().processed_event_ids) == 1024
    controls.stop = False
    assert running.run().outcome is SchedulerOutcome.NOTHING_DUE
    clock.advance(timedelta(days=30))
    assert (
        running.run((event("valuable", clock.now()),)).outcome
        is SchedulerOutcome.NO_ACTION
    )


def test_expired_uncertain_batch_releases_capacity_for_new_events(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    repo = SQLiteSchedulerRepository(path)
    pending = tuple(event(str(i)) for i in range(1024))
    claim = repo.claim(pending, NOW, timedelta(seconds=5))
    assert repo.begin(claim, NOW)
    clock, controls = MutableClock(NOW + timedelta(minutes=11)), Controls()
    running = scheduler(path, clock, controls, lambda *_: AgentDecision.NO_ACTION)
    result = running.run((event("new-value", clock.now()),))
    assert result.outcome is SchedulerOutcome.NO_ACTION
    assert result.selected_event_id == "new-value"
    assert repo.outcomes()[0].outcome is SchedulerOutcome.FAILED


def test_inflight_submission_retains_future_events_without_locking_callback(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    clock, controls = MutableClock(NOW), Controls()
    second = scheduler(path, clock, controls, lambda *_: AgentDecision.NO_ACTION)
    future = event("future", NOW + timedelta(hours=1))

    def deliberate(*_):
        assert second.run((future,)).outcome is SchedulerOutcome.EXECUTION_LIMIT
        second.repository.select_cadence(timedelta(hours=2))
        return AgentDecision.NO_ACTION

    first = scheduler(path, clock, controls, deliberate)
    assert first.run((event("one"),)).outcome is SchedulerOutcome.NO_ACTION
    clock.advance(timedelta(hours=1))
    assert second.run().selected_event_id == "future"
    clock.advance(timedelta(minutes=11))
    claim = second.repository.claim(
        (event("probe", clock.now()),), clock.now(), timedelta(seconds=5)
    )
    assert claim.cadence == timedelta(hours=2)


def test_two_processes_race_for_one_durable_wakeup(tmp_path):
    import multiprocessing

    path = tmp_path / "scheduler.sqlite"
    SQLiteSchedulerRepository(path)
    context = multiprocessing.get_context("fork")
    start, release = context.Event(), context.Event()
    calls, results = context.Queue(), context.Queue()

    def worker():
        def deliberate(*_):
            calls.put("called")
            assert release.wait(10)
            return AgentDecision.NO_ACTION

        running = scheduler(path, MutableClock(NOW), Controls(), deliberate)
        assert start.wait(10)
        results.put(running.run((event("one"),)).outcome.value)

    children = [context.Process(target=worker) for _ in range(2)]
    for child in children:
        child.start()
    try:
        start.set()
        assert calls.get(timeout=10) == "called"
        assert results.get(timeout=10) == SchedulerOutcome.EXECUTION_LIMIT.value
        release.set()
        assert results.get(timeout=10) == SchedulerOutcome.NO_ACTION.value
        for child in children:
            child.join(timeout=10)
            assert child.exitcode == 0
        assert calls.empty()
    finally:
        release.set()
        for child in children:
            if child.is_alive():
                child.kill()
            child.join(timeout=10)
        calls.close()
        results.close()


def test_stale_owner_cannot_complete_reassigned_claim(tmp_path):
    from app.behavior.scheduler import SchedulerRun

    repo = SQLiteSchedulerRepository(tmp_path / "scheduler.sqlite")
    first = repo.claim((event("one"),), NOW, timedelta(seconds=5))
    later = NOW + timedelta(seconds=6)
    second = repo.claim((), later, timedelta(seconds=5))
    assert second.token != first.token
    assert repo.begin(second, later)
    result = SchedulerRun(
        SchedulerOutcome.NO_ACTION, "one", 0, 1, AgentDecision.NO_ACTION
    )
    assert not repo.begin(first, later)
    assert not repo.complete(first, first.state, result, later)
    assert repo.complete(second, second.state, result, later)
    assert repo.outcomes() == (result,)


def test_completed_no_action_survives_real_process_exit(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "scheduler.sqlite"
    script = """
import os, sys
from pathlib import Path
from app.behavior.durable_scheduler import DurableScheduler, SQLiteSchedulerRepository
from app.behavior.scheduler import MutableClock, SchedulerOutcome, WakeupEvent
from app.core.agent_proposals import AgentDecision
from tests.test_behavior_scheduler import NOW, policy, event
class Controls:
    def active(self): return False
    def is_spend_suspended(self): return False
controls = Controls()
scheduler = DurableScheduler(policy(), SQLiteSchedulerRepository(Path(sys.argv[1])), MutableClock(NOW), lambda *_: AgentDecision.NO_ACTION, owner=controls, provider=controls)
assert scheduler.run((event("completed"),)).outcome is SchedulerOutcome.NO_ACTION
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode()
    reopened = scheduler(
        path,
        MutableClock(NOW),
        Controls(),
        lambda *_: pytest.fail("completed decision replay"),
    )
    assert reopened.run((event("completed"),)).outcome is SchedulerOutcome.DUPLICATE
    assert reopened.repository.outcomes()[-1].outcome is SchedulerOutcome.NO_ACTION


def test_rollback_behind_committed_start_fence_rejects_apparent_success(tmp_path):
    clock, controls = MutableClock(NOW), Controls()

    class AdvancingRepository(SQLiteSchedulerRepository):
        def begin(self, claim, now):
            clock.advance(timedelta(seconds=1))
            return super().begin(claim, clock.now())

    def deliberate(*_):
        clock.advance(timedelta(milliseconds=-500))
        return AgentDecision.NO_ACTION

    path = tmp_path / "scheduler.sqlite"
    running = DurableScheduler(
        policy(),
        AdvancingRepository(path),
        clock,
        deliberate,
        owner=controls,
        provider=controls,
    )
    assert running.run((event("rollback"),)).outcome is SchedulerOutcome.EXECUTION_LIMIT
    assert running.repository.outcomes() == ()
    clock.advance(timedelta(seconds=8))
    reopened = scheduler(
        path, clock, controls, lambda *_: pytest.fail("uncertain replay")
    )
    assert reopened.run().outcome is SchedulerOutcome.NOTHING_DUE
    assert reopened.repository.outcomes()[-1].outcome is SchedulerOutcome.FAILED


def test_one_time_arrival_during_rollback_is_durable_without_resubmission(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    clock, controls = MutableClock(NOW), Controls()
    calls = []

    def deliberate(wakeup, _timeout):
        calls.append(wakeup.event_id)
        return AgentDecision.NO_ACTION

    running = scheduler(path, clock, controls, deliberate)
    assert running.run((event("one"),)).outcome is SchedulerOutcome.NO_ACTION
    clock.advance(timedelta(seconds=-1))
    assert (
        running.run((event("one-time", clock.now()),)).outcome
        is SchedulerOutcome.EXECUTION_LIMIT
    )
    clock.advance(timedelta(minutes=11))
    reopened = scheduler(path, clock, controls, deliberate)
    assert reopened.run().outcome is SchedulerOutcome.NO_ACTION
    assert calls == ["one", "one-time"]


@pytest.mark.parametrize("maximum", [1e20, 1e308, 10**308])
def test_large_valid_execution_limit_has_bounded_runnable_lease(tmp_path, maximum):
    path = tmp_path / "scheduler.sqlite"
    clock, controls = MutableClock(NOW), Controls()
    received = []

    def deliberate(_event, timeout):
        received.append(timeout)
        return AgentDecision.NO_ACTION

    running = DurableScheduler(
        policy(max_execution_seconds=maximum),
        SQLiteSchedulerRepository(path),
        clock,
        deliberate,
        owner=controls,
        provider=controls,
    )
    assert running.run((event("large-limit"),)).outcome is SchedulerOutcome.NO_ACTION
    assert received == [maximum]


def test_lease_deadline_clamps_at_latest_representable_instant(tmp_path):
    from datetime import UTC, datetime

    latest = datetime.max.replace(tzinfo=UTC)
    repo = SQLiteSchedulerRepository(tmp_path / "scheduler.sqlite")
    claim = repo.claim((event("latest", latest),), latest, timedelta.max)
    assert claim.events == (event("latest", latest),)
    assert not repo.begin(claim, latest)
    # The due event remains recoverable, although there is no representable
    # later instant in which execution could start with an unexpired lease.
    recovered = repo.claim((), latest, timedelta(seconds=5))
    assert recovered.events == claim.events


@pytest.mark.parametrize(
    "cadence", [timedelta.max, timedelta(days=365001, microseconds=1)]
)
def test_selected_cadence_round_trips_exactly_across_restart(tmp_path, cadence):
    path = tmp_path / "scheduler.sqlite"
    repo = SQLiteSchedulerRepository(path)
    repo.select_cadence(cadence)
    reopened = SQLiteSchedulerRepository(path)
    claim = reopened.claim((event("cadence"),), NOW, timedelta(seconds=5))
    assert claim.cadence == cadence


def test_expired_reusable_identity_survives_rollback_after_delayed_start(tmp_path):
    path = tmp_path / "scheduler.sqlite"
    clock, controls = MutableClock(NOW), Controls()
    calls = []

    def deliberate(wakeup, _timeout):
        calls.append((wakeup.event_id, wakeup.trigger))
        return AgentDecision.NO_ACTION

    running = scheduler(path, clock, controls, deliberate)
    assert running.run((event("reusable"),)).outcome is SchedulerOutcome.NO_ACTION
    clock.advance(timedelta(hours=23))
    claim = running.repository.claim(
        (event("paused", clock.now()),), clock.now(), timedelta(days=1)
    )
    clock.advance(timedelta(hours=2))
    assert running.repository.begin(claim, clock.now())
    clock.advance(timedelta(minutes=-30))
    reused = WakeupEvent("reusable", clock.now(), "new-valuable-evidence")
    assert running.run((reused,)).outcome is SchedulerOutcome.EXECUTION_LIMIT
    clock.advance(timedelta(hours=22, minutes=30, seconds=1))
    reopened = scheduler(path, clock, controls, deliberate)
    assert reopened.run().outcome is SchedulerOutcome.NO_ACTION
    assert calls == [("reusable", "periodic"), ("reusable", "new-valuable-evidence")]


@pytest.mark.parametrize("elapsed", [timedelta(hours=25), timedelta(hours=48)])
def test_claim_covers_long_valid_execution_budget(tmp_path, elapsed):
    clock, controls = MutableClock(NOW), Controls()
    allowed = timedelta(hours=48).total_seconds()

    def deliberate(_event, timeout):
        assert timeout == allowed
        clock.advance(elapsed)
        return AgentDecision.NO_ACTION

    running = DurableScheduler(
        policy(max_execution_seconds=allowed),
        SQLiteSchedulerRepository(tmp_path / "scheduler.sqlite"),
        clock,
        deliberate,
        owner=controls,
        provider=controls,
    )
    assert running.run((event("long-budget"),)).outcome is SchedulerOutcome.NO_ACTION
    assert running.repository.outcomes()[-1].outcome is SchedulerOutcome.NO_ACTION
    assert running.repository.load().last_deliberation_at == clock.now()
