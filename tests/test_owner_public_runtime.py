"""Owner observation has no provider credential and cannot bypass shared Stop."""

from datetime import timedelta
import json
import sqlite3
import pytest

from app.owner_control.public_runtime import PublicRuntimeReadView
from app.ops.int26_owner_pilot import build_application
import test_finite_public_grant as grant_cases
from test_int26_owner_console import settings, call, login

pending_public = grant_cases.pending_public


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


def view(app, *, scheduler=True):
    return PublicRuntimeReadView(
        account=app.account,
        journal_path=app.journal.path,
        threads_path=app.service.store.database,
        ledger_path=app.ledger.path,
        owner_directory=app.owner.directory,
        scheduler_path=app.scheduler.repository.path if scheduler else None,
    )


def config(app):
    return {
        "version": "public-runtime-observation-v1",
        "account": app.account,
        "journal_path": str(app.journal.path),
        "threads_path": str(app.service.store.database),
        "ledger_path": str(app.ledger.path),
        "owner_directory": str(app.owner.directory),
        "scheduler_path": str(app.scheduler.repository.path),
    }


def test_owner_observes_real_budgets_and_decisions_and_stops_same_public_app(
    pending_public, tmp_path
):
    from app.threads.service import PolicyDeniedError

    app, _, _, http, _, _, authority, _ = pending_public
    path = tmp_path / "observation.json"
    path.write_text(json.dumps(config(app)))
    path.chmod(0o400)
    args = settings(tmp_path)
    args.public_runtime_config = str(path)
    owner = build_application(args)
    cookie, csrf = login(owner)
    code, status, _ = call(owner, "/status", cookie=cookie)
    assert code == 200
    public = status["public_runtime"]
    assert public["available"] and public["billing_authority"] is False
    assert public["recent"][0]["decision"] == "propose_publication"
    assert public["grants"][0]["publication_limit"] == authority.max_publications
    assert public["windows"] and public["cadence_observed"]
    assert str(app.journal.path) not in json.dumps(public)
    _, issued, _ = call(owner, "/operation", "POST", cookie=cookie, csrf=csrf)
    code, _, _ = call(
        owner,
        "/stop",
        "POST",
        cookie=cookie,
        csrf=csrf,
        payload={"operation_id": issued["operation_id"], "active": True},
    )
    assert code == 200 and app.owner.active()
    with pytest.raises(PolicyDeniedError):
        app.publication_guard()
    assert http.calls == []


def test_readonly_status_distinguishes_effective_cadence_from_missing_observation(
    pending_public,
):
    app = pending_public[0]
    current = view(app)
    assert current.owner_status()["cadence_seconds"] is None
    app.scheduler.repository.select_cadence(timedelta(seconds=30))
    assert current.owner_status()["cadence_seconds"] == 30
    assert not view(app, scheduler=False).owner_status()["cadence_observed"]
    with __import__(
        "app.core.read_only_sqlite", fromlist=["connect_read_only"]
    ).connect_read_only(app.journal.path) as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM intents")


@pytest.mark.parametrize(
    "case", ["runtime", "intellectual", "threads", "ledger", "owner", "payload"]
)
def test_observation_never_promotes_corrupt_or_wrong_scope_metadata(
    pending_public, case
):
    app = pending_public[0]
    current = view(app)
    if case == "threads":
        with app.service.store._open() as db:
            db.execute("UPDATE threads_schema_metadata SET version=999")
    elif case == "ledger":
        with app.ledger._open() as db:
            db.execute("DELETE FROM privacy_control_generation")
    else:
        with app.journal.transaction() as db:
            statement = {
                "runtime": "UPDATE public_runtime_schema SET account='other'",
                "intellectual": "UPDATE agent_intellectual_schema SET version='wrong'",
                "owner": "UPDATE public_runtime_binding SET value='/other'",
                "payload": "UPDATE intents SET payload='[]'",
            }[case]
            db.execute(statement)
    assert current.owner_status()["available"] is False


def test_binding_configuration_cannot_change_owner_or_add_credentials(
    pending_public, tmp_path
):
    app = pending_public[0]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config(app)))
    path.chmod(0o400)
    with pytest.raises(ValueError, match="share owner Stop"):
        PublicRuntimeReadView.from_config(
            path, expected_owner_directory=tmp_path / "other"
        )
    path.chmod(0o600)
    path.write_text(json.dumps(config(app) | {"token": "synthetic-not-a-credential"}))
    path.chmod(0o400)
    with pytest.raises(ValueError, match="observation config"):
        PublicRuntimeReadView.from_config(
            path, expected_owner_directory=app.owner.directory
        )
    with pytest.raises(ValueError, match="absolute"):
        PublicRuntimeReadView(
            account=app.account,
            journal_path="relative.db",
            threads_path=app.service.store.database,
            ledger_path=app.ledger.path,
            owner_directory=app.owner.directory,
        )


def test_missing_source_is_never_created_and_no_grant_is_invented(tmp_path):
    from test_int24_runtime import setup

    app, worker, _, http, _ = setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    try:
        status = view(app).owner_status()
        assert status["recent"] == status["windows"] == status["grants"] == []
        missing = tmp_path / "missing.db"
        with pytest.raises(sqlite3.Error):
            PublicRuntimeReadView(
                account=app.account,
                journal_path=missing,
                threads_path=app.service.store.database,
                ledger_path=app.ledger.path,
                owner_directory=app.owner.directory,
            )
        assert not missing.exists() and http.calls == []
    finally:
        worker.close()
