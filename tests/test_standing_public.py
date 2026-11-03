"""Standing authority renews finite leases without resetting safety or history."""

from datetime import UTC, datetime, timedelta
import json
import os
import sqlite3

import pytest

from app.core.public_research_profile import (
    load_research_profile,
    provider_configuration,
)
from app.core.public_research_period import (
    initialize_periods,
    period_counts,
    record_period,
)
from app.integration.public_standing import StandingPublicPolicy, StandingPublicRunner
from app.threads.service import PolicyDeniedError


NOW = datetime(2030, 1, 1, tzinfo=UTC)


def profile_data():
    return dict(
        version="public-research-profile-v3",
        role="worker",
        purpose="public-deliberation-autonomous-research-v1",
        configuration="public-research-v1",
        provider_configuration=provider_configuration(),
        profile_id="a" * 64,
        authorization="b" * 64,
        application_scope="public-test",
        account_ref="account-1",
        peer_uid=os.geteuid(),
        issued_at=NOW.isoformat(),
        expires_at=datetime.max.replace(tzinfo=UTC).isoformat(),
        reviewer="synthetic-operator",
        reviewed_at=NOW.isoformat(),
        aggregate_limit=100,
        renewal="standing-monthly-utc",
        question_policy=dict(
            mode="agent_generated_public_questions",
            max_question_bytes=512,
            per_question_limit=2,
        ),
    )


def test_explicit_public_only_standing_profile(tmp_path):
    path = tmp_path / "standing.json"

    def load(data):
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return load_research_profile(path, role="worker")

    value = profile_data()
    profile = load(value)
    assert profile.standing_monthly and profile.aggregate_limit == 100
    for changes in (
        dict(renewal="daily-reset"),
        dict(purpose="private-conversation-public-research-v1"),
        dict(expires_at=(NOW + timedelta(days=31)).isoformat()),
        dict(aggregate_limit=101),
        dict(version="public-research-profile-v2"),
    ):
        with pytest.raises(ValueError):
            load(value | changes)


def test_research_periods_preserve_legacy_usage_replays_and_clock_watermark():
    with sqlite3.connect(":memory:") as db:
        initialize_periods(db)
        assert period_counts(db, "account-1", NOW, {"old": 24}) == {"old": 24}
        record_period(db, "op-1", "account-1", "question", NOW)
        assert period_counts(db, "account-1", NOW, {"old": 25}) == {
            "old": 24,
            "question": 1,
        }
        later = datetime(2030, 2, 1, tzinfo=UTC)
        assert period_counts(db, "account-1", later, {"old": 25}) == {}
        record_period(db, "op-2", "account-1", "question", later)
        assert period_counts(db, "account-1", later, {}) == {"question": 1}
        assert (
            db.execute("SELECT COUNT(*) FROM public_research_period_work").fetchone()[0]
            == 2
        )
        with pytest.raises(sqlite3.IntegrityError):
            record_period(db, "op-1", "account-1", "question", later)
        with pytest.raises(ValueError, match="clock"):
            period_counts(db, "account-1", NOW, {})


def test_standing_policy_rejects_unbudgeted_or_mismatched_authority():
    policy = StandingPublicPolicy("continuous-1", "account-1", "proj_public")
    window, grant = policy.lease(NOW)
    assert NOW < window.expires_at <= NOW + timedelta(days=1)
    assert (
        grant.window_id == window.window_id
        and grant.grant_id == window.publication_grant
    )
    assert grant.account_ref == "account-1" and grant.max_publications == 6
    assert policy.lease(NOW + timedelta(hours=1)) == (window, grant)
    assert policy.lease(NOW + timedelta(days=1))[0].window_id != window.window_id
    with pytest.raises(ValueError):
        StandingPublicPolicy("continuous-1", "account-1", "")


def connected(tmp_path):
    from app.core.public_research_client import PublicResearchClient
    from test_b11c_window import setup, NO_ACTION
    from test_int24_runtime import NOW as runtime_now

    value = profile_data() | {
        "role": "application",
        "issued_at": runtime_now.isoformat(),
        "reviewed_at": runtime_now.isoformat(),
    }
    path = tmp_path / "application.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    parts = setup(
        tmp_path,
        NO_ACTION,
        mission_evolution_enabled=True,
        free_missions_enabled=True,
        autonomous_publication_enabled=True,
        research_profile_path=path,
        research_scope="public-test",
        research_enabled=True,
        research_client=PublicResearchClient(
            tmp_path / "unused-research.sock", expected_worker_uid=os.geteuid()
        ),
    )
    return parts, StandingPublicRunner(
        parts[0], StandingPublicPolicy("continuous-1", "account-1", "proj_public")
    )


