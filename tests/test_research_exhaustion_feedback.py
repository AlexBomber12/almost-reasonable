"""Authoritative exhaustion is durable feedback, never a grant renewal."""

from datetime import UTC, datetime, timedelta
import json
import os
from threading import Event, Thread

import pytest

from app.core.public_research_client import PublicResearchClient
from app.core.public_research_profile import load_research_profile
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.trusted.public_research import PublicResearchBoundary, PublicResearchServer
from app.trusted.public_research import _response, decode_public_response
from app.trusted.public_research_transport import PublicResearchTransport
from app.trusted.public_monthly_budget import BudgetedResponses, MonthlyBudget
from app.trusted.research_authority_journal import AuthorityJournal
from app.trusted.openai_worker_server import WorkerServerConfig
from test_agent_research_questions import authority
from test_b05a_profile import NOW
from test_b05b_public_runtime import _setup
from test_int20_public_research import SDK, raw_response
from test_int24_runtime import run_once
from test_public_experience import wake


@pytest.mark.parametrize("public", [False, True])
def test_native_exhaustion_is_distinct_from_a_duplicate_and_makes_no_extra_call(
    tmp_path, public
):
    path, _ = authority(tmp_path, public=public, aggregate=1)
    profile = load_research_profile(path, role="worker")
    journal_path = tmp_path / "authority.sqlite"
    journal_path.touch(mode=0o600)
    journal = AuthorityJournal(journal_path)
    store = ProviderStateStore(EventStore(tmp_path / "provider.db"))
    sdk = SDK([raw_response()])
    worker = PublicResearchBoundary(
        store=store,
        transport=PublicResearchTransport(sdk),
        enabled=True,
        utcnow=lambda: NOW,
    )
    worker.install_profile(profile, profile_path=path, journal=journal)
    assert (
        worker.execute(
            profile.request("one", "How do tectonic plates move?"),
            peer_uid=os.geteuid(),
        )["outcome"]
        == "completed"
    )
    result = worker.execute(
        profile.request("two", "Why do stars form in clusters?"), peer_uid=os.geteuid()
    )
    assert result["outcome"] == "allowance_exhausted"
    assert result["attempts"] == 0 and result["report"] is None
    assert len(sdk.calls) == store.usage_attempt_count() == 1
    assert (
        worker.execute(
            profile.request("one", "How do tectonic plates move?"),
            peer_uid=os.geteuid(),
        )["outcome"]
        == "duplicate_operation"
    )


@pytest.mark.parametrize(
    "attempts,usage",
    [(1, None), (0, {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})],
)
def test_exhaustion_cannot_claim_a_provider_attempt_or_rejected_usage(attempts, usage):
    with pytest.raises(ValueError, match="exhausted"):
        decode_public_response(
            json.dumps(_response("allowance_exhausted", attempts, usage=usage)).encode()
        )


@pytest.mark.parametrize("limit", ["authority", "monthly_dollars", "monthly_searches"])
def test_external_consumption_removes_research_offer_but_preserves_existing_report(
    tmp_path,
    limit,
):
    question = "How do tectonic plates move?"
    app, inference, sdk, http, clock = _setup(
        tmp_path, {"decision": "research", "topic_id": question}
    )
    folder = tmp_path / "research"
    folder.mkdir(mode=0o700)
    paths = []
    for role in ("application", "worker"):
        path, value = authority(folder, public=True, role=role, aggregate=3)
        value.update(
            issued_at=clock.now().isoformat(),
            reviewed_at=clock.now().isoformat(),
            expires_at=(clock.now() + timedelta(days=1)).isoformat(),
        )
        if limit != "authority":
            value.update(
                version="public-research-profile-v3",
                renewal="standing-monthly-utc",
                aggregate_limit=100,
                expires_at=datetime.max.replace(tzinfo=UTC).isoformat(),
            )
        path.write_text(json.dumps(value))
        paths.append(path)
    app.research_enabled, app.research_scope, app.research_profile_path = (
        True,
        "own-questions-test",
        paths[0],
    )
    app.research_client = PublicResearchClient(
        folder / "search.sock", expected_worker_uid=os.geteuid()
    )
    journal_path = folder / "authority.sqlite"
    journal_path.touch(mode=0o600)
    search_sdk = SDK([raw_response(), raw_response(), raw_response()])
    transport_client = search_sdk
    budget = None
    if limit != "authority":
        from types import SimpleNamespace

        wallet = folder / "monthly.sqlite"
        MonthlyBudget.initialize(wallet, lane="research", project_id="proj_public")
        budget = MonthlyBudget(
            wallet, lane="research", project_id="proj_public", clock=clock.now
        )
        transport_client = SimpleNamespace(
            responses=BudgetedResponses(search_sdk, budget)
        )
    store = ProviderStateStore(EventStore(folder / "provider.db"))
    boundary = PublicResearchBoundary(
        store=store,
        transport=PublicResearchTransport(transport_client),
        enabled=True,
        utcnow=clock.now,
    )
    profile = load_research_profile(paths[1], role="worker")
    boundary.install_profile(
        profile,
        profile_path=paths[1],
        journal=AuthorityJournal(journal_path, clock=clock.now),
    )
    search = PublicResearchServer(
        WorkerServerConfig(folder / "search.sock", frozenset({os.geteuid()})), boundary
    )
    search.start()
    stop = Event()
    thread = Thread(target=lambda: search.serve(should_stop=stop.is_set), daemon=True)
    thread.start()
    try:
        run_once(app, inference)
        if limit == "authority":
            for operation, public_question in (
                ("operator", "Why do stars form in clusters?"),
                ("operator-two", "How does photosynthesis work?"),
            ):
                assert (
                    boundary.execute(
                        profile.request(operation, public_question),
                        peer_uid=os.geteuid(),
                    )["outcome"]
                    == "completed"
                )
        else:
            for index in range(7 if limit == "monthly_dollars" else 99):
                operation = "held-" + str(index)
                assert budget.reserve(operation)
                if limit == "monthly_searches":
                    budget.settle(
                        operation, dict(input_tokens=0, output_tokens=0, total_tokens=0)
                    )
            before = budget.snapshot()
        sdk.proposal = {"decision": "research", "topic_id": "How does rainfall form?"}
        wake(app, inference, clock, "exhausted-search")
        sdk.proposal = {"decision": "no_action", "reason_category": "other"}
        wake(app, inference, clock, "after-exhaustion")
        context = json.loads(sdk.calls[-1]["input"][1]["content"])
        assert context["permitted_research_topics"] == []
        assert (
            json.loads(context["prior_untrusted_public_evidence"][0])["question"]
            == question
        )
        assert len(search_sdk.calls) == (3 if limit == "authority" else 1)
        if budget is not None:
            assert budget.snapshot() == before
            with app.journal.transaction() as db:
                operation = db.execute(
                    "SELECT research_id FROM public_research_ops WHERE phase='failed'"
                ).fetchone()[0]
            recorded = store.usage_attempt(operation)
            assert recorded.outcome == "allowance_exhausted"
            assert recorded.provider_error_code == "public_monthly_budget_exhausted"
            assert recorded.total_tokens is None
            assert not store.is_spend_suspended()
        assert not http.calls
        with app.journal.transaction() as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM public_research_ops WHERE phase='failed' AND evidence=?",
                    (json.dumps({"outcome": "allowance_exhausted"}),),
                ).fetchone()[0]
                == 1
            )
    finally:
        stop.set()
        thread.join(5)
        search.close()
        inference.close()
