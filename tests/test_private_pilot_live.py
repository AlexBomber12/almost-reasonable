"""Credential-free checks for the deferred operator command."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from acceptance import private_pilot_live as live
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


def test_aggregate_allowance_is_durable_across_run_ids_and_commit_changes(tmp_path):
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


@pytest.mark.parametrize("failure", [False, True])
def test_operator_command_records_result_and_stops_on_completion_or_failure(
    tmp_path, monkeypatch, failure
):
    current = datetime.now(UTC)
    approved = approval()
    approved["valid_from"] = (current - timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    approved["valid_until"] = (current + timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    state = tmp_path / "state"
    (state / "secrets").mkdir(parents=True)
    (state / "secrets" / "openai-api-key").write_text("offline-placeholder")
    authorization = tmp_path / "authorization.json"
    authorization.write_text(json.dumps(approved))
    activation_path = tmp_path / "activation.json"
    active = activation()
    active["not_before"] = (current - timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    activation_path.write_text(json.dumps(active))
    args = SimpleNamespace(
        authorization=authorization,
        activation=activation_path,
        state_root=state,
        aggregate_ledger=tmp_path / "aggregate.sqlite3",
        origin="https://chat.test",
        report=tmp_path / "report.json",
    )
    commands = []
    owner_changes = []
    monkeypatch.setattr(live.os, "geteuid", lambda: 0)
    monkeypatch.setattr(live.os, "chown", lambda *_args: None)
    monkeypatch.setattr(live, "_protected", lambda *_args: None)
    monkeypatch.setattr(live, "getpass", lambda _prompt: "synthetic-password")
    real_stop = live._stop_file

    def simulated_root_stop(path, active):
        if path.exists():
            path.chmod(0o600)
        real_stop(path, active)

    monkeypatch.setattr(live, "_stop_file", simulated_root_stop)

    def command(parts, _env):
        if parts[:2] == ("git", "rev-parse"):
            return "synthetic-commit"
        if parts[:2] == ("git", "status"):
            return ""
        return "sha256:synthetic-image"

    def compose(_env, *parts, **_kwargs):
        commands.append(parts)
        return ""

    monkeypatch.setattr(live, "_command", command)
    monkeypatch.setattr(live, "_compose", compose)
    monkeypatch.setattr(
        live,
        "_app_python",
        lambda _env, code, *_args: "True" if "print(OwnerStore" in code else "",
    )
    monkeypatch.setattr(live, "_worker_python", lambda *_args: "")
    monkeypatch.setattr(
        live,
        "_owner_change",
        lambda _env, _password, active: owner_changes.append(active),
    )
    monkeypatch.setattr(live, "_usage", lambda _env: [])
    monkeypatch.setattr(live, "_remaining", lambda _env: 5)

    class Participant:
        def __init__(self, _origin):
            pass

        def login(self, _password):
            pass

        def request(self, *_args):
            return {}

    monkeypatch.setattr(live, "HTTPSParticipant", Participant)
    monkeypatch.setattr(live, "_wait_http_ready", lambda _participant: None)

    def journey(_send, _history, restart, **_kwargs):
        restart()
        if failure:
            raise AssertionError("offline failure")
        return {
            "ship_name": "Искра",
            "steps": ["name", "recall", "restart_mars", "history"],
        }

    monkeypatch.setattr(live, "run_ship_journey", journey)
    if failure:
        with pytest.raises(AssertionError, match="offline failure"):
            live.run(args)
    else:
        live.run(args)
    report = json.loads(args.report.read_text())
    assert report["status"] == ("failed" if failure else "passed")
    assert report["worker_stop"] is True
    assert report["owner_stop"] is True
    assert report["running_services"] == []
    assert report["remaining_allowance"] == 5
    assert owner_changes == [False, True]
    assert any(parts[:2] == ("restart", "private-app") for parts in commands)
    assert any(parts[:2] == ("stop", "private-app") for parts in commands)
    assert Path(args.report).stat().st_mode & 0o777 == 0o600