def test_connected_daily_renewal_preserves_leases_stop_and_revocation(
    tmp_path, monkeypatch
):
    from acceptance.guards import install
    from test_b11c_window import serve, finish
    from dataclasses import replace

    install(monkeypatch, local_ipc=True)
    (app, worker, sdk, _http, clock), runner = connected(tmp_path)
    original_lease = runner.policy.lease
    # Two real finite windows with real journals/IPC, advancing only the test clock.
    monkeypatch.setattr(
        StandingPublicPolicy,
        "lease",
        lambda self, now: (
            replace(original_lease(now)[0], max_model_calls=1, max_poll_pages=0),
            original_lease(now)[1],
        ),
    )
    thread = serve(worker, 2)
    try:

        def wait(seconds):
            clock.advance(timedelta(hours=12))

        result = runner.run(wait=wait, should_stop=lambda: len(sdk.calls) == 2)
        finish(thread)
        assert result["state"] == "interrupted" and len(sdk.calls) == 2
        with app.journal.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM public_windows").fetchone()[0] == 2
            assert db.execute("SELECT COUNT(*) FROM public_grants").fetchone()[0] == 2
        runner._check()
        with app.owner._connection() as db:
            db.execute("UPDATE owner_stop SET active=1")
        with pytest.raises(PolicyDeniedError):
            runner.run(should_stop=lambda: True)
        with app.owner._connection() as db:
            db.execute("UPDATE owner_stop SET active=0")
        runner.revoke()
        with pytest.raises(PolicyDeniedError):
            runner.run(should_stop=lambda: True)
        assert len(sdk.calls) == 2
    finally:
        worker.close()


def test_standing_guard_rejects_privacy_changes_and_clock_regression(tmp_path):
    (app, worker, _sdk, _http, clock), runner = connected(tmp_path)
    try:
        runner._check()
        runner.guard()
        clock.advance(timedelta(seconds=-1))
        with pytest.raises(PolicyDeniedError, match="authority"):
            runner.guard()
        with pytest.raises(PolicyDeniedError):
            runner._check()
        clock.advance(timedelta(seconds=1))
        with app.journal.transaction() as db:
            db.execute("UPDATE public_standing_authority SET controls='changed'")
        with pytest.raises(PolicyDeniedError):
            runner._check()
        runner.revoke()
        with pytest.raises(PolicyDeniedError):
            runner.guard()
    finally:
        worker.close()


def test_worker_monthly_research_quota_survives_profile_renewal(tmp_path):
    from dataclasses import replace
    from app.trusted.research_authority_journal import AuthorityJournal

    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile_data() | {"aggregate_limit": 2}))
    path.chmod(0o600)
    profile = load_research_profile(path, role="worker")
    now = [NOW]
    journal_path = tmp_path / "journal.sqlite"
    journal_path.touch(mode=0o600)
    journal = AuthorityJournal(journal_path, clock=lambda: now[0])
    journal.activate(profile)
    question = "How do stars form?"
    topic = profile.topic(question).topic_id
    assert journal.reserve(profile, "one", topic, question=question)
    assert journal.reserve(profile, "two", topic, question=question)
    assert journal.aggregate_exhausted(profile, "three")
    renewed = replace(profile, profile_id="c" * 64, digest="d" * 64)
    journal.activate(renewed)
    assert not journal.reserve(renewed, "three", topic, question=question)
    now[0] = datetime(2030, 2, 1, tzinfo=UTC)
    assert not journal.aggregate_exhausted(renewed, "new-month")
    assert journal.reserve(renewed, "new-month", topic, question=question)
    assert not journal.reserve(renewed, "one", topic, question=question)


def test_standing_restart_reuses_lease_and_does_not_retry_failed_window(
    tmp_path, monkeypatch
):
    from app.integration.public_window import PublicOperatingWindow

    (app, worker, _sdk, _http, _clock), runner = connected(tmp_path)
    monkeypatch.setattr(
        PublicOperatingWindow, "run", lambda *a, **kw: {"state": "failed"}
    )
    try:
        assert runner.run()["state"] == "failed"
        assert runner.run()["state"] == "failed"
        with app.journal.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM public_grants").fetchone()[0] == 1
        window, grant = runner.policy.lease(app.clock.now())
        app.public_grants.revoke(grant.grant_id)
        with pytest.raises(PolicyDeniedError, match="revoked"):
            runner.run()
        with app.journal.transaction() as db:
            db.execute("UPDATE public_grants SET revoked=0,spec='changed'")
        with pytest.raises(PolicyDeniedError, match="changed"):
            runner.run()
        with app.journal.handoff_lock("standing-public", blocking=False):
            assert runner.run() == {"state": "busy"}
        with app.journal.transaction() as db:
            db.execute("DELETE FROM public_standing_authority")
        with pytest.raises(PolicyDeniedError):
            runner.guard()
        with pytest.raises(ValueError, match="missing"):
            runner.revoke()
    finally:
        worker.close()


