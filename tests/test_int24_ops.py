"""Manual operator and worker entrypoint routing without credentials or HTTP."""

import json
import os
import runpy
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.public_behavior import PublicBehaviorRequest, public_context_digest
from app.integration.threads_runtime import PUBLIC_RUNTIME_VERSION
from app.integration.threads_runtime import PublicApprovalResult
from app.integration.threads_stage0 import Stage0Journal, _identity
from app.threads.domain import OutboxState
from app.trusted.openai_transport import OPENAI_API_BASE_URL
from app.ops.int24_public_worker import _attestor
from app.ops.int24_threads_runtime import main


def test_worker_attestor_requires_exact_started_application_identity(tmp_path):
    journal = Stage0Journal(tmp_path / "journal.sqlite", "account-1")
    operation = _identity("account-1", "wake", "wake-1")
    origin = {
        "runtime_version": PUBLIC_RUNTIME_VERSION,
        "kind": "wake",
        "event": "wake-1",
        "correlation": operation,
        "trigger": "autonomous_wakeup",
        "context_digest": public_context_digest(()),
        "mission_text": (),
        "timeout": 5,
    }
    request = PublicBehaviorRequest(
        operation, operation, "autonomous_wakeup", (), (), 5
    )
    attest = _attestor("account-1")
    assert attest(request) is False
    with journal.transaction() as db:
        db.execute("CREATE TABLE public_runtime_schema(version TEXT, account TEXT)")
        db.execute(
            "INSERT INTO public_runtime_schema VALUES(?,?)",
            (PUBLIC_RUNTIME_VERSION, "account-1"),
        )
    journal.start(operation, origin)
    evidence = {
        "account": "account-1",
        "kind": "wake",
        "event": "wake-1",
        "resumed": False,
        "runtime_version": PUBLIC_RUNTIME_VERSION,
        "phase": "started",
        "context_digest": public_context_digest(()),
        "mission": (),
        "timeout": 5,
    }
    request = replace(request, reservation=evidence)
    assert attest(request) is True
    assert (
        attest(
            PublicBehaviorRequest(
                operation, operation, "autonomous_wakeup", ("different",), (), 5
            )
        )
        is False
    )
    assert (
        attest(replace(request, reservation=evidence | {"phase": "finished"})) is False
    )
    mission = "Сохранять публичную добросовестность"
    mission_request = replace(
        request, mission=(mission,), reservation=evidence | {"mission": (mission,)}
    )
    assert attest(mission_request) is True
    assert attest(replace(mission_request, mission=("changed",))) is False
    assert (
        attest(replace(request, reservation=evidence | {"account": "other"})) is False
    )


def test_default_operator_entrypoint_remains_disabled(capsys):
    assert main(["readiness"]) == 0
    assert json.loads(capsys.readouterr().out)["ready"] is False
    with pytest.raises(SystemExit):
        main(["dispatch", "operation-1"])


