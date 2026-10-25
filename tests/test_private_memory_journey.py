"""The opt-in live journey uses the same closed synthetic assertions offline."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from acceptance.private_memory_journey import (
    FILLERS,
    FIRST,
    JourneyOutcomeError,
    MAX_OPERATIONS,
    QUESTION,
    REVISION,
    run_robot_journey,
)
from acceptance import private_memory_worker
from acceptance import private_pilot_live as live
from app.trusted.openai_transport import OpenAIResponsesTransport


def test_rejected_typed_attempt_is_billed_and_diagnosed_without_accounting_error(
    monkeypatch,
):
    operations = [
        ("evolution:abc", "corr"),
        ("typed:abc", "corr"),
        ("reply:abc", "corr"),
    ]
    rows = [
        [
            name + ":1",
            "corr",
            "invalid_proposal" if name.startswith("typed:") else "completed",
            "invalid_purpose_proposal" if name.startswith("typed:") else None,
            2,
            1,
            3,
            0,
            "0",
            "estimated",
            "typed_invalid_number" if name.startswith("typed:") else None,
        ]
        for name, _ in operations
    ]
    monkeypatch.setattr(
        live, "_usage", lambda *_args, **_kwargs: {"attempts": rows, "reservations": []}
    )
    monkeypatch.setattr(
        live,
        "_allowance",
        lambda *_args, **_kwargs: {
            "maximum": MAX_OPERATIONS,
            "consumed": [name for name, _ in operations],
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
                {},
                baseline,
                operations,
                {"max_aggregate_input_tokens": 100, "max_aggregate_output_tokens": 100},
                required=MAX_OPERATIONS,
            )[1]
        )
        == 3
    )
    failure = live._proposal_failure(rows, "robot-original")
    assert failure.step == "robot-original"
    assert failure.operation_family == "typed"
    assert failure.diagnostic_code == "typed_invalid_number"
    other = list(rows[2])
    other[2:4] = ["invalid_proposal", "invalid_purpose_proposal"]
    other[10] = "typed_invalid_number"
    assert (
        live._proposal_failure([other], "robot-original").diagnostic_code == "unknown"
    )
    rows[1][4] = None
    with pytest.raises(ValueError, match="usage is unavailable"):
        live._reconcile(
            {},
            baseline,
            operations,
            {"max_aggregate_input_tokens": 100, "max_aggregate_output_tokens": 100},
            required=MAX_OPERATIONS,
        )
    unsafe = JourneyOutcomeError(
        "private_canary",
        "invalid_proposal",
        operation_family="private_canary",
        diagnostic_code="private_canary",
    )
    assert unsafe.step == "<invalid>"
    assert unsafe.operation_family == "unknown"
    assert unsafe.diagnostic_code == "unknown"
    assert "private_canary" not in str(unsafe)


def test_rejected_narrative_keeps_billed_usage_and_conservative_reply_hold(monkeypatch):
    suffix = "a" * 64
    operations = [
        (family + ":" + suffix, "corr")
        for family in ("evolution", "typed", "narrative")
    ]
    rows = [
        [
            operation + ":1",
            "corr",
            "invalid_proposal" if operation.startswith("narrative:") else "completed",
            "invalid_purpose_proposal" if operation.startswith("narrative:") else None,
            4,
            2,
            6,
            0,
            "0",
            "estimated",
            "narrative_invalid_handle" if operation.startswith("narrative:") else None,
        ]
        for operation, _ in operations
    ]
    monkeypatch.setattr(
        live, "_usage", lambda *_args, **_kwargs: {"attempts": rows, "reservations": []}
    )
    monkeypatch.setattr(
        live,
        "_allowance",
        lambda *_args, **_kwargs: {
            "maximum": 20,
            "consumed": [operation for operation, _ in operations]
            + ["reply_hold:" + suffix],
        },
    )
    baseline = {"attempts": set(), "rows": {}, "consumed": set(), "maximum": 20}
    assert (
        len(
            live._reconcile(
                {},
                baseline,
                operations,
                {"max_aggregate_input_tokens": 100, "max_aggregate_output_tokens": 100},
                required=20,
            )[1]
        )
        == 3
    )
    failure = live._proposal_failure(rows, "combined-story")
    assert failure.operation_family == "narrative"
    assert failure.diagnostic_code == "narrative_invalid_handle"
    assert failure.step == "combined-story"


def test_robot_journey_requires_actual_persistent_projection_after_eviction():
    sent = []
    deleted = False
    restarted = False
    steps = []

    def send(message_id, content):
        sent.append((message_id, content))
        return {
            "model_state": "completed",
            "response": "Текущий проект — Кобальт; предпочитаю Ладога.",
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
                    "text": "Вымышленный проект робота Кобальт",
                },
                {"kind": "durable_interest", "text": "Предпочитаю Ладога"},
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


@pytest.mark.parametrize("no_action_fillers", [(), (0,), (0, 2, 4), tuple(range(5))])
def test_filler_terminal_outcomes_preserve_the_full_journey(no_action_fillers):
    sent = []
    deleted = False
    restarted = False

    def send(step, _content):
        sent.append(step)
        if step.startswith("robot-filler-") and int(step[-1]) in no_action_fillers:
            return {"model_state": "no_action", "response": None}
        return {"model_state": "completed", "response": "Кобальт; Ладога"}

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
                {"kind": "candidate_useful_memory", "text": "Кобальт"},
                {"kind": "durable_interest", "text": "Ладога"},
            ],
        }

    def delete():
        nonlocal deleted
        deleted = True
        return {"status": "applied"}

    run_robot_journey(send, restart, inspect, delete)
    assert restarted and deleted
    assert len(sent) == 9
    assert len(set(sent)) == 9


@pytest.mark.parametrize(
    "state,response",
    [
        ("pending", None),
        ("uncertain", None),
        ("stopped", None),
        ("unavailable", None),
        ("rejected", None),
        ("failed", None),
        ("spend_suspended", None),
        ("unknown", None),
        ("no_action", "delivered text"),
        ("completed", None),
    ],
)
def test_invalid_filler_outcome_fails_at_bounded_step(state, response):
    def send(step, _content):
        if step == "robot-filler-0":
            return {"model_state": state, "response": response}
        return {"model_state": "completed", "response": "Ответ"}

    with pytest.raises(JourneyOutcomeError) as failure:
        run_robot_journey(send, lambda: None, lambda: {}, lambda: {})
    assert failure.value.step == "robot-filler-0"
    assert failure.value.model_state == state


def test_malformed_filler_payload_is_safely_bounded():
    def send(step, _content):
        if step == "robot-filler-0":
            return {"model_state": "unexpected private text!", "response": None}
        return {"model_state": "completed", "response": "Ответ"}

    with pytest.raises(JourneyOutcomeError) as failure:
        run_robot_journey(send, lambda: None, lambda: {}, lambda: {})
    assert failure.value.step == "robot-filler-0"
    assert failure.value.model_state == "<invalid>"
    assert "private text" not in str(failure.value)


def test_nondictionary_filler_payload_is_rejected():
    def send(step, _content):
        return (
            None
            if step == "robot-filler-0"
            else {"model_state": "completed", "response": "Ответ"}
        )

    with pytest.raises(JourneyOutcomeError) as failure:
        run_robot_journey(send, lambda: None, lambda: {}, lambda: {})
    assert failure.value.step == "robot-filler-0"
    assert failure.value.model_state == "<invalid>"


@pytest.mark.parametrize(
    "step", ["robot-original", "robot-revision", "robot-recall", "robot-after-delete"]
)
def test_mandatory_answer_turn_rejects_no_action(step):
    deleted = False

    def send(current, _content):
        return (
            {"model_state": "no_action", "response": None}
            if current == step
            else {"model_state": "completed", "response": "Кобальт; Ладога"}
        )

    def inspect():
        return {
            "task": "private_conversation",
            "conversation": [],
            "memories": []
            if deleted
            else [
                {"kind": "candidate_useful_memory", "text": "Кобальт"},
                {"kind": "durable_interest", "text": "Ладога"},
            ],
        }

    def delete():
        nonlocal deleted
        deleted = True
        return {"status": "applied"}

    with pytest.raises(JourneyOutcomeError) as failure:
        run_robot_journey(send, lambda: None, inspect, delete)
    assert failure.value.step == step
    assert failure.value.model_state == "no_action"


@pytest.mark.parametrize("reject_typed", [False, True])
def test_http_worker_no_action_filler_reaches_restart_recall_deletion_and_accounting(
    tmp_path,
    reject_typed,
    monkeypatch,
):
    from dataclasses import replace
    import os
    import sqlite3
    from threading import Thread

    from app.core.context import ContextLimits
    from app.core.openai_worker_client import OpenAIWorkerClient
    from app.core.provider_contract import (
        DeploymentStage,
        default_openai_provider_config,
    )
    from app.core.provider_spend import ProviderStateStore
    from app.core.store import EventStore
    from app.private_web.runtime import WorkerConversationRuntime
    from app.privacy.application import EventStoreErasureAdapter, PrivacyReadGate
    from app.privacy.deletion import DeletionCoordinator
    from app.trusted.openai_gateway import TrustedOpenAIGateway
    from app.trusted.openai_transport import FakeOpenAITransport
    from app.trusted.openai_worker_server import OpenAIWorkerServer, WorkerServerConfig
    from test_combined_narrative_memory import (
        _generic_no_change,
        _result,
        _typed_no_change,
    )
    from test_int19_worker_runtime import _app
    from test_privacy_deletion import (
        context as deletion_context,
        request as deletion_request,
    )

    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    _auth, events, ledger, gate, app, browser, _other = _app(tmp_path, socket_path)
    ipc_store = EventStore(tmp_path / "ipc.db")

    def runtime(store, privacy):
        return WorkerConversationRuntime(
            store,
            privacy,
            OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
            provider_state=ProviderStateStore(ipc_store),
            memory_extraction_enabled=True,
            memory_evolution_enabled=True,
            typed_memory_enabled=True,
            context_limits=ContextLimits(32000, 5),
        )

    app._conversations = runtime(events, gate)

    class Scenario(FakeOpenAITransport):
        def call(self, request, *, timeout_seconds):
            index = len(self.calls)
            task = json.loads(request.input_text)["task"]
            if task == "evolve_private_memory":
                outcome = (
                    _result(
                        {
                            "decision": "new_memory",
                            "handles": [],
                            "kind": "candidate_useful_memory",
                            "statement": "Проект Кобальт",
                            "confidence": 0.8,
                        }
                    )
                    if index == 3
                    else _generic_no_change()
                )
            elif task == "evolve_private_typed_memory":
                outcome = (
                    _result(
                        {
                            "decision": "new_memory",
                            "handle": None,
                            "kind": "durable_interest",
                            "source_role": "supporting",
                            "fields": {
                                "topic": "Synthetic invalid interest",
                                "strength": 101,
                                "status": "active",
                                "reason_for_interest": "Synthetic reason",
                            },
                        }
                    )
                    if reject_typed and index == 1
                    else _result(
                        {
                            "decision": "new_memory",
                            "handle": None,
                            "kind": "durable_interest",
                            "source_role": "supporting",
                            "fields": {
                                "topic": "Ладога",
                                "strength": 70,
                                "status": "active",
                                "reason_for_interest": "Предпочтение в конструкции",
                            },
                        }
                    )
                    if index == 4
                    else _typed_no_change()
                )
            else:
                assert task == "private_conversation"
                outcome = (
                    _result(
                        {
                            "proposal": {
                                "decision": "no_action",
                                "reason_category": "not_warranted",
                            }
                        }
                    )
                    if index == 8
                    else _result(
                        {
                            "proposal": {
                                "decision": "reply",
                                "content": (
                                    "Получено."
                                    if index in {11, 14, 17, 20}
                                    else "Проект Кобальт; предпочитаю Ладога."
                                ),
                            }
                        }
                    )
                )
            self._outcomes[:] = [outcome]
            return super().call(request, timeout_seconds=timeout_seconds)

    fake = Scenario([])
    provider_store = ProviderStateStore(EventStore(tmp_path / "provider.db"))
    server = OpenAIWorkerServer(
        WorkerServerConfig(socket_path, frozenset({os.getuid()})),
        TrustedOpenAIGateway(
            store=provider_store,
            transport=fake,
            config=replace(
                default_openai_provider_config(),
                deployment_stage=DeploymentStage.STAGE_1,
                paid_calls_enabled=True,
                max_attempts=1,
            ),
        ),
        evolution_enabled=True,
        typed_enabled=True,
    )
    server.start()
    worker = Thread(
        target=lambda: server.serve(
            max_connections=3 if reject_typed else MAX_OPERATIONS
        ),
        daemon=True,
    )
    worker.start()
    projections = []
    sent = []
    deleted = False
    restarted = False
    active_gate = gate
    try:

        def send(step, content):
            payload = json.loads(browser.submit(step, content)[2])
            sent.append((step, payload))
            projections.append(json.loads(fake.calls[-1].request.input_text))
            return payload

        def restart():
            nonlocal restarted, active_gate
            reopened = EventStore(events.database_path)
            new_gate = PrivacyReadGate(ledger, reopened)
            assert new_gate.reconcile()
            app._conversations = runtime(reopened, new_gate)
            active_gate = new_gate
            restarted = True

        def inspect():
            return projections[-1]

        def delete():
            nonlocal deleted
            with events._open() as db:
                source = db.execute(
                    "SELECT event_id FROM event_envelopes WHERE source_message_id=?",
                    ("robot-revision",),
                ).fetchone()[0]
            disposition = (
                DeletionCoordinator(ledger, EventStoreErasureAdapter(events))
                .delete(deletion_context(), deletion_request(content_reference=source))
                .disposition.value
            )
            deleted = True
            assert active_gate.reconcile()
            return {"status": disposition}

        if reject_typed:
            assert send("robot-original", FIRST)["model_state"] == "completed"
            with sqlite3.connect(tmp_path / "provider.db") as db:
                rows = db.execute(
                    "SELECT attempt_id, correlation_id, outcome, rejection_reason, "
                    "input_tokens, output_tokens, total_tokens, retry_count, "
                    "estimated_cost, cost_estimate_status, provider_error_code "
                    "FROM provider_usage_attempts ORDER BY started_at, attempt_id"
                ).fetchall()
                assert db.execute(
                    "SELECT count(*) FROM provider_attempt_reservations"
                ).fetchone() == (0,)
            assert len(rows) == 3
            assert [row[2] for row in rows].count("invalid_proposal") == 1
            assert (
                next(row[3] for row in rows if row[2] == "invalid_proposal")
                == "invalid_purpose_proposal"
            )
            assert all(
                row[4] is not None and row[5] is not None and row[7] == 0
                for row in rows
            )
            assert len({row[0] for row in rows}) == 3
            operations = [(row[0].removesuffix(":1"), row[1]) for row in rows]
            monkeypatch.setattr(
                live,
                "_usage",
                lambda *_args, **_kwargs: {"attempts": rows, "reservations": []},
            )
            monkeypatch.setattr(
                live,
                "_allowance",
                lambda *_args, **_kwargs: {
                    "maximum": MAX_OPERATIONS,
                    "consumed": [name for name, _ in operations],
                },
            )
            assert (
                len(
                    live._reconcile(
                        {},
                        {
                            "attempts": set(),
                            "rows": {},
                            "consumed": set(),
                            "maximum": MAX_OPERATIONS,
                        },
                        operations,
                        {
                            "max_aggregate_input_tokens": 10_000,
                            "max_aggregate_output_tokens": 10_000,
                        },
                        required=MAX_OPERATIONS,
                    )[1]
                )
                == 3
            )
            from app.core.self_state_store import SelfStateStore
            from test_cognition_pipeline import principal

            assert (
                SelfStateStore(events).retrieve_private_candidates(principal("a")) == ()
            )
            failure = live._proposal_failure(rows, "robot-original")
            assert failure.operation_family == "typed"
            assert failure.diagnostic_code == "typed_invalid_number"
            assert failure.model_state == "invalid_proposal"
            return

        run_robot_journey(send, restart, inspect, delete)
        assert restarted and deleted
        assert sent[2][1]["model_state"] == "no_action"
        assert sent[2][1]["response"] is None
        assert len(sent) == 9
        assert len(fake.calls) == MAX_OPERATIONS == 27
        with sqlite3.connect(tmp_path / "provider.db") as db:
            attempts = db.execute(
                "SELECT attempt_id, outcome, retry_count FROM provider_usage_attempts"
            ).fetchall()
        assert len(attempts) == 27
        assert len({row[0].removesuffix(":1") for row in attempts}) == 27
        assert all(row[0].endswith(":1") for row in attempts)
        assert all(row[1] == "completed" and row[2] == 0 for row in attempts)
    finally:
        worker.join(5)
        server.close()


def test_live_compose_uses_the_offline_journey_history_bound():
    compose = Path("ops/compose.private-pilot-memory-acceptance.yaml").read_text()
    assert '      - --worker-history-events\n      - "5"' in compose


@pytest.mark.parametrize(
    "error",
    [
        JourneyOutcomeError("robot-filler-0", "pending"),
        AssertionError("memory proof failed"),
    ],
)
def test_cli_returns_nonzero_for_journey_assertion_and_keeps_protected_report(
    tmp_path, monkeypatch, capsys, error
):
    report = tmp_path / "report.json"
    report.write_text('{"status":"failed","failure_step":"robot-filler-0"}')
    report.chmod(0o600)

    def fail(_args):
        raise error

    monkeypatch.setattr(live, "run", fail)
    argv = [
        "--authorization",
        str(tmp_path / "auth"),
        "--activation",
        str(tmp_path / "activation"),
        "--state-root",
        str(tmp_path / "state"),
        "--aggregate-ledger",
        str(tmp_path / "ledger"),
        "--origin",
        "https://chat.test",
        "--backend-subnet",
        "172.30.252.0/29",
        "--backend-ip",
        "172.30.252.3",
        "--report",
        str(report),
        "--memory-journey",
    ]
    assert live.main(argv) == 1
    assert report.stat().st_mode & 0o777 == 0o600
    assert json.loads(report.read_text())["failure_step"] == "robot-filler-0"
    assert "inspect protected report" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["Кобальт", "Ладога"])
def test_robot_journey_rejects_missing_persistent_memory(missing):
    def send(_message_id, _content):
        return {"model_state": "completed", "response": "Кобальт и Ладога"}

    def inspect():
        memories = "Кобальт и Ладога".replace(missing, "omitted")
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
        "purpose": "agent",
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