@pytest.mark.parametrize("change", ["account", "profile", "finite-profile"])
def test_standing_authority_requires_matching_public_monthly_profile(tmp_path, change):
    from dataclasses import replace

    (app, worker, _sdk, _http, _clock), runner = connected(tmp_path)
    try:
        if change == "account":
            runner.policy = replace(runner.policy, account_ref="another-account")
        elif change == "profile":
            app.research_profile_path.unlink()
        else:
            data = json.loads(app.research_profile_path.read_text())
            data["version"] = "public-research-profile-v2"
            data.pop("renewal")
            data["expires_at"] = (app.clock.now() + timedelta(days=1)).isoformat()
            app.research_profile_path.write_text(json.dumps(data))
        with pytest.raises(PolicyDeniedError):
            runner.run(should_stop=lambda: True)
    finally:
        worker.close()


def test_standing_cli_is_explicit_and_restores_signal_handler(
    tmp_path, monkeypatch, capsys
):
    from app.ops import int24_threads_runtime as module

    (app, worker, _sdk, _http, _clock), runner = connected(tmp_path)
    config = dict(
        account_ref=app.account,
        standing_public_reviewed=True,
        standing_public=dict(
            policy_id="continuous-1", account_ref=app.account, project_id="proj_public"
        ),
    )
    monkeypatch.setattr(module, "_config", lambda _: config)
    monkeypatch.setattr(module, "_runtime", lambda _: app)
    monkeypatch.setattr(StandingPublicRunner, "run", lambda _: {"state": "interrupted"})
    args = ["--config", "/reviewed/config", "--activate-live"]
    try:
        assert module.main(["run-continuous", *args]) == 0
        assert json.loads(capsys.readouterr().out) == {"state": "interrupted"}
        runner._check()
        assert module.main(["revoke-standing", *args]) == 0
        assert (
            json.loads(capsys.readouterr().out)["standing_public_revoked"]
            == "continuous-1"
        )
        config["standing_public_reviewed"] = False
        with pytest.raises(ValueError, match="reviewed"):
            module.main(["run-continuous", *args])
        config["standing_public_reviewed"] = True
        config["standing_public"]["account_ref"] = "different"
        with pytest.raises(ValueError, match="account"):
            module.main(["run-continuous", *args])
    finally:
        worker.close()


def test_standing_research_worker_cannot_start_without_budget(tmp_path):
    from app.core.store import EventStore
    from app.trusted.public_research_worker import build_worker
    from app.trusted.public_research_transport import PublicResearchTransport
    from test_int20_public_research import SDK

    path = tmp_path / "profile.json"
    now = datetime.now(UTC)
    path.write_text(
        json.dumps(
            profile_data()
            | {"issued_at": now.isoformat(), "reviewed_at": now.isoformat()}
        )
    )
    path.chmod(0o600)
    journal = tmp_path / "authority.sqlite"
    journal.touch(mode=0o600)
    provider = tmp_path / "provider.sqlite"
    EventStore(provider)
    with pytest.raises(ValueError, match="monthly budget"):
        build_worker(
            socket_path=tmp_path / "search.sock",
            provider_database=provider,
            profile_path=path,
            authority_journal=journal,
            stop_path=tmp_path / "stop.json",
            project_id="proj_public",
            organization_id="org-test",
            transport=PublicResearchTransport(SDK([])),
        )


def test_monthly_application_research_ledger_retains_errors_and_resets_only_period(
    tmp_path, monkeypatch
):
    from test_int24_runtime import run_once

    (app, worker, sdk, _http, clock), runner = connected(tmp_path)
    sdk.proposal = dict(decision="research", topic_id="How do stars form?")
    calls = []

    def exhausted(request):
        calls.append(request)
        return dict(outcome="allowance_exhausted", attempts=0)

    monkeypatch.setattr(app.research_client, "execute", exhausted)
    try:
        run_once(app, worker)
        profile = app.current_research_profile()
        assert len(calls) == 1
        assert app.available_topics(profile) == ()
        clock.advance(timedelta(days=31))
        assert app.available_topics(profile) == ("agent-public-question",)
        with app.journal.transaction() as db:
            assert (
                db.execute("SELECT COUNT(*) FROM public_research_ops").fetchone()[0]
                == 1
            )
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM public_research_period_work"
                ).fetchone()[0]
                == 1
            )
    finally:
        worker.close()


