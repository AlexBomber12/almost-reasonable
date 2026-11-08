"""Search count must not stop authorized work before the shared dollar budget."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import os
import sqlite3
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from app.core.public_research_client import PublicResearchClient
from app.core.public_research_profile import load_research_profile
from app.core.store import EventStore
from app.integration.public_window import WindowSpec
from app.trusted.public_monthly_budget import BudgetedResponses
from app.trusted.public_research_transport import PublicResearchTransport
from app.trusted.public_research_worker import build_worker
from app.trusted.research_authority_journal import AuthorityJournal
from app.trusted.shared_budget_service import BudgetServer, RemoteBudget
from app.trusted.shared_monthly_budget import SharedWallet
from app.worker_telemetry.protocol import _budget
from test_agent_research_questions import authority
from test_b05b_public_runtime import _setup
from test_int20_public_research import SDK, raw_response
from test_shared_budget_service import config
from test_shared_monthly_budget import PROJECT, USAGE, sources, wallet


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


def profile_file(folder, *, role="worker", now=None, **changes):
    now = now or datetime.now(UTC) - timedelta(seconds=1)
    path, value = authority(folder, public=True, role=role)
    value.update(
        version="public-research-profile-v4",
        renewal="standing-monthly-utc",
        aggregate_limit=None,
        issued_at=now.isoformat(),
        reviewed_at=now.isoformat(),
        expires_at=datetime.max.replace(tzinfo=UTC).isoformat(),
    )
    value.update(changes)
    path.write_text(json.dumps(value))
    return path


def test_more_than_100_searches_still_share_one_finite_dollar_balance(tmp_path):
    budget = wallet(tmp_path)
    assert budget.reserve("private_sol", "unknown-private")
    for number in range(110):
        operation = str(number)
        assert budget.reserve("research", operation)
        budget.settle("research", operation, USAGE)
    fresh = SharedWallet(budget.path, project_id=PROJECT, clock=budget.clock)
    status = fresh.observe("research")
    assert status["month_attempts"] == 110 and status["search_limit"] is None
    assert status["unresolved_attempts"] == 1
    assert not fresh.reserve("research", "0")
    # The other two lanes can consume the same balance; no search-only credit.
    for number in range(200):
        fresh.reserve("sol" if number % 2 else "private_sol", "lost-" + str(number))
    # A smaller research reservation can use the last fraction of a dollar.
    assert fresh.reserve("research", "remaining-capacity")
    before = fresh.observe("research")
    assert before["charged_microusd"] <= 40_000_000
    assert not fresh.reserve("research", "over-budget")
    assert fresh.observe("research") == before


@pytest.mark.parametrize("role", ["application", "worker"])
def test_explicit_money_only_profile_keeps_history_replay_and_question_fences(
    tmp_path, role
):
    path = profile_file(tmp_path, role=role)
    profile = load_research_profile(path, role=role)
    journal_path = tmp_path / "authority.sqlite"
    journal_path.touch(mode=0o600)
    journal = AuthorityJournal(journal_path)
    journal.activate(profile)
    for number in range(101):
        question = "What shapes mountain range number " + str(number) + "?"
        topic = profile.topic(question)
        assert journal.reserve(profile, str(number), topic.topic_id, question=question)
    fresh = AuthorityJournal(journal_path)
    assert not fresh.aggregate_exhausted(profile, "next")
    assert not fresh.aggregate_exhausted(profile, "0")
    question = "What shapes mountain range number 0?"
    assert not fresh.reserve(
        profile, "repeat-question", profile.topic(question).topic_id, question=question
    )
    with sqlite3.connect(journal_path) as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 101


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "public-research-profile-v3"},
        {"aggregate_limit": 100},
        {"aggregate_limit": False},
        {"aggregate_limit": "none"},
        {"purpose": "private-conversation-public-research-v1"},
        {
            "question_policy": {
                "mode": "agent_generated_public_questions",
                "max_question_bytes": 512,
                "per_question_limit": 101,
            }
        },
    ],
)
def test_money_only_is_an_explicit_public_standing_authority(tmp_path, changes):
    path = profile_file(tmp_path, **changes)
    with pytest.raises(ValueError):
        load_research_profile(path, role="worker")


def test_runtime_still_offers_research_after_100_historical_questions(tmp_path):
    app, *_ = _setup(
        tmp_path, {"decision": "no_action", "reason_category": "not_warranted"}
    )
    folder = tmp_path / "profile"
    folder.mkdir(mode=0o700)
    path = profile_file(folder, role="application", now=app.clock.now())
    profile = load_research_profile(path, role="application")
    with app.journal.transaction() as db:
        db.executemany(
            "INSERT INTO public_research_ops VALUES(?,?,?,?,?,NULL)",
            [
                (
                    str(n),
                    "research-" + str(n),
                    "Question " + str(n),
                    "old-profile",
                    "failed",
                )
                for n in range(100)
            ],
        )
    assert app.available_topics(profile) == ("agent-public-question",)
    spec = WindowSpec(
        "money-window", app.clock.now() + timedelta(hours=1), 12, 0, 0, 3600
    )
    assert (
        spec.maximum_work(research_limit=None)["research_operations_separate_authority"]
        == 12
    )


@contextmanager
def broker(tmp_path):
    folder = tmp_path / "b"
    folder.mkdir(mode=0o700)
    path = folder / "shared.sqlite"
    SharedWallet.initialize(path, project_id=PROJECT, sources=sources(folder))
    ledger = SharedWallet(path, project_id=PROJECT)
    server = BudgetServer(folder / "b.sock", ledger, {os.geteuid(): "research"})
    server.start()
    stop = Event()
    thread = Thread(target=server.serve, kwargs={"should_stop": stop.is_set})
    thread.start()
    try:
        route = config(tmp_path, folder / "b.sock", lane="research")
        yield ledger, route
    finally:
        stop.set()
        thread.join(5)
        server.close()
        assert not thread.is_alive()


def worker_args(tmp_path, route):
    path = profile_file(tmp_path)
    provider = tmp_path / "provider.sqlite"
    EventStore(provider)
    journal = tmp_path / "authority.sqlite"
    journal.touch(mode=0o600)
    stop = tmp_path / "stop.json"
    stop.write_text(json.dumps({"version": "public-research-stop-v1", "stop": False}))
    stop.chmod(0o400)
    return dict(
        socket_path=tmp_path / "r.sock",
        provider_database=provider,
        profile_path=path,
        authority_journal=journal,
        stop_path=stop,
        project_id=PROJECT,
        organization_id="org_synthetic",
        monthly_budget_path=route,
    )


def test_native_search_and_budget_channels_admit_101_then_deny_dollar_exhaustion(
    tmp_path,
):
    with broker(tmp_path) as (ledger, route):
        remote = RemoteBudget(route, lane="research", project_id=PROJECT)
        for number in range(100):
            assert remote.reserve("historical-" + str(number))
            remote.settle("historical-" + str(number), USAGE)
        assert _budget(remote.observation())["search_limit"] is None
        args = worker_args(tmp_path, route)
        profile = load_research_profile(args["profile_path"], role="worker")
        sdk = SDK([raw_response()])
        transport = PublicResearchTransport(
            SimpleNamespace(responses=BudgetedResponses(sdk, remote))
        )
        server = build_worker(**args, transport=transport)
        server.start()
        stop = Event()
        thread = Thread(target=server.serve, kwargs={"should_stop": stop.is_set})
        thread.start()
        try:
            client = PublicResearchClient(
                args["socket_path"], expected_worker_uid=os.geteuid()
            )
            assert (
                client.execute(profile.request("next", "How do tectonic plates move?"))[
                    "outcome"
                ]
                == "completed"
            )
            assert len(sdk.calls) == 1
            for number in range(130):
                ledger.reserve("sol", "unknown-" + str(number))
            result = client.execute(
                profile.request("unfunded", "Why do stars form in clusters?")
            )
            assert result["outcome"] == "allowance_exhausted"
            assert len(sdk.calls) == 1
        finally:
            stop.set()
            thread.join(5)
            server.close()
            assert not thread.is_alive()


@pytest.mark.parametrize("mode", ["missing", "legacy", "other-project", "offline"])
def test_money_only_worker_fails_before_credentials_without_the_shared_wallet(
    tmp_path, mode
):
    with broker(tmp_path) as (_, route):
        args = worker_args(tmp_path, route)
        if mode == "missing":
            args["monthly_budget_path"] = None
        elif mode == "legacy":
            args["monthly_budget_path"] = tmp_path / "legacy.sqlite"
        elif mode == "other-project":
            args["project_id"] = "proj_other"
        else:
            value = json.loads(route.read_text())
            value["socket_path"] = str(tmp_path / "missing.sock")
            route.chmod(0o600)
            route.write_text(json.dumps(value))
            route.chmod(0o400)
        with pytest.raises(ValueError):
            build_worker(**args, transport=PublicResearchTransport(SDK([])))


def test_money_only_worker_rejects_a_still_capped_broker(tmp_path, monkeypatch):
    with broker(tmp_path) as (ledger, route):
        original = ledger._status
        monkeypatch.setattr(
            ledger, "_status", lambda *args: original(*args) | {"search_limit": 100}
        )
        with pytest.raises(ValueError, match="still has a search count limit"):
            build_worker(
                **worker_args(tmp_path, route),
                transport=PublicResearchTransport(SDK([])),
            )


def test_telemetry_distinguishes_legacy_count_caps_from_shared_money_only(tmp_path):
    with broker(tmp_path) as (_, route):
        observation = RemoteBudget(
            route, lane="research", project_id=PROJECT
        ).observation()
        assert _budget(observation)["search_limit"] is None
        assert _budget(observation | {"search_limit": 100})["search_limit"] == 100
        with pytest.raises(ValueError):
            _budget(observation | {"scope": "conservative_monthly_wallet"})
