"""Public-only dollars and search admission survive failures and calendar rollover."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import sqlite3
from types import SimpleNamespace

import pytest

from app.trusted.public_monthly_budget import MonthlyBudget, BudgetedResponses
from app.core.public_rejection import PublicOutputRejected


NOV = datetime(2026, 11, 2, tzinfo=UTC)
DEC = datetime(2026, 12, 1, tzinfo=UTC)


def wallet(tmp_path, lane="sol", clock=lambda: NOV):
    path = tmp_path / (lane + ".sqlite")
    if not path.exists() and lane in {"sol", "research"}:
        MonthlyBudget.initialize(path, lane=lane, project_id="proj_public")
    return MonthlyBudget(path, lane=lane, project_id="proj_public", clock=clock)


def test_lost_attempts_remain_reserved_and_new_month_keeps_history(tmp_path):
    now = [NOV]
    budget = wallet(tmp_path, clock=lambda: now[0])
    for i in range(15):
        assert budget.reserve(str(i))
    assert not budget.reserve("over")
    assert not wallet(tmp_path).reserve("restart")
    now[0] = DEC
    assert not budget.reserve("december")
    with sqlite3.connect(budget.path) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM public_budget_attempts").fetchone()[0]
            == 15
        )
    now[0] = NOV
    with pytest.raises(ValueError, match="clock"):
        budget.reserve("regression")


def test_atomic_reservations_and_idempotent_settlement(tmp_path):
    budget = wallet(tmp_path)
    with ThreadPoolExecutor(max_workers=12) as pool:
        admitted = list(pool.map(lambda i: budget.reserve(str(i)), range(30)))
    assert sum(admitted) == 15
    assert not budget.reserve("0")
    usage = {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}
    ids = [str(i) for i, value in enumerate(admitted) if value]
    budget.settle(ids[0], usage)
    budget.settle(ids[0], usage)
    assert budget.snapshot()["charged_microusd"] == 14_007_500
    assert not budget.reserve("replacement")
    budget.settle(ids[1], usage)
    assert budget.reserve("replacement")


def test_month_boundary_settles_original_month_and_replay_stays_blocked(tmp_path):
    now = [NOV]
    budget = wallet(tmp_path, clock=lambda: now[0])
    assert budget.reserve("across-month")
    now[0] = DEC
    budget.settle("across-month", dict(input_tokens=0, output_tokens=0, total_tokens=0))
    assert not budget.reserve("across-month")
    assert budget.snapshot()["charged_microusd"] == 0


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": True},
        dict(input_tokens=1, output_tokens=2, total_tokens=4),
        dict(input_tokens=-1, output_tokens=1, total_tokens=0),
        dict(input_tokens=200_000, output_tokens=1, total_tokens=200_001),
        dict(input_tokens=1, output_tokens=2049, total_tokens=2050),
    ],
)
def test_unknown_or_impossible_usage_cannot_release_reservation(tmp_path, usage):
    budget = wallet(tmp_path)
    assert budget.reserve("lost")
    budget.settle("lost", usage)
    assert budget.snapshot()["charged_microusd"] == 1_000_000


def test_search_limit_survives_renewal_independent_of_dollars(tmp_path):
    budget = wallet(tmp_path, "research")
    for i in range(100):
        assert budget.reserve(str(i))
        budget.settle(str(i), dict(input_tokens=0, output_tokens=0, total_tokens=0))
    assert budget.snapshot()["charged_microusd"] == 1_100_000
    assert not wallet(tmp_path, "research").reserve("101")
    fresh = wallet(tmp_path, "research", clock=lambda: DEC)
    assert fresh.reserve("next-month")


def test_policy_is_pinned_and_database_errors_fail_closed(tmp_path):
    budget = wallet(tmp_path)
    with pytest.raises(ValueError, match="policy"):
        MonthlyBudget(budget.path, lane="research", project_id="proj_public")
    with pytest.raises(ValueError, match="policy"):
        MonthlyBudget(budget.path, lane="sol", project_id="proj_other")
    with pytest.raises(ValueError):
        wallet(tmp_path, "private")
    with sqlite3.connect(budget.path) as db:
        db.execute("DROP TABLE public_budget_attempts")
    with pytest.raises(sqlite3.Error):
        budget.reserve("broken")


def test_sdk_fence_validates_contract_before_any_paid_call(tmp_path):
    budget = wallet(tmp_path)
    seen = []
    raw = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=1000, output_tokens=100, total_tokens=1100)
    )
    sdk = SimpleNamespace(create=lambda **kwargs: seen.append(kwargs) or raw)
    responses = BudgetedResponses(sdk, budget)
    kwargs = dict(
        model="gpt-6.1-sol",
        input="hello",
        tools=[],
        max_output_tokens=2048,
        store=False,
    )
    assert responses.create(**kwargs) is raw
    assert len(seen) == 1
    assert budget.snapshot()["charged_microusd"] == 7500
    for change in (
        dict(model="other"),
        dict(tools=[{"type": "web_search"}]),
        dict(service_tier="priority"),
        dict(max_output_tokens=9999),
        dict(input="x" * 140_000),
    ):
        with pytest.raises(PublicOutputRejected):
            responses.create(**(kwargs | change))
    assert len(seen) == 1


def test_sdk_failure_and_missing_usage_keep_full_reserve(tmp_path):
    budget = wallet(tmp_path)

    def fail(**kwargs):
        raise TimeoutError("synthetic timeout")

    responses = BudgetedResponses(SimpleNamespace(create=fail), budget)
    kwargs = dict(
        model="gpt-6.1-sol",
        input="hello",
        tools=[],
        max_output_tokens=2048,
        store=False,
    )
    with pytest.raises(TimeoutError):
        responses.create(**kwargs)
    responses.delegate = SimpleNamespace(create=lambda **kwargs: {})
    responses.create(**kwargs)
    assert budget.snapshot()["charged_microusd"] == 2_000_000


def test_wallet_missing_replaced_or_unsafe_state_does_not_replenish(tmp_path):
    with pytest.raises((OSError, ValueError)):
        MonthlyBudget(tmp_path / "missing.sqlite", lane="sol", project_id="proj_public")
    with pytest.raises(ValueError):
        MonthlyBudget("relative.sqlite", lane="sol", project_id="proj_public")
    with pytest.raises(ValueError):
        MonthlyBudget.initialize(
            tmp_path / "bad.sqlite", lane="private", project_id="proj_public"
        )
    budget = wallet(tmp_path)
    with pytest.raises(FileExistsError):
        MonthlyBudget.initialize(budget.path, lane="sol", project_id="proj_public")
    budget.path.chmod(0o644)
    with pytest.raises(ValueError, match="unsafe"):
        wallet(tmp_path)
    budget.path.chmod(0o600)
    linked = tmp_path / "linked.sqlite"
    linked.symlink_to(budget.path)
    with pytest.raises(ValueError, match="path"):
        MonthlyBudget(linked, lane="sol", project_id="proj_public")
    budget.path.rename(tmp_path / "original.sqlite")
    MonthlyBudget.initialize(budget.path, lane="sol", project_id="proj_public")
    with pytest.raises(ValueError, match="replaced"):
        budget.reserve("replacement")


@pytest.mark.parametrize("operation", [None, True, "", "x" * 129])
def test_bad_operation_ids_are_closed(tmp_path, operation):
    with pytest.raises(ValueError):
        wallet(tmp_path).reserve(operation)


def test_naive_clock_and_changed_policy_are_closed(tmp_path):
    budget = wallet(tmp_path, clock=lambda: datetime(2026, 11, 2))
    with pytest.raises(ValueError, match="clock"):
        budget.reserve("naive")
    budget.clock = lambda: NOV
    with sqlite3.connect(budget.path) as db:
        db.execute("UPDATE policy SET value='other'")
    with pytest.raises(ValueError, match="policy"):
        budget.reserve("changed")


def test_sdk_exhaustion_never_reaches_delegate_and_research_is_counted(tmp_path):
    budget = wallet(tmp_path)
    for i in range(15):
        assert budget.reserve(str(i))
    calls = []
    responses = BudgetedResponses(
        SimpleNamespace(create=lambda **kw: calls.append(kw)), budget
    )
    kwargs = dict(
        model="gpt-6.1-sol", input="text", tools=[], max_output_tokens=1, store=False
    )
    with pytest.raises(PublicOutputRejected, match="exhausted"):
        responses.create(**kwargs)
    assert calls == []
    research = wallet(tmp_path, "research")
    responses = BudgetedResponses(
        SimpleNamespace(
            create=lambda **kw: {
                "usage": dict(input_tokens=100, output_tokens=10, total_tokens=110)
            }
        ),
        research,
    )
    kwargs = dict(
        model="gpt-6-luna",
        input="question",
        tools=[{"type": "web_search"}],
        max_tool_calls=1,
        max_output_tokens=1000,
        store=False,
    )
    responses.create(**kwargs)
    assert research.snapshot()["charged_microusd"] == 11069
    for change in (
        dict(tools=None),
        dict(tools=[]),
        dict(tools=[{}, {}]),
        dict(tools=[1]),
        dict(tools=[{"type": "other"}]),
        dict(max_tool_calls=2),
        dict(max_output_tokens=True),
        dict(input=[{"role": "user", "content": []}]),
        dict(input=[1]),
        dict(input={}),
        dict(prompt_cache_retention="12h"),
        dict(store=True),
    ):
        with pytest.raises(PublicOutputRejected):
            responses.create(**(kwargs | change))
    fresh = tmp_path / "fresh"
    fresh.mkdir(mode=0o700)
    responses.budget = wallet(fresh)
    responses.create(
        model="gpt-6.1-sol",
        input=[dict(role="system", content="text"), dict(role="user", content="text")],
        tools=[],
        max_output_tokens=1,
        store=False,
    )


def test_public_worker_requires_budget_before_reading_credential(monkeypatch):
    from app.ops import int24_public_worker as module

    config = dict(
        worker_enabled=True,
        project_id="proj_public",
        organization_id="org-test",
        stop_file="/unused/stop",
    )
    monkeypatch.setattr(module, "_private_config", lambda _: config)

    def forbidden(*args, **kwargs):
        pytest.fail("credential must not be read without a budget")

    monkeypatch.setattr(module, "read_protected_worker_file", forbidden)
    with pytest.raises(ValueError, match="monthly budget"):
        module.main(["--config", "/unused/config"])


def test_real_research_factory_wraps_sdk_and_exhaustion_is_local(tmp_path, monkeypatch):
    from dataclasses import replace
    from app.research.brief import (
        ResearchQuestion,
        ResearchRecordSet,
        build_safe_research_brief,
        ResearchScreeningContext,
    )
    from app.research.provider import (
        default_hosted_search_config,
        HostedSearchTransportError,
    )
    from app.trusted.public_research_transport import PublicResearchTransport
    from app.trusted.public_research import _build_request
    from test_int20_public_research import SDK, raw_response

    path = tmp_path / "research-budget.sqlite"
    MonthlyBudget.initialize(path, lane="research", project_id="proj_public")
    sdk = SDK([raw_response()])
    monkeypatch.setattr(
        "app.trusted.public_research_transport.build_worker_openai_transport",
        lambda **kwargs: SimpleNamespace(_client=sdk),
    )
    transport = PublicResearchTransport.from_protected_file(
        project_id="proj_public", organization_id="org-test", monthly_budget_path=path
    )
    brief = build_safe_research_brief(
        ResearchRecordSet((ResearchQuestion("How do stars form?"),)),
        result_budget=3,
        trusted_config_version="public-research-brief-v1",
        screening=ResearchScreeningContext(),
    ).brief
    request = _build_request(
        brief, replace(default_hosted_search_config(), enabled=True, max_attempts=1)
    )
    transport.call(request, timeout_seconds=1)
    assert len(sdk.calls) == 1
    budget = MonthlyBudget(path, lane="research", project_id="proj_public")
    for i in range(7):
        assert budget.reserve("lost-" + str(i))
    with pytest.raises(HostedSearchTransportError) as caught:
        transport.call(request, timeout_seconds=1)
    assert caught.value.code == "allowance_exhausted"
    assert caught.value.provider_code == "public_monthly_budget_exhausted"
    assert len(sdk.calls) == 1
    transport._client.responses.budget.lane = "sol"
    with pytest.raises(HostedSearchTransportError) as caught:
        transport.call(request, timeout_seconds=1)
    assert caught.value.code == "provider_error"
    assert caught.value.provider_code == "public_budget_contract_invalid"
    assert len(sdk.calls) == 1
