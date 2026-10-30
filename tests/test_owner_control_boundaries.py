"""Legacy records cannot invent new scheduling or control authority."""

import sqlite3
import pytest

import test_owner_worker_registry as registry_cases
from app.integration.agent_cadence import cadence_sources
from app.ops.int26_owner_pilot import build_application
from test_int26_owner_console import settings, call, login

workers = registry_cases.workers


def test_legacy_journal_cannot_invent_a_cadence_capability(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE legacy(value)")
    assert cadence_sources(path, 0) == {}


def test_legacy_operation_cannot_silently_authorize_a_new_registry(workers, tmp_path):
    args = settings(tmp_path)
    args.worker_registry = str(workers[0])
    app = build_application(args)
    cookie, csrf = login(app)
    token = cookie.split("=", 1)[1]
    operation = app.store.issue_worker_stop(token)
    assert app.store.worker_registry_operation(token, operation) is None
    code, _, _ = call(
        app,
        "/emergency-stop",
        "POST",
        cookie=cookie,
        csrf=csrf,
        payload={"operation_id": operation},
    )
    assert code == 409
    with pytest.raises(ValueError, match="outcome"):
        app.store.record_worker_targets(
            token, operation, {name: "confirmed" for name in app.workers.names}
        )
