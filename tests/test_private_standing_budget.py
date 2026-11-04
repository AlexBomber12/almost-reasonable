"""Standing private authority and a separate, durable $10 UTC-month wallet."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import os
import sqlite3
from threading import Thread
from types import SimpleNamespace

import pytest

from app.cognition.conversation_context import MemoryModelRequest
from app.core.openai_worker_client import OpenAIWorkerClient
from app.core.store import EventStore
from app.ops import private_pilot_ops
from app.trusted import private_pilot_worker
from app.trusted.openai_transport import OpenAIResponsesTransport
from app.trusted.openai_transport import OpenAITransportError
from app.trusted.private_budget import PrivateBudgetedResponses, available
from app.trusted.openai_worker_protocol import InferenceRequest
from app.trusted.private_pilot import (
    MAX_OPERATING_OPERATIONS,
    PilotAllowance,
    PilotAuthorityError,
    load_activation,
)
from app.trusted.public_monthly_budget import MonthlyBudget
from test_int22_controls import _stop
from test_int22_pilot import _rewrite
from test_openai_worker_protocol import model_request
from test_private_operating_grant import NOW, grant


DEC = datetime(2026, 12, 1, tzinfo=UTC)


def standing(tmp_path):
    path, value, old = grant(tmp_path)
    value.update(
        version="private-standing-grant-v1",
        pilot_id="synthetic-standing-private",
        model="gpt-6.1-sol",
        expires_at=None,
        max_operations=MAX_OPERATING_OPERATIONS,
    )
    _rewrite(path, value)
    return path, value, old


def private_wallet(tmp_path, clock=lambda: NOW):
    path = tmp_path / "private-budget.sqlite"
    if not path.exists():
        MonthlyBudget.initialize(path, lane="private_sol", project_id="proj_synthetic")
    return MonthlyBudget(
        path, lane="private_sol", project_id="proj_synthetic", clock=clock
    )


def test_standing_is_explicit_without_a_calendar_expiry_or_operation_refill(tmp_path):
    path, value, old = standing(tmp_path)
    authority = load_activation(path, now=NOW)
    assert authority.standing and authority.ordinary and authority.expires_at is None
    assert load_activation(path, now=NOW + timedelta(days=800)) == authority
    with pytest.raises(PilotAuthorityError):
        load_activation(path, now=NOW - timedelta(seconds=1))
    ledger = tmp_path / "allowance.db"
    prior = PilotAllowance.initialize(ledger, old)
    used = "reply:" + "a" * 64
    assert prior.reserve(used)
    private_pilot_ops.replace(path, ledger)
    allowance = PilotAllowance(ledger, authority)
    assert allowance.current(NOW + timedelta(days=800))
    assert not allowance.current(NOW)
    assert not prior.current(NOW + timedelta(days=800))
    assert not allowance.reserve(used)
    assert allowance.reserve("reply:" + "b" * 64)
    assert allowance.remaining() == MAX_OPERATING_OPERATIONS - 1
    with pytest.raises(PilotAuthorityError):
        private_pilot_ops.replace(path, ledger)
    with sqlite3.connect(ledger) as db:
        assert db.execute("SELECT count(*) FROM operating_consumed").fetchone() == (2,)
    assert private_pilot_ops.plan(path, now=DEC)["expires_at"] is None
    assert private_pilot_ops.plan(path, now=DEC)["standing"] is True
    # Existing finite grants still reject null expiry; no implicit conversion.
    value["version"] = "private-operating-grant-v1"
    _rewrite(path, value)
    with pytest.raises(PilotAuthorityError):
        load_activation(path, now=NOW)


@pytest.mark.parametrize(
    "change",
    [
        {"model": "gpt-6-luna"},
        {"expires_at": "2099-01-01T00:00:00Z"},
        {"max_operations": 936},
        {"budget_optional": True},
    ],
)
def test_standing_cannot_be_confused_with_a_finite_or_unbudgeted_grant(
    tmp_path, change
):
    path, value, _old = standing(tmp_path)
    _rewrite(path, value | change)
    with pytest.raises(PilotAuthorityError):
        load_activation(path, now=NOW)


def test_private_wallet_exhausts_atomically_and_is_separate_from_public(tmp_path):
    wallet = private_wallet(tmp_path)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda i: wallet.reserve(str(i)), range(25)))
    assert sum(results) == 10
    assert wallet.snapshot()["limit_microusd"] == 10_000_000
    assert not private_wallet(tmp_path).reserve("restart")
    with pytest.raises(ValueError, match="policy"):
        MonthlyBudget(wallet.path, lane="sol", project_id="proj_synthetic")
    with pytest.raises(ValueError, match="policy"):
        MonthlyBudget(wallet.path, lane="private_sol", project_id="proj_other")
    with pytest.raises(FileExistsError):
        MonthlyBudget.initialize(
            wallet.path, lane="private_sol", project_id="proj_synthetic"
        )
    # Unknown holds survive a month boundary; never spend another $10 over them.
    future = private_wallet(tmp_path, clock=lambda: DEC)
    assert not future.reserve("december-unknown")
    assert future.snapshot()["charged_microusd"] == 10_000_000


def test_settled_private_spend_resets_next_month_without_resetting_ids(tmp_path):
    now = [NOW]
    wallet = private_wallet(tmp_path, clock=lambda: now[0])
    for index in range(11):
        assert wallet.reserve(str(index))
        wallet.settle(
            str(index),
            dict(input_tokens=133_120, output_tokens=2048, total_tokens=135_168),
        )
    assert not wallet.reserve("over")
    now[0] = DEC
    assert wallet.reserve("december")
    assert not wallet.reserve("0")
    assert wallet.snapshot()["charged_microusd"] == 1_000_000
    now[0] = NOW
    with pytest.raises(ValueError, match="clock"):
        wallet.reserve("rollback")


def test_standing_builder_requires_wallet_before_opening_credentials(
    tmp_path, monkeypatch
):
    path, _value, _old = standing(tmp_path)
    authority = load_activation(path, now=NOW)
    ledger = tmp_path / "allowance.db"
    PilotAllowance.initialize(ledger, authority)
    provider = tmp_path / "provider.db"
    EventStore(provider)
    opened = []
    monkeypatch.setattr(
        private_pilot_worker,
        "build_worker_openai_transport",
        lambda **kwargs: opened.append(kwargs),
    )
    kwargs = dict(
        socket_path=tmp_path / "s.sock",
        provider_database=provider,
        allowance_database=ledger,
        activation_path=path,
        clock=lambda: NOW,
    )
    with pytest.raises(PilotAuthorityError, match="budget"):
        private_pilot_worker.build_worker(**kwargs)
    with pytest.raises((OSError, ValueError)):
        private_pilot_worker.build_worker(
            **kwargs, monthly_budget_path=tmp_path / "missing.sqlite"
        )
    assert opened == []


def test_native_budget_provisioning_and_inspection_are_explicit(tmp_path, capsys):
    path, _value, _old = standing(tmp_path)
    authority = load_activation(path, now=NOW)
    allowance, provider = tmp_path / "allowance.db", tmp_path / "provider.db"
    PilotAllowance.initialize(allowance, authority)
    EventStore(provider)
    stop = path.parent / "stop.json"
    _stop(stop, False)
    budget = tmp_path / "budget.db"
    assert (
        private_pilot_ops.main(
            [
                "prepare-budget",
                "--activation",
                str(path),
                "--monthly-budget",
                str(budget),
            ]
        )
        == 0
    )
    assert "prepared" in capsys.readouterr().out
    assert (
        private_pilot_ops.inspect(path, allowance, stop, provider, now=NOW)["ready"]
        is False
    )
    args = [
        "inspect",
        "--activation",
        str(path),
        "--allowance",
        str(allowance),
        "--provider-database",
        str(provider),
        "--stop",
        str(stop),
        "--monthly-budget",
        str(budget),
    ]
    assert private_pilot_ops.main(args) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["ready"] and state["standing"] and state["expires_at"] is None
    assert state["monthly_budget"]["limit_microusd"] == 10_000_000
    finite_dir = tmp_path / "finite"
    finite_dir.mkdir(mode=0o700)
    finite_path, _value, _authority = grant(finite_dir)
    with pytest.raises(PilotAuthorityError, match="standing"):
        private_pilot_ops.prepare_budget(finite_path, tmp_path / "finite.db")


def test_private_sdk_fence_keeps_uncertainty_and_rejects_races_and_tools(tmp_path):
    budget = private_wallet(tmp_path)
    seen = []
    responses = PrivateBudgetedResponses(
        SimpleNamespace(create=lambda **kwargs: seen.append(kwargs) or {}), budget
    )
    kwargs = dict(
        model="gpt-6.1-sol",
        input="synthetic",
        tools=[],
        store=False,
        max_output_tokens=2048,
    )
    responses.create(**kwargs)
    assert budget.snapshot()["charged_microusd"] == 1_000_000
    for change in (
        dict(tools=[{"type": "web_search"}]),
        dict(model="gpt-6-luna"),
        dict(background=True),
        dict(stream=True),
        dict(truncation="auto"),
    ):
        with pytest.raises(OpenAITransportError) as error:
            responses.create(**(kwargs | change))
        assert error.value.error_code == "private_budget_contract_invalid"
    assert len(seen) == 1
    for index in range(9):
        assert budget.reserve(str(index))
    with pytest.raises(OpenAITransportError) as error:
        responses.create(**kwargs)
    assert error.value.error_code == "private_monthly_budget_exhausted"
    assert len(seen) == 1
    public = tmp_path / "public.sqlite"
    MonthlyBudget.initialize(public, lane="sol", project_id="proj_synthetic")
    assert not available(MonthlyBudget(public, lane="sol", project_id="proj_synthetic"))


@pytest.mark.parametrize("failure", ["missing", "replaced", "corrupt", "clock"])
def test_private_budget_unavailable_fails_closed_without_replenishment(
    tmp_path, failure
):
    budget = private_wallet(tmp_path)
    if failure == "missing":
        budget.path.unlink()
    elif failure == "replaced":
        budget.path.rename(tmp_path / "old.sqlite")
        MonthlyBudget.initialize(
            budget.path, lane="private_sol", project_id="proj_synthetic"
        )
    elif failure == "corrupt":
        with sqlite3.connect(budget.path) as db:
            db.execute("DROP TABLE public_budget_attempts")
    else:
        budget.clock = lambda: datetime(2026, 11, 1)
    assert not available(budget)


def test_standing_gate_without_budget_and_retired_policy_cannot_authorize(tmp_path):
    path, value, _old = standing(tmp_path)
    authority = load_activation(path, now=NOW)
    ledger = tmp_path / "allowance.db"
    allowance = PilotAllowance.initialize(ledger, authority)
    stop = path.parent / "stop.json"
    _stop(stop, False)
    gate = private_pilot_worker.PilotRequestGate(
        authority,
        allowance,
        activation_path=path,
        stop=private_pilot_worker.PilotStop(stop),
        clock=lambda: NOW,
    )
    request = InferenceRequest(
        "reply:" + "a" * 64, "synthetic", MemoryModelRequest(model_request(), (), False)
    )
    assert (
        not gate.current()
        and gate.authorize(request).outcome == "authorization_rejected"
    )
    gate.monthly_budget = private_wallet(tmp_path)
    assert gate.current()
    value["pilot_id"] = "operator-replacement"
    _rewrite(path, value)
    private_pilot_ops.replace(path, ledger)
    assert not gate.current() and gate.paid_transport_blocked()


def test_atomic_sdk_budget_race_does_not_latch_provider_suspension(tmp_path):
    from test_openai_gateway import gateway, request, response

    worker, transport, store = gateway(
        tmp_path,
        [
            OpenAITransportError(error_code="private_monthly_budget_exhausted"),
            response(),
        ],
    )
    result = worker.submit(request(), correlation_id="race", operation_id="race-budget")
    assert result.outcome == "spend_suspended" and result.attempts == 1
    assert not store.is_spend_suspended()
    result = worker.submit(request(), correlation_id="next", operation_id="new-month")
    assert result.outcome == "completed" and len(transport.calls) == 2


def sdk_response(**_kwargs):
    return {
        "id": "synthetic-response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "proposal": {
                                    "decision": "reply",
                                    "content": "Синтетический ответ.",
                                }
                            }
                        ),
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
    }


def test_real_private_ipc_budget_stop_restart_and_calendar_rollover(
    tmp_path, monkeypatch
):
    path, _value, _old = standing(tmp_path)
    authority = load_activation(path, now=NOW)
    ledger = tmp_path / "allowance.db"
    PilotAllowance.initialize(ledger, authority)
    provider = tmp_path / "provider.db"
    EventStore(provider)
    stop = path.parent / "stop.json"
    _stop(stop, False)
    now = [NOW]
    wallet = private_wallet(tmp_path, clock=lambda: now[0])
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return sdk_response()

    monkeypatch.setattr(
        private_pilot_worker,
        "build_worker_openai_transport",
        lambda **_kwargs: OpenAIResponsesTransport(
            SimpleNamespace(responses=SimpleNamespace(create=create))
        ),
    )

    def infer(index):
        worker = private_pilot_worker.build_worker(
            socket_path=tmp_path / "s.sock",
            provider_database=provider,
            allowance_database=ledger,
            activation_path=path,
            stop_path=stop,
            monthly_budget_path=wallet.path,
            clock=lambda: now[0],
        )
        worker.start()
        thread = Thread(target=worker.serve, kwargs={"max_connections": 1})
        thread.start()
        try:
            return OpenAIWorkerClient(
                tmp_path / "s.sock", expected_worker_uid=os.geteuid()
            ).infer(
                MemoryModelRequest(model_request(), (), False),
                operation_id="reply:" + f"{index:064x}",
                correlation_id="synthetic-standing",
            )
        finally:
            thread.join(5)
            worker.close()
            assert not thread.is_alive()

    result = infer(1)
    assert result.outcome == "completed"
    assert wallet.snapshot()["charged_microusd"] == 750
    assert len(calls) == 1 and calls[0]["model"] == "gpt-6.1-sol"
    assert infer(1).outcome == "authorization_rejected"  # restart cannot replay
    _stop(stop, True)
    assert infer(2).outcome == "emergency_stop"
    _stop(stop, False)
    for index in range(9):
        assert wallet.reserve("unknown-" + str(index))
    # Simulate a tenth charged call; the atomic SDK fence cannot exceed $10.
    assert not wallet.reserve("exceeds-reservation")
    assert infer(3).outcome == "spend_suspended"
    assert len(calls) == 1
    assert (
        private_pilot_ops.inspect(
            path, ledger, stop, provider, now=now[0], monthly_budget_path=wallet.path
        )["ready"]
        is False
    )
    # Settled spend frees the next month. Unknown attempts keep their holds.
    now[0] = DEC
    assert infer(4).outcome == "completed"
    assert len(calls) == 2
    with sqlite3.connect(provider) as db:
        assert db.execute(
            "SELECT count(*) FROM provider_spend_suspensions"
        ).fetchone() == (0,)
    state = private_pilot_ops.inspect(
        path, ledger, stop, provider, now=now[0], monthly_budget_path=wallet.path
    )
    assert (
        state["authority_current"] and state["standing"] and state["expires_at"] is None
    )
    wallet.path.chmod(0o644)
    with pytest.raises(ValueError):
        infer(5)
    assert len(calls) == 2
