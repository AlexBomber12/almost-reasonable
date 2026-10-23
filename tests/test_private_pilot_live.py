"""Credential-free checks of the live operator flow and durable accounting."""

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from acceptance import private_pilot_live as live
from acceptance.private_pilot_conversation import ASK, FIRST, MARS, run_ship_journey
from acceptance.private_pilot_live import (
    MODEL,
    TASK,
    reserve_aggregate,
    validate_authorization,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)


def approval():
    return {
        "task": TASK,
        "commit": "synthetic-commit",
        "model": MODEL,
        "project_id": "proj_synthetic",
        "organization_id": "org-synthetic",
        "valid_from": (NOW - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valid_until": (NOW + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "billing_budget_usd": "1.00",
        "max_aggregate_operations": 6,
        "max_aggregate_input_tokens": 10000,
        "max_aggregate_output_tokens": 3072,
        "max_requests_per_run": 3,
    }


def activation():
    return {
        "model": MODEL,
        "project_id": "proj_synthetic",
        "organization_id": "org-synthetic",
        "not_before": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (NOW + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "max_attempts_per_operation": 1,
        "max_operations": 5,
    }


def test_live_authorization_requires_task_scope_limits_and_current_commit():
    validate_authorization(approval(), activation(), "synthetic-commit", NOW)
    changes = [
        ("task", "another-task"),
        ("commit", "another-commit"),
        ("model", "another-model"),
        ("billing_budget_usd", "0"),
        ("max_aggregate_operations", 0),
        ("max_aggregate_output_tokens", True),
        ("max_requests_per_run", 2),
        ("project_id", "proj_other"),
    ]
    for key, value in changes:
        changed = approval()
        changed[key] = value
        with pytest.raises(ValueError):
            validate_authorization(changed, activation(), "synthetic-commit", NOW)
    with pytest.raises(ValueError):
        validate_authorization(
            approval(), activation(), "synthetic-commit", NOW + timedelta(hours=1)
        )
    changed_activation = activation()
    changed_activation["max_attempts_per_operation"] = 2
    with pytest.raises(ValueError):
        validate_authorization(approval(), changed_activation, "synthetic-commit", NOW)
    changed_activation = activation()
    changed_activation["expires_at"] = (NOW + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with pytest.raises(ValueError, match="beyond"):
        validate_authorization(approval(), changed_activation, "synthetic-commit", NOW)


def test_aggregate_allowance_is_durable_across_run_ids_and_commit_changes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(live, "_protected_parents", lambda *_: None)
    ledger = tmp_path / "aggregate.sqlite3"
    approved = approval()
    assert reserve_aggregate(ledger, approved, "first-run") == 3
    assert reserve_aggregate(ledger, approved, "second-run") == 0
    with pytest.raises(ValueError, match="exhausted"):
        reserve_aggregate(ledger, approved, "third-run")
    altered = json.loads(json.dumps(approved))
    altered["commit"] = "new-commit"
    with pytest.raises(ValueError, match="policy changed"):
        reserve_aggregate(ledger, altered, "new-commit-run")


def test_contradictory_mars_text_does_not_pass_destination_oracle():
    replies = iter(
        (
            "Искра",
            "Корабль: Искра; Назначение: Юпитер",
            "Корабль Искра всё ещё летит к Юпитеру, а не к Марсу. Марс не выбран.",
        )
    )

    def send(_message_id, _content):
        return {"model_state": "completed", "response": next(replies)}

    with pytest.raises(AssertionError, match="Mars"):
        run_ship_journey(send, lambda: [], lambda: None)


def test_protected_file_rejects_writable_parent_and_open_replacement(
    tmp_path, monkeypatch
):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o770)
    unsafe.chmod(0o770)
    protected = unsafe / "approval.json"
    protected.write_bytes(b"approved")
    protected.chmod(0o400)
    with pytest.raises(ValueError, match="unsafe protected directory"):
        live._protected(protected, 0o400, os.geteuid())

    monkeypatch.setattr(live, "_protected", lambda path, _mode, _uid: path.lstat())
    assert live._read_protected(protected, 0o400, os.geteuid()) == b"approved"
    original_open = live.os.open

    def replace_before_open(path, flags):
        replacement = unsafe / "replacement.json"
        replacement.write_bytes(b"substituted")
        replacement.chmod(0o400)
        replacement.replace(path)
        return original_open(path, flags)

    monkeypatch.setattr(live.os, "open", replace_before_open)
    with pytest.raises(ValueError, match="changed while opening"):
        live._read_protected(protected, 0o400, os.geteuid())


def test_compose_build_uses_separate_preparation_timeout(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs["timeout"]))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(live.subprocess, "run", run)
    live._compose({}, "build", timeout=600)
    live._compose({}, "ps")
    assert calls == [((*live.COMPOSE, "build"), 600), ((*live.COMPOSE, "ps"), 120)]


def attempt(
    operation,
    correlation,
    *,
    input_tokens=3,
    output_tokens=2,
    outcome="completed",
    rejection=None,
    retry=0,
    suffix=1,
):
    return [
        f"{operation}:{suffix}",
        correlation,
        outcome,
        rejection,
        input_tokens,
        output_tokens,
        None
        if input_tokens is None or output_tokens is None
        else input_tokens + output_tokens,
        retry,
        "0.01",
        "estimated",
    ]


@pytest.mark.parametrize(
    "case",
    [
        "passed",
        "pricing_unavailable",
        "exact_limit_final",
        "limit_after_first",
        "unknown_after_first",
        "empty_ledger",
        "empty_after_journey",
        "historical_only",
        "historical_mutation",
        "duplicate",
        "extra",
        "incomplete",
        "rejected",
        "retries",
        "wrong_allowance_delta",
        "wrong_correlation",
        "unresolved",
        "expired_mid_journey",
    ],
)
def test_operator_reconciles_real_journey_and_cleans_up(tmp_path, monkeypatch, case):
    current = datetime.now(UTC)
    approved = approval()
    approved["valid_from"] = (current - timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    approved["valid_until"] = (current + timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    if case == "limit_after_first":
        approved["max_aggregate_input_tokens"] = 4
    if case == "exact_limit_final":
        approved["max_aggregate_input_tokens"] = 10
    state_dir = tmp_path / "state"
    (state_dir / "secrets").mkdir(parents=True)
    (state_dir / "secrets" / "openai-api-key").write_text("offline-placeholder")
    authorization = tmp_path / "authorization.json"
    authorization.write_text(json.dumps(approved))
    activation_path = tmp_path / "activation.json"
    active = activation()
    active["not_before"] = (current - timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    active["expires_at"] = (current + timedelta(minutes=4)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    activation_path.write_text(json.dumps(active))
    args = SimpleNamespace(
        authorization=authorization,
        activation=activation_path,
        state_root=state_dir,
        aggregate_ledger=tmp_path / "aggregate.sqlite3",
        origin="https://chat.test",
        report=tmp_path / "report.json",
    )
    commands = []
    owner_changes = []
    monkeypatch.setattr(live.os, "geteuid", lambda: 0)
    monkeypatch.setattr(live.os, "chown", lambda *_: None)
    monkeypatch.setattr(live, "_protected", lambda *_: None)
    monkeypatch.setattr(live, "_read_protected", lambda path, *_: path.read_bytes())
    monkeypatch.setattr(live, "getpass", lambda _: "synthetic-password")
    real_stop = live._stop_file

    def stop(path, active):
        if path.exists():
            path.chmod(0o600)
        real_stop(path, active)

    monkeypatch.setattr(live, "_stop_file", stop)

    def command(parts, _env):
        if parts[:2] == ("git", "rev-parse"):
            return "synthetic-commit"
        if parts[:2] == ("git", "status"):
            return ""
        return "sha256:synthetic-image"

    monkeypatch.setattr(live, "_command", command)

    def compose(_env, *parts, **_kwargs):
        if parts == ("build",):
            assert _kwargs["timeout"] == 600
        commands.append(parts)
        return ""

    monkeypatch.setattr(live, "_compose", compose)
    source = {}

    def app_python(_env, code, input_text=None):
        if "FROM event_envelopes" in code:
            return json.dumps([source[input_text.rstrip("\n")]])
        return "True" if "print(OwnerStore" in code else ""

    monkeypatch.setattr(live, "_app_python", app_python)
    old = attempt("reply:historical", "historic", input_tokens=1, output_tokens=1)
    rows = [old]
    consumed = ["reply:historical"]
    reservations = []
    history_seen = False

    def worker_python(_env, code):
        if "provider_usage_attempts" in code:
            observed = [old] if case == "empty_after_journey" and history_seen else rows
            return json.dumps({"attempts": observed, "reservations": reservations})
        if "SELECT operation_id FROM consumed" in code:
            observed = (
                consumed[:1]
                if case == "empty_after_journey" and history_seen
                else consumed
            )
            return json.dumps({"maximum": 5, "consumed": observed})
        return ""

    monkeypatch.setattr(live, "_worker_python", worker_python)
    monkeypatch.setattr(
        live,
        "_owner_change",
        lambda _env, _password, active: owner_changes.append(active),
    )
    replies = (
        "Искра",
        "Корабль: Искра; Назначение: Юпитер",
        "Корабль: Искра; Назначение: Марс",
    )
    transcript = []
    sends = []

    class Participant:
        def __init__(self, _origin):
            pass

        def login(self, _password):
            pass

        def request(self, method, path, payload=None):
            nonlocal history_seen
            if path == "/api/history":
                history_seen = True
                return {"messages": transcript}
            if path != "/api/messages":
                return {}
            index = len(sends)
            sends.append(payload)
            message_id = payload["message_id"]
            event_id = f"event-{index}"
            correlation = f"correlation-{index}"
            source[message_id] = [event_id, correlation]
            operation = "reply:" + hashlib.sha256(event_id.encode()).hexdigest()
            transcript.extend(
                (
                    {"role": "participant", "content": payload["content"]},
                    {"role": "agent", "content": replies[index]},
                )
            )
            if case not in {"empty_ledger", "historical_only"}:
                row = attempt(operation, correlation)
                if case == "limit_after_first" and index == 0:
                    row[4] = 4
                if case == "unknown_after_first" and index == 0:
                    row[4] = None
                if case == "incomplete" and index == 2:
                    row[2] = "timeout"
                if case == "rejected" and index == 2:
                    row[3] = "project_spend_limit_exceeded"
                if case == "retries" and index == 2:
                    row[7] = 1
                if case == "wrong_correlation" and index == 2:
                    row[1] = "other-correlation"
                if case == "pricing_unavailable":
                    row[8] = None
                    row[9] = "pricing_unavailable"
                rows.append(row)
                if case == "duplicate" and index == 2:
                    rows.append(row.copy())
                if case == "extra" and index == 2:
                    rows.append(attempt("reply:extra", "other"))
                if case == "unresolved" and index == 2:
                    reservations.append("reply:unresolved:1")
                if case == "historical_mutation" and index == 2:
                    old[4] = 2
                if case != "wrong_allowance_delta" or index != 2:
                    consumed.append(operation)
            return {"model_state": "completed", "response": replies[index]}

    monkeypatch.setattr(live, "HTTPSParticipant", Participant)
    monkeypatch.setattr(live, "_wait_http_ready", lambda _: None)
    if case == "expired_mid_journey":
        original = live.validate_authorization
        count = 0

        def validate(*values):
            nonlocal count
            count += 1
            if count == 4:
                raise ValueError("authorization is outside its validity period")
            return original(*values)

        monkeypatch.setattr(live, "validate_authorization", validate)
    if case in {"passed", "exact_limit_final", "pricing_unavailable"}:
        live.run(args)
    else:
        with pytest.raises(
            (ValueError, RuntimeError),
            match="allowance|usage|accounting|reservation|attempt|identity|period|exhausted|cleanup|match",
        ):
            live.run(args)
    report = json.loads(args.report.read_text())
    assert report["status"] == (
        "passed"
        if case in {"passed", "exact_limit_final", "pricing_unavailable"}
        else "failed"
    )
    assert report["worker_stop"] is True
    assert report["owner_stop"] is True
    assert report["running_services"] == []
    assert owner_changes == [False, True]
    assert any(parts[:2] == ("stop", "private-app") for parts in commands)
    assert Path(args.report).stat().st_mode & 0o777 == 0o600
    if case in {"limit_after_first", "unknown_after_first"}:
        assert len(sends) == 1
    if case in {"passed", "exact_limit_final", "pricing_unavailable"}:
        assert [item["content"] for item in sends] == [FIRST, ASK, MARS]
        assert report["provider_attempts"] == 3
        assert report["remaining_allowance"] == 1
        assert report["aggregate_input_tokens"] == 10
        assert any(parts[:2] == ("restart", "private-app") for parts in commands)
    if case == "pricing_unavailable":
        assert report["estimated_cost_total"] is None
        assert report["cost_estimate_statuses"] == ["pricing_unavailable"]
