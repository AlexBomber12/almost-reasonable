"""Manual operator and worker entrypoint routing without credentials or HTTP."""

import json
import sys
from types import SimpleNamespace

import pytest

from app.core.public_behavior import PublicBehaviorRequest, public_context_digest
from app.integration.threads_runtime import PUBLIC_RUNTIME_VERSION
from app.integration.threads_stage0 import Stage0Journal, _identity
from app.trusted.openai_transport import OPENAI_API_BASE_URL
from ops.int24_public_worker import _attestor
from ops.int24_threads_runtime import main


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
    attest = _attestor(journal.path, "account-1")
    assert attest(request) is False
    with journal.transaction() as db:
        db.execute("CREATE TABLE public_runtime_schema(version TEXT, account TEXT)")
        db.execute(
            "INSERT INTO public_runtime_schema VALUES(?,?)",
            (PUBLIC_RUNTIME_VERSION, "account-1"),
        )
    journal.start(operation, origin)
    assert attest(request) is True
    assert (
        attest(
            PublicBehaviorRequest(
                operation, operation, "autonomous_wakeup", ("different",), (), 5
            )
        )
        is False
    )
    journal.finish(operation, origin, rejected=True)
    assert attest(request) is False


def test_default_operator_entrypoint_remains_disabled(capsys):
    assert main(["readiness"]) == 0
    assert json.loads(capsys.readouterr().out)["ready"] is False
    with pytest.raises(SystemExit):
        main(["dispatch", "operation-1"])


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
    import ops.int24_public_worker as module

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


def test_worker_passes_reviewed_billing_scope_to_sdk(monkeypatch):
    import ops.int24_public_worker as module

    config = {
        "worker_enabled": True,
        "project_id": "proj_reviewed",
        "organization_id": "org-reviewed",
        "openai_key_file": "/unused/key",
        "socket_path": "/unused/socket",
        "attempts_path": "/unused/attempts",
        "application_uid": 1001,
        "application_gid": 1002,
        "journal_path": "/unused/journal",
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
    assert seen["started"] is True
    assert seen["closed"] is True


def test_worker_main_stays_disabled_without_activation(monkeypatch):
    import ops.int24_public_worker as module

    monkeypatch.setattr(module, "_private_config", lambda _: {"worker_enabled": False})
    with pytest.raises(ValueError, match="disabled"):
        module.main(["--config", "/unused/config"])
