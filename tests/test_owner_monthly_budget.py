"""Read-only wallet observation preserves charges, unknown holds and clocks."""

from datetime import UTC, datetime
import json
import os
from threading import Thread

import pytest

from app.trusted.public_monthly_budget import MonthlyBudget
from app.trusted.worker_telemetry import WorkerTelemetryServer
from app.owner_control.worker_telemetry import WorkerTelemetryClient
from app.worker_telemetry.protocol import decode_diagnostics
from test_worker_telemetry import sources


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


@pytest.fixture
def wallet(tmp_path):
    directory = tmp_path / "wallet"
    directory.mkdir(mode=0o700)
    path = directory / "monthly.sqlite"
    MonthlyBudget.initialize(
        path, lane="research", project_id="proj_SYNTHETIC_PRIVATE_CANARY"
    )
    clock = {"now": datetime(2026, 10, 31, 12, tzinfo=UTC)}
    budget = MonthlyBudget(
        path,
        lane="research",
        project_id="proj_SYNTHETIC_PRIVATE_CANARY",
        clock=lambda: clock["now"],
    )
    assert budget.reserve("OLD_UNKNOWN_OPERATION_CANARY")
    clock["now"] = datetime(2026, 11, 5, 12, tzinfo=UTC)
    assert budget.reserve("SETTLED_OPERATION_CANARY")
    budget.settle(
        "SETTLED_OPERATION_CANARY",
        {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
    )
    return path, clock, budget


def test_wallet_diagnostic_keeps_old_unknown_hold_and_does_not_advance_the_wallet(
    wallet,
):
    from app.trusted.budget_telemetry import observe_budget

    path, clock, _ = wallet
    before = path.read_bytes()
    result = observe_budget(path, clock=lambda: clock["now"])
    assert result == {
        "available": True,
        "scope": "conservative_monthly_wallet",
        "billing_authority": False,
        "collected_at": "2026-11-05T12:00:00.000000Z",
        "month": "2026-11",
        "lane": "research",
        "limit_microusd": 5_000_000,
        "settled_microusd": 11_069,
        "unresolved_microusd": 700_000,
        "unresolved_attempts": 1,
        "remaining_microusd": 4_288_931,
        "month_attempts": 1,
        "search_limit": 100,
        "next_reservation_microusd": 700_000,
        "admission_available": True,
    }
    assert path.read_bytes() == before
    assert "CANARY" not in json.dumps(result)


def test_wallet_exhaustion_is_observed_without_releasing_unknown_reservations(wallet):
    from app.trusted.budget_telemetry import observe_budget

    path, clock, budget = wallet
    for index in range(6):
        assert budget.reserve("unknown-" + str(index))
    result = observe_budget(path, clock=lambda: clock["now"])
    assert not result["admission_available"] and result["unresolved_attempts"] == 7
    assert result["unresolved_microusd"] == 4_900_000
    assert result["month_attempts"] == 7
    assert not budget.reserve("still-denied")


def test_diagnostics_cross_the_existing_authenticated_socket_without_identity_leaks(
    tmp_path, wallet
):
    provider, stop, socket = sources(tmp_path)
    path, clock, _ = wallet
    # Server clock is real; wallet last_seen is deliberately in its past.
    server = WorkerTelemetryServer(
        socket, provider, stop, frozenset({os.getuid()}), monthly_budget_path=path
    )
    server.start()
    thread = Thread(target=server.serve_once)
    thread.start()
    try:
        result = WorkerTelemetryClient(socket, os.getuid()).read_diagnostics()
        assert result["available"] and result["budget"]["available"]
        assert result["budget"]["lane"] == "research"
        assert "CANARY" not in json.dumps(result)
    finally:
        thread.join(5)
        server.close()
    assert not thread.is_alive()


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "policy",
        "empty_policy",
        "duplicate_policy",
        "clock",
        "permissions",
        "symlink",
        "broken",
    ],
)
def test_wallet_unavailability_is_explicit_and_never_replenishes_or_creates_state(
    wallet, case
):
    from app.trusted.budget_telemetry import observe_budget
    import sqlite3

    path, clock, _ = wallet
    if case == "missing":
        path = path.with_name("missing.sqlite")
    elif case == "policy":
        with sqlite3.connect(path) as db:
            db.execute("UPDATE policy SET value='[]'")
    elif case == "empty_policy":
        with sqlite3.connect(path) as db:
            db.execute("DELETE FROM policy")
    elif case == "duplicate_policy":
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO policy SELECT * FROM policy")
    elif case == "clock":
        clock["now"] = datetime(2026, 10, 1, tzinfo=UTC)
    elif case == "permissions":
        path.chmod(0o644)
    elif case == "symlink":
        link = path.with_name("link.sqlite")
        link.symlink_to(path)
        path = link
    else:
        path.write_bytes(b"PRIVATE_CORRUPT_CANARY")
    existed = path.exists()
    assert observe_budget(path, clock=lambda: clock["now"]) == {"available": False}
    assert path.exists() == existed