def test_operator_approve_command_exposes_exact_revocable_reference(
    monkeypatch, capsys
):
    import app.ops.int24_threads_runtime as module
    from datetime import datetime, UTC

    now = datetime(2026, 10, 17, 12, tzinfo=UTC)

    class FakeRuntime:
        clock = SimpleNamespace(now=lambda: now)

        def approve(self, operation, *, expires_at):
            assert operation == "operation-1"
            assert expires_at > now
            return PublicApprovalResult(
                operation, OutboxState.READY, "renewed-reference"
            )

    monkeypatch.setattr(module, "_config", lambda _: {})
    monkeypatch.setattr(module, "_runtime", lambda _: FakeRuntime())
    assert (
        module.main(
            ["approve", "operation-1", "--activate-live", "--config", "/unused"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "operation_id": "operation-1",
        "state": "ready",
        "approval_reference": "renewed-reference",
    }


def test_operator_poll_and_reconcile_attempts_use_distinct_correlation_ids(
    monkeypatch, capsys
):
    import app.ops.int24_threads_runtime as module
    from datetime import UTC, datetime

    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    seen = []

    class FakeRuntime:
        clock = SimpleNamespace(now=lambda: now)

        def poll(self, stream, *, correlation_id, **_kwargs):
            seen.append((stream.value, correlation_id))
            return {"polled": stream.value}

        def reconcile(self, operation, *, correlation_id):
            seen.append((operation, correlation_id))
            return {"reconciled": operation}

    monkeypatch.setattr(module, "_config", lambda _: {})
    monkeypatch.setattr(module, "_runtime", lambda _: FakeRuntime())
    for command, value in (
        ("poll", "reply"),
        ("poll", "mention"),
        ("reconcile", "operation-1"),
        ("reconcile", "operation-1"),
    ):
        assert (
            module.main([command, value, "--activate-live", "--config", "/unused"]) == 0
        )
    assert len({correlation for _, correlation in seen}) == len(seen)
    assert all(correlation for _, correlation in seen)
    capsys.readouterr()


def test_operator_recovery_commands_route_exact_operation_and_approval(
    monkeypatch, capsys
):
    import app.ops.int24_threads_runtime as module
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    seen = []

    class FakeRuntime:
        clock = SimpleNamespace(now=lambda: now)

        def recover_sending(self, operation):
            seen.append(("sending", operation))
            return {"state": "unknown"}

        def approve_reconciliation_recovery(self, operation, *, expires_at):
            seen.append(("approve", operation, expires_at))
            return PublicApprovalResult(
                operation, OutboxState.UNKNOWN, "recovery-reference"
            )

        def recover_reconciliation_claim(self, operation, reference):
            seen.append(("claim", operation, reference))
            return {"state": "unknown"}

    monkeypatch.setattr(module, "_config", lambda _: {})
    monkeypatch.setattr(module, "_runtime", lambda _: FakeRuntime())
    base = ["--activate-live", "--config", "/unused"]
    assert module.main(["recover-sending", "operation-1", *base]) == 0
    assert module.main(["approve-reconciliation-recovery", "operation-1", *base]) == 0
    output = capsys.readouterr().out.strip().splitlines()
    assert json.loads(output[-1])["approval_reference"] == "recovery-reference"
    with pytest.raises(SystemExit):
        module.main(["recover-reconciliation", "operation-1", *base])
    assert (
        module.main(
            [
                "recover-reconciliation",
                "operation-1",
                "recovery-reference",
                *base,
            ]
        )
        == 0
    )
    assert seen == [
        ("sending", "operation-1"),
        ("approve", "operation-1", now + timedelta(minutes=15)),
        ("claim", "operation-1", "recovery-reference"),
    ]


@pytest.mark.parametrize(
    ("project", "organization"),
    [
        (None, "org-reviewed"),
        ("proj_reviewed", None),
        ("wrong-project", "org-reviewed"),
        ("proj_reviewed", "wrong-organization"),
    ],
)
def test_worker_rejects_missing_or_unreviewed_billing_scope(
    monkeypatch, project, organization
):
    import app.ops.int24_public_worker as module

    monkeypatch.setattr(
        module,
        "_private_config",
        lambda _: {
            "worker_enabled": True,
            "project_id": project,
            "organization_id": organization,
        },
    )
    monkeypatch.setattr(
        module,
        "read_protected_worker_file",
        lambda *_args, **_kwargs: pytest.fail("credential opened"),
    )
    with pytest.raises(ValueError, match="billing scope"):
        module.main(["--config", "/unused/config"])


def test_worker_passes_reviewed_billing_scope_to_sdk(monkeypatch, tmp_path):
    import app.ops.int24_public_worker as module

    config = {
        "worker_enabled": True,
        "project_id": "proj_reviewed",
        "organization_id": "org-reviewed",
        "openai_key_file": "/unused/key",
        "socket_path": "/unused/socket",
        "attempts_path": "/unused/attempts",
        "worker_event_store_path": str(tmp_path / "events.sqlite"),
        "application_uid": 1001,
        "application_gid": 1002,
        "account_ref": "account-1",
        "model": "reviewed-model",
    }
    seen = {}

    class StopWorker(Exception):
        pass

    class FakeServer:
        def __init__(self, *args, **kwargs):
            seen["server"] = (args, kwargs)

        def start(self):
            seen["started"] = True

        def serve_once(self):
            raise StopWorker

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(module, "_private_config", lambda _: config)
    monkeypatch.setattr(
        module, "read_protected_worker_file", lambda *_a, **_k: b"sk-fake"
    )
    monkeypatch.setattr(module, "PublicWorkerServer", FakeServer)
    monkeypatch.setattr(module.os, "umask", lambda _: 0o077)
    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(
            OpenAI=lambda **kwargs: seen.update(kwargs)
            or SimpleNamespace(responses=object())
        ),
    )
    with pytest.raises(StopWorker):
        module.main(["--config", "/unused/config"])
    assert seen["project"] == "proj_reviewed"
    assert seen["organization"] == "org-reviewed"
    assert seen["base_url"] == OPENAI_API_BASE_URL
    assert seen["max_retries"] == 0
    assert (
        seen["server"][1]["provider_store"]._event_store.database_path
        == tmp_path / "events.sqlite"
    )
    assert seen["started"] is True
    assert seen["closed"] is True


def test_worker_main_stays_disabled_without_activation(monkeypatch):
    import app.ops.int24_public_worker as module

    monkeypatch.setattr(module, "_private_config", lambda _: {"worker_enabled": False})
    with pytest.raises(ValueError, match="disabled"):
        module.main(["--config", "/unused/config"])


def test_worker_reads_only_its_protected_configuration(tmp_path):
    import app.ops.int24_public_worker as module

    path = tmp_path / "worker.json"
    path.write_text(json.dumps({"worker_enabled": False}))
    path.chmod(0o400)
    assert module._private_config(path) == {"worker_enabled": False}


def test_entrypoint_module_guards_remain_covered_without_live_activation(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["int24_threads_runtime", "readiness"])
    with pytest.raises(SystemExit) as result:
        runpy.run_module("app.ops.int24_threads_runtime", run_name="__main__")
    assert result.value.code == 0
    monkeypatch.setattr(sys, "argv", ["int24_public_worker"])
    with pytest.raises(SystemExit):
        runpy.run_module("app.ops.int24_public_worker", run_name="__main__")


def test_operator_protected_config_and_runtime_use_application_owned_state(
    tmp_path, monkeypatch
):
    import app.ops.int24_threads_runtime as module

    config_path = tmp_path / "operator.json"
    config_path.write_text(json.dumps({"activation_reviewed": True}))
    config_path.chmod(0o400)
    assert module._config(config_path) == {"activation_reviewed": True}
    config_path.chmod(0o600)
    config_path.write_text("{}")
    config_path.chmod(0o400)
    with pytest.raises(ValueError, match="reviewed activation"):
        module._config(config_path)
    assert module.Clock().now().tzinfo is not None

    seen = []
    monkeypatch.setattr(
        module, "read_protected_worker_file", lambda *_a, **_k: b"fake-token"
    )
    monkeypatch.setattr(
        module,
        "ThreadsRuntime",
        lambda *args, **kwargs: seen.append((args, kwargs)) or object(),
    )
    config = {
        "threads_token_file": str(tmp_path / "token"),
        "account_ref": "account-1",
        "provider_user_id": "user-1",
        "reply_thread_id": "root-1",
        "scopes": ["threads_basic"],
        "journal_path": str(tmp_path / "journal.sqlite"),
        "scheduler_path": str(tmp_path / "scheduler.sqlite"),
        "threads_path": str(tmp_path / "threads.sqlite"),
        "worker_socket": str(tmp_path / "worker.sock"),
        "worker_uid": os.getuid(),
        "owner_directory": str(tmp_path / "owner"),
        "event_store_path": str(tmp_path / "app-events.sqlite"),
        "ledger_path": str(tmp_path / "ledger.sqlite"),
        "operator_uid": os.getuid(),
        "operator_actor": "operator-1",
    }
    module._runtime(config)
    assert (
        seen[-1][1]["provider"]._event_store.database_path
        == tmp_path / "app-events.sqlite"
    )
    assert seen[-1][1]["mission"] is None
    monkeypatch.setattr(module, "PublicMissionContext", lambda *_a, **_k: "mission")
    module._runtime(config | {"public_mission_path": str(tmp_path / "mission.json")})
    assert seen[-1][1]["mission"] == "mission"


def test_operator_remaining_routes_and_expiry_guards(monkeypatch, capsys):
    import app.ops.int24_threads_runtime as module

    calls = []

    class FakeRuntime:
        clock = SimpleNamespace(now=lambda: module.Clock().now())

        def readiness(self):
            return ("live_unreviewed",)

        def run(self, events):
            calls.append(("run", events))
            return {"run": True}

        def process_inbound(self, stream, limit):
            calls.append(("process", stream, limit))
            return {"process": True}

        def draft(self, operation):
            calls.append(("draft", operation))
            return {"draft": True}

        def revoke(self, approval):
            calls.append(("revoke", approval))

        def dispatch(self, operation):
            calls.append(("dispatch", operation))
            return {"dispatch": True}

    monkeypatch.setattr(module, "_config", lambda _: {})
    monkeypatch.setattr(module, "_runtime", lambda _: FakeRuntime())
    with pytest.raises(SystemExit):
        module.main(["readiness", "--activate-live"])
    for command, value in (
        ("readiness", None),
        ("wakeup", "wake-1"),
        ("process", "reply"),
        ("draft", "operation-1"),
        ("revoke", "approval-1"),
        ("dispatch", "operation-1"),
    ):
        args = (
            [command]
            + ([value] if value else [])
            + ["--activate-live", "--config", "/unused"]
        )
        assert module.main(args) == 0
    for command in ("approve", "approve-reconciliation-recovery"):
        with pytest.raises(SystemExit):
            module.main(
                [
                    command,
                    "operation-1",
                    "--activate-live",
                    "--config",
                    "/unused",
                    "--expires-minutes",
                    "0",
                ]
            )
    assert {call[0] for call in calls} == {
        "run",
        "process",
        "draft",
        "revoke",
        "dispatch",
    }
    assert "live_unreviewed" in capsys.readouterr().out
