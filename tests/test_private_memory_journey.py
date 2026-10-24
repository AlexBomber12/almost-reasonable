"""The opt-in live journey uses the same closed synthetic assertions offline."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from acceptance.private_memory_journey import (
    FILLERS,
    FIRST,
    MAX_OPERATIONS,
    QUESTION,
    REVISION,
    run_robot_journey,
)
from acceptance import private_memory_worker
from acceptance import private_pilot_live as live
from app.trusted.openai_transport import OpenAIResponsesTransport


def test_robot_journey_requires_actual_persistent_projection_after_eviction():
    sent = []
    deleted = False
    restarted = False
    steps = []

    def send(message_id, content):
        sent.append((message_id, content))
        return {
            "model_state": "completed",
            "response": "The current project is Cobalt; I prefer silent gears.",
        }

    def restart():
        nonlocal restarted
        restarted = True

    def inspect():
        return {
            "task": "private_conversation",
            "conversation": [{"content": QUESTION}],
            "memories": []
            if deleted
            else [
                {
                    "kind": "candidate_useful_memory",
                    "text": "The fictional robot project is Cobalt",
                },
                {"kind": "durable_interest", "text": "The preference is silent gears"},
            ],
        }

    def delete():
        nonlocal deleted
        deleted = True
        return {"status": "applied"}

    assert (
        run_robot_journey(send, restart, inspect, delete, steps.append)["steps"]
        == steps
    )
    assert restarted and deleted
    assert [content for _, content in sent] == [
        FIRST,
        REVISION,
        *FILLERS,
        QUESTION,
        QUESTION,
    ]
    assert MAX_OPERATIONS == len(sent) * 3 == 27


def test_live_compose_uses_the_offline_journey_history_bound():
    compose = Path("ops/compose.private-pilot-memory-acceptance.yaml").read_text()
    assert '      - --worker-history-events\n      - "5"' in compose


@pytest.mark.parametrize("missing", ["Cobalt", "silent gears"])
def test_robot_journey_rejects_missing_persistent_memory(missing):
    def send(_message_id, _content):
        return {"model_state": "completed", "response": "Cobalt and silent gears"}

    def inspect():
        memories = "Cobalt and silent gears".replace(missing, "omitted")
        return {
            "task": "private_conversation",
            "conversation": [{"content": QUESTION}],
            "memories": [
                {"kind": "candidate_useful_memory", "text": memories},
                {"kind": "durable_interest", "text": memories},
            ],
        }

    with pytest.raises(AssertionError, match="persistent memories"):
        run_robot_journey(send, lambda: None, inspect, lambda: {"status": "applied"})


def test_synthetic_worker_capture_records_exact_reply_input(tmp_path, monkeypatch):
    capture = tmp_path / "capture.jsonl"
    monkeypatch.setenv("PILOT_MEMORY_CAPTURE", str(capture))
    original = OpenAIResponsesTransport.call
    seen = []

    def fake_call(_transport, request, *, timeout_seconds):
        seen.append((request, timeout_seconds))
        return "synthetic-result"

    def fake_main(_argv):
        request = SimpleNamespace(
            purpose="agent", input_text='{"task":"private_conversation"}'
        )
        assert (
            OpenAIResponsesTransport.call(object(), request, timeout_seconds=5)
            == "synthetic-result"
        )
        return 0

    monkeypatch.setattr(OpenAIResponsesTransport, "call", fake_call)
    monkeypatch.setattr(private_memory_worker.private_pilot_worker, "main", fake_main)
    assert private_memory_worker.main([]) == 0
    assert json.loads(capture.read_text()) == {
        "task": "private_conversation",
        "input": '{"task":"private_conversation"}',
    }
    assert len(seen) == 1
    assert OpenAIResponsesTransport.call is fake_call
    assert original is not fake_call


def test_memory_live_opt_in_has_distinct_task_and_reserved_operation_bound(
    tmp_path, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    now = datetime(2030, 1, 1, tzinfo=UTC)
    approval = {
        "task": live.MEMORY_TASK,
        "commit": "synthetic-commit",
        "model": live.MODEL,
        "project_id": "proj_synthetic",
        "organization_id": "org_synthetic",
        "valid_from": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valid_until": (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "billing_budget_usd": "1.00",
        "max_aggregate_operations": 54,
        "max_aggregate_input_tokens": 200000,
        "max_aggregate_output_tokens": 12288,
        "max_requests_per_run": MAX_OPERATIONS,
    }
    activation = {
        "version": "private-pilot-activation-b03d-v1",
        "task": live.MEMORY_TASK,
        "model": live.MODEL,
        "project_id": "proj_synthetic",
        "organization_id": "org_synthetic",
        "not_before": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "max_attempts_per_operation": 1,
        "max_operations": MAX_OPERATIONS,
    }
    live.validate_authorization(
        approval, activation, "synthetic-commit", now, memory=True
    )
    with pytest.raises(ValueError, match="task authorization"):
        live.validate_authorization(approval, activation, "synthetic-commit", now)
    monkeypatch.setattr(live, "_protected_parents", lambda *_: None)
    ledger = tmp_path / "aggregate.sqlite3"
    assert (
        live.reserve_aggregate(ledger, approval, "first", slots=MAX_OPERATIONS)
        == MAX_OPERATIONS
    )
    assert live.reserve_aggregate(ledger, approval, "second", slots=MAX_OPERATIONS) == 0
    with pytest.raises(ValueError, match="exhausted"):
        live.reserve_aggregate(ledger, approval, "third", slots=MAX_OPERATIONS)
    commands = []
    monkeypatch.setattr(
        live,
        "_command",
        lambda command, _env, **_kwargs: commands.append(command) or "",
    )
    live._compose({"PILOT_MEMORY_JOURNEY": "1"}, "config", "--quiet")
    assert "ops/compose.private-pilot-memory-acceptance.yaml" in commands[0]
    expected = [(f"evolution:{index:064x}", "corr") for index in range(MAX_OPERATIONS)]
    rows = [
        [operation + ":1", "corr", "completed", None, 1, 1, 2, 0, "0", "estimated"]
        for operation, _ in expected
    ]
    monkeypatch.setattr(
        live,
        "_usage",
        lambda _env, **_kwargs: {"attempts": rows, "reservations": []},
    )
    monkeypatch.setattr(
        live,
        "_allowance",
        lambda _env, **_kwargs: {
            "maximum": MAX_OPERATIONS,
            "consumed": [operation for operation, _ in expected],
        },
    )
    baseline = {
        "attempts": set(),
        "rows": {},
        "consumed": set(),
        "maximum": MAX_OPERATIONS,
    }
    assert (
        len(
            live._reconcile(
                {}, baseline, expected, approval, final=True, required=MAX_OPERATIONS
            )[1]
        )
        == MAX_OPERATIONS
    )