def test_standing_history_exceeds_finite_capacity_without_replenishing_daily_work(
    tmp_path, monkeypatch
):
    from dataclasses import replace
    from test_b11c_window import serve, finish
    from app.integration.threads_stage0 import MAX_INTENTS

    (app, worker, sdk, http, clock), runner = connected(tmp_path)
    sdk.proposal = dict(
        decision="propose_publication", draft="Проверяю основания своего суждения."
    )
    window, grant = runner.policy.lease(clock.now())
    monkeypatch.setattr(
        StandingPublicPolicy,
        "lease",
        lambda self, now: (replace(window, max_model_calls=1, max_poll_pages=0), grant),
    )
    try:
        with app.journal.transaction() as db:
            db.executemany(
                "INSERT INTO intents VALUES(?,'held','{}')",
                (("old-" + str(i),) for i in range(MAX_INTENTS)),
            )
            app.public_grants._schema(db)
            db.executemany(
                "INSERT INTO public_grants VALUES(?,'{}','{}','{}','old',0,?,0)",
                (
                    ("old-" + str(i), clock.now().isoformat())
                    for i in range(MAX_INTENTS)
                ),
            )
            db.executemany(
                "INSERT INTO approvals VALUES(?,'old','old',?,0,'publication','old')",
                (
                    ("old-" + str(i), clock.now().isoformat())
                    for i in range(MAX_INTENTS)
                ),
            )
        with pytest.raises(ValueError, match="capacity"):
            app.journal.start("finite-overflow", {})
        # Create the window schema without consuming its lifetime capacity.
        from app.integration.public_window import PublicOperatingWindow

        original = replace(window, window_id="old-window", publication_grant=None)
        PublicOperatingWindow(app, original)._initialize()
        with app.journal.transaction() as db:
            db.executemany(
                "INSERT INTO public_windows SELECT ?,spec,controls,last_seen,next_wake,next_poll,0,0 FROM public_windows WHERE id='old-window'",
                (("old-window-" + str(i),) for i in range(MAX_INTENTS - 1)),
            )
        thread = serve(worker, 1)
        assert runner.run(should_stop=lambda: bool(sdk.calls))["state"] == "interrupted"
        finish(thread)
        with app.journal.transaction() as db:
            assert (
                db.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
                == MAX_INTENTS + 1
            )
            assert (
                db.execute("SELECT COUNT(*) FROM public_grants").fetchone()[0]
                == MAX_INTENTS + 1
            )
            assert (
                db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
                == MAX_INTENTS + 1
            )
            assert (
                db.execute("SELECT COUNT(*) FROM public_windows").fetchone()[0]
                == MAX_INTENTS + 1
            )
        assert len(sdk.calls) == 1
        assert [method for method, _url, _body in http.calls] == [
            "GET",
            "POST",
            "GET",
            "POST",
            "GET",
        ]
    finally:
        worker.close()


def test_event_claim_lookup_streams_large_standing_history(tmp_path):
    import tracemalloc
    from app.integration.threads_stage0 import Stage0Journal

    journal = Stage0Journal(tmp_path / "history.sqlite", "account-1")
    wake = json.dumps(dict(kind="wake", event="old-wake", note="x" * 3300))
    reply = json.dumps(dict(kind="reply", event="one-reply", resumed=False))
    with journal.transaction() as db:
        db.executemany(
            "INSERT INTO intents VALUES(?,'linked',?)",
            (("old-" + str(i), wake) for i in range(5000)),
        )
        db.execute("INSERT INTO intents VALUES('reply','linked',?)", (reply,))
    tracemalloc.start()
    try:
        assert journal.claimed_events("reply", False) == ("one-reply",)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024, (
        "claim lookup must not decode the entire retained journal at once"
    )


def test_recovery_reads_only_mission_outcomes_in_active_window(tmp_path, monkeypatch):
    import tracemalloc
    from types import SimpleNamespace
    from app.core.agent_proposals import AgentDecision

    (app, worker, _sdk, _http, _clock), _runner = connected(tmp_path)
    old = json.dumps(dict(kind="wake", decision="no_action", note="x" * 3300))
    mission = json.dumps(dict(kind="wake", decision=AgentDecision.REVISE_MISSION.value))
    recorded = []
    monkeypatch.setattr(
        app,
        "_record_mission_outcome",
        lambda operation, payload: recorded.append(operation),
    )
    app.operating_window = SimpleNamespace(
        operations=lambda: ("wanted",), guard=lambda: None
    )
    try:
        with app.journal.transaction() as db:
            db.executemany(
                "INSERT INTO intents VALUES(?,'linked',?)",
                (("old-" + str(i), old) for i in range(5000)),
            )
            db.executemany(
                "INSERT INTO intents VALUES(?,'linked',?)",
                ((name, mission) for name in ("wanted", "outside-window")),
            )
        tracemalloc.start()
        try:
            assert app.recover() == ()
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert recorded == ["wanted"]
        assert peak < 2 * 1024 * 1024, (
            "recovery must not fetch all historical linked payloads"
        )
    finally:
        worker.close()