def test_budget_extension_is_closed_and_optional_for_legacy_workers(wallet):
    from app.trusted.budget_telemetry import observe_budget

    path, clock, _ = wallet
    frame = {"version": "worker-diagnostics-v1", "available": True, "rows": []}
    assert decode_diagnostics(json.dumps(frame).encode()) == {
        "available": True,
        "rows": [],
    }
    budget = observe_budget(path, clock=lambda: clock["now"])
    assert (
        decode_diagnostics(json.dumps(frame | {"budget": budget}).encode())["budget"]
        == budget
    )
    for altered in (
        budget | {"project": "PRIVATE_CANARY"},
        budget | {"month_attempts": True},
        budget | {"remaining_microusd": -1},
        budget | {"month": "invalid"},
        budget | {"billing_authority": True},
        budget | {"remaining_microusd": 1},
        budget | {"admission_available": False},
        {"available": 0},
    ):
        with pytest.raises(ValueError):
            decode_diagnostics(json.dumps(frame | {"budget": altered}).encode())


def test_search_count_can_exhaust_before_the_dollar_limit(wallet):
    from app.trusted.budget_telemetry import observe_budget

    path, clock, budget = wallet
    for index in range(99):
        operation = "bounded-search-" + str(index)
        assert budget.reserve(operation)
        budget.settle(
            operation, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        )
    result = observe_budget(path, clock=lambda: clock["now"])
    assert result["month_attempts"] == 100 and result["remaining_microusd"] > 700_000
    assert not result["admission_available"]
    assert result["unresolved_attempts"] == 1


@pytest.mark.parametrize(
    "lane,limit", [("sol", 15_000_000), ("private_sol", 10_000_000)]
)
def test_other_wallet_lanes_remain_distinct_and_have_no_search_allowance(
    tmp_path, lane, limit
):
    from app.trusted.budget_telemetry import observe_budget

    directory = tmp_path / "wallet"
    directory.mkdir(mode=0o700)
    path = directory / "budget.sqlite"
    MonthlyBudget.initialize(path, lane=lane, project_id="proj_SYNTHETIC_CANARY")
    result = observe_budget(path)
    assert result["available"] and result["lane"] == lane
    assert result["limit_microusd"] == result["remaining_microusd"] == limit
    assert result["search_limit"] is None
    frame = {
        "version": "worker-diagnostics-v1",
        "available": True,
        "rows": [],
        "budget": result,
    }
    assert decode_diagnostics(json.dumps(frame).encode())["budget"] == result


def test_unavailable_wallet_does_not_hide_provider_diagnostics(tmp_path):
    provider, stop, socket = sources(tmp_path)
    server = WorkerTelemetryServer(
        socket,
        provider,
        stop,
        frozenset({os.getuid()}),
        monthly_budget_path=provider.with_name("missing.sqlite"),
    )
    server.start()
    thread = Thread(target=server.serve_once)
    thread.start()
    try:
        result = WorkerTelemetryClient(socket, os.getuid()).read_diagnostics()
        assert result == {"available": True, "rows": [], "budget": {"available": False}}
    finally:
        thread.join(5)
        server.close()


@pytest.mark.parametrize(
    "budget_path", ["relative", __import__("pathlib").Path("relative")]
)
def test_telemetry_configuration_never_accepts_an_unbound_wallet_path(
    tmp_path, budget_path
):
    provider, stop, socket = sources(tmp_path)
    with pytest.raises(ValueError, match="monthly budget"):
        WorkerTelemetryServer(
            socket,
            provider,
            stop,
            frozenset({os.getuid()}),
            monthly_budget_path=budget_path,
        )


def test_wallet_read_rejects_replacement_and_naive_clock(wallet, monkeypatch):
    from app.trusted import budget_telemetry

    path, clock, _ = wallet
    assert budget_telemetry.observe_budget(
        path, clock=lambda: datetime(2026, 11, 5)
    ) == {"available": False}
    original, calls = budget_telemetry.protected_state_identity, []

    def replaced(path):
        identity = original(path)
        calls.append(True)
        return (
            identity if len(calls) == 1 else (identity[0], identity[1], identity[2] + 1)
        )

    monkeypatch.setattr(budget_telemetry, "protected_state_identity", replaced)
    assert budget_telemetry.observe_budget(path, clock=lambda: clock["now"]) == {
        "available": False
    }
    assert len(calls) == 2
