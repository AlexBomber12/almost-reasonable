"""One project budget must fence all three lanes together without losing holds."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import sqlite3

import pytest

from app.trusted.public_monthly_budget import MonthlyBudget
from app.trusted.shared_monthly_budget import SharedWallet


NOV = datetime(2026, 11, 8, tzinfo=UTC)
DEC = datetime(2026, 12, 1, tzinfo=UTC)
PROJECT = "proj_synthetic"
LANES = ("sol", "research", "private_sol")
USAGE = dict(input_tokens=1000, output_tokens=100, total_tokens=1100)


def sources(tmp_path):
    result = {}
    for lane in LANES:
        path = tmp_path / (lane + ".sqlite")
        MonthlyBudget.initialize(path, lane=lane, project_id=PROJECT)
        result[lane] = path
    return result


def wallet(tmp_path, *, clock=lambda: NOV):
    path = tmp_path / "shared.sqlite"
    SharedWallet.initialize(path, project_id=PROJECT, sources=sources(tmp_path))
    return SharedWallet(path, project_id=PROJECT, clock=clock)


def test_shared_admission_is_atomic_across_lanes_and_restarts(tmp_path):
    budget = wallet(tmp_path)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(
            pool.map(lambda i: budget.reserve(LANES[i % 3], str(i)), range(180))
        )
    used = budget.snapshot("sol")["charged_microusd"]
    assert 39_600_000 <= used <= 40_000_000
    fresh = SharedWallet(budget.path, project_id=PROJECT, clock=lambda: NOV)
    assert not fresh.reserve("sol", "after-restart")
    assert fresh.snapshot("private_sol")["charged_microusd"] == used
    assert sum(results) < 180


def test_import_preserves_old_charges_unknown_holds_searches_and_watermark(tmp_path):
    old = sources(tmp_path)
    for lane, path in old.items():
        budget = MonthlyBudget(path, lane=lane, project_id=PROJECT, clock=lambda: NOV)
        assert budget.reserve("unknown")
        assert budget.reserve("completed")
        budget.settle("completed", USAGE)
    path = tmp_path / "shared.sqlite"
    SharedWallet.initialize(path, project_id=PROJECT, sources=old)
    budget = SharedWallet(path, project_id=PROJECT, clock=lambda: NOV)
    status = budget.snapshot("research")
    assert status["charged_microusd"] == 2_700_000 + 7_500 * 2 + 11_690
    assert status["unresolved_attempts"] == 3
    assert status["month_attempts"] == 2
    assert not budget.reserve("sol", "unknown")
    assert budget.reserve("sol", "new")
    with pytest.raises(FileExistsError):
        SharedWallet.initialize(path, project_id=PROJECT, sources=old)
    with pytest.raises(ValueError, match="clock"):
        SharedWallet(
            path, project_id=PROJECT, clock=lambda: NOV.replace(day=7)
        ).snapshot("sol")
    for lane, source in old.items():
        with sqlite3.connect(source) as db:
            assert (
                db.execute("SELECT COUNT(*) FROM public_budget_attempts").fetchone()[0]
                == 2
            )


def test_settlement_only_releases_same_lane_and_uses_new_pinned_upper_prices(tmp_path):
    budget = wallet(tmp_path)
    assert budget.reserve("sol", "same-id")
    budget.settle("private_sol", "same-id", USAGE)
    assert budget.snapshot("sol")["unresolved_attempts"] == 1
    budget.settle("sol", "same-id", USAGE)
    assert budget.snapshot("private_sol")["charged_microusd"] == 3850
    budget.settle(
        "sol", "same-id", dict(input_tokens=0, output_tokens=0, total_tokens=0)
    )
    assert budget.snapshot("sol")["charged_microusd"] == 3850
    assert not budget.reserve("sol", "same-id")


def test_known_cached_input_uses_cached_ceiling_but_bad_details_retain_hold(tmp_path):
    budget = wallet(tmp_path)
    assert budget.reserve("sol", "cached")
    budget.settle("sol", "cached", USAGE | dict(cached_input_tokens=1000))
    assert budget.snapshot("sol")["charged_microusd"] == 1210
    for index, cached in enumerate((True, -1, 1001, None)):
        operation = "bad-cache-" + str(index)
        assert budget.reserve("sol", operation)
        budget.settle("sol", operation, USAGE | dict(cached_input_tokens=cached))
    assert budget.snapshot("sol")["charged_microusd"] == 1_601_210


def test_search_count_and_unknown_holds_survive_month_rollover(tmp_path):
    now = [NOV]
    budget = wallet(tmp_path, clock=lambda: now[0])
    assert budget.reserve("private_sol", "lost")
    for i in range(100):
        assert budget.reserve("research", str(i))
        budget.settle("research", str(i), USAGE)
    assert budget.reserve("research", "101")
    budget.settle("research", "101", USAGE)
    assert budget.reserve("sol", "still-allowed")
    now[0] = DEC
    status = budget.snapshot("research")
    assert status["charged_microusd"] == 800_000
    assert status["unresolved_attempts"] == 2
    assert status["month_attempts"] == 0
    assert budget.reserve("research", "december")
    budget.settle("sol", "still-allowed", USAGE)
    assert budget.snapshot("sol")["charged_microusd"] == 723850


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": True},
        dict(input_tokens=1, output_tokens=2, total_tokens=4),
        dict(input_tokens=200000, output_tokens=0, total_tokens=200000),
        dict(input_tokens=1, output_tokens=2049, total_tokens=2050),
    ],
)
def test_unknown_or_impossible_usage_cannot_release_reserve(tmp_path, usage):
    budget = wallet(tmp_path)
    assert budget.reserve("sol", "lost")
    budget.settle("sol", "lost", usage)
    assert budget.snapshot("sol")["charged_microusd"] == 400000


def test_policy_source_and_lane_tampering_fail_closed(tmp_path):
    old = sources(tmp_path)
    with pytest.raises(ValueError):
        SharedWallet.initialize(
            tmp_path / "missing.sqlite", project_id=PROJECT, sources={}
        )
    with pytest.raises(ValueError):
        SharedWallet.initialize(
            tmp_path / "wrong.sqlite", project_id="proj_other", sources=old
        )
    path = tmp_path / "shared.sqlite"
    SharedWallet.initialize(path, project_id=PROJECT, sources=old)
    with pytest.raises(ValueError):
        SharedWallet(path, project_id="proj_other")
    budget = SharedWallet(path, project_id=PROJECT, clock=lambda: NOV)
    with pytest.raises(ValueError):
        budget.reserve("unknown", "op")
    with pytest.raises(ValueError):
        budget.reserve("sol", "")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE policy SET value='{}'")
    with pytest.raises(ValueError):
        budget.snapshot("sol")


def test_invalid_sources_clock_and_file_replacement_cannot_replenish(tmp_path):
    old = sources(tmp_path)
    with pytest.raises(ValueError):
        SharedWallet.initialize(
            tmp_path / "bad.sqlite", project_id="invalid", sources=old
        )
    prior = MonthlyBudget(old["sol"], lane="sol", project_id=PROJECT, clock=lambda: NOV)
    assert prior.reserve("bad-month")
    with sqlite3.connect(old["sol"]) as db:
        db.execute("UPDATE public_budget_attempts SET month='invalid-month'")
    with pytest.raises(ValueError, match="source"):
        SharedWallet.initialize(
            tmp_path / "bad.sqlite", project_id=PROJECT, sources=old
        )
    with sqlite3.connect(old["sol"]) as db:
        db.execute("UPDATE public_budget_attempts SET month='2026-11'")
    path = tmp_path / "shared.sqlite"
    SharedWallet.initialize(path, project_id=PROJECT, sources=old)
    budget = SharedWallet(
        path, project_id=PROJECT, clock=lambda: NOV.replace(tzinfo=None)
    )
    with pytest.raises(ValueError, match="naive"):
        budget.snapshot("sol")
    path.rename(path.with_suffix(".backup"))
    path.write_bytes(path.with_suffix(".backup").read_bytes())
    path.chmod(0o600)
    with pytest.raises(ValueError, match="replaced"):
        budget.snapshot("sol")
