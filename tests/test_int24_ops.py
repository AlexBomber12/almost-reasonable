"""Manual operator and worker entrypoint routing without credentials or HTTP."""

import json

import pytest

from app.core.public_behavior import PublicBehaviorRequest, public_context_digest
from app.integration.threads_runtime import PUBLIC_RUNTIME_VERSION
from app.integration.threads_stage0 import Stage0Journal, _identity
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
