"""Ordinary public entrypoint wiring with real stores/IPC and fake provider SDK."""

from dataclasses import replace
import json
import os

import pytest

from app.cognition.shared import SharedIntelligence
from app.core.shared_graph import erase_shared_source
from app.integration.public_shared_source import from_config
from app.ops import int24_threads_runtime as ops
from app.owner_control.store import OwnerStore
from test_cognition_pipeline import NOW
from test_reviewed_shared import candidate
from test_shared_intelligence import shared as shared_fixture
from test_int24_runtime import setup, run_once


@pytest.fixture
def shared(tmp_path):
    return shared_fixture.__wrapped__(tmp_path)


def configuration(tmp_path, shared, **changes):
    events, ledger, _gate, _a, _b = shared
    (tmp_path / "public").mkdir(mode=0o700, exist_ok=True)
    OwnerStore(tmp_path / "public" / "owner")
    value = {
        "version": "public-shared-source-v1",
        "account": "account-1",
        "event_store_path": str(events.database_path),
        "ledger_path": str(ledger.path),
        "owner_directory": str(tmp_path / "public" / "owner"),
        **changes,
    }
    path = tmp_path / "shared.json"
    if path.exists():
        path.chmod(0o600)
    path.write_text(json.dumps(value))
    path.chmod(0o400)
    return path


def load(path, tmp_path, **kwargs):
    return from_config(
        path,
        account="account-1",
        owner_directory=tmp_path / "public" / "owner",
        public_paths=(
            tmp_path / "public" / "ledger.sqlite",
            tmp_path / "public" / "events.sqlite",
        ),
        clock=lambda: NOW,
        **kwargs,
    )


def test_normal_operator_runtime_delivers_allowed_private_influence_to_public_sdk(
    shared, tmp_path, monkeypatch
):
    events, _ledger, gate, source, _ = shared
    intelligence = SharedIntelligence(
        events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )
    item = replace(
        candidate(source, value="Compare causal models of uncertainty."),
        audience=("participant-a", "participant-b", "public:account-1"),
    )
    intelligence.transition(item)
    public = tmp_path / "public"
    public.mkdir(mode=0o700)
    original, worker, sdk, http, clock = setup(
        public, {"decision": "no_action", "reason_category": "other"}
    )
    path = configuration(tmp_path, shared)
    config = {
        "threads_token_file": str(public / "token"),
        "account_ref": "account-1",
        "provider_user_id": "user-1",
        "scopes": list(original.policy.scopes),
        "journal_path": str(public / "journal.sqlite"),
        "scheduler_path": str(public / "scheduler.sqlite"),
        "threads_path": str(public / "threads.sqlite"),
        "worker_socket": str(public / "worker.sock"),
        "worker_uid": os.getuid(),
        "owner_directory": str(public / "owner"),
        "event_store_path": str(public / "events.sqlite"),
        "ledger_path": str(public / "ledger.sqlite"),
        "operator_uid": os.getuid(),
        "operator_actor": "operator-1",
        "shared_intellectual_config": str(path),
    }
    monkeypatch.setattr(ops, "Clock", lambda: clock)
    monkeypatch.setattr(
        ops, "read_protected_worker_file", lambda *_a, **_k: b"fake-token"
    )
    monkeypatch.setattr(ops, "HTTPSClient", lambda: http)
    try:
        app = ops._runtime(config)
        worker.attest = app.attest_worker_request
        run_once(app, worker)
        rendered = sdk.calls[-1]["input"][1]["content"]
        assert item.proposal.candidate.value in rendered
        assert (
            "participant-a" not in rendered
            and source not in rendered
            and "Synthetic A" not in rendered
        )
        assert not http.calls
        with events._open() as db, db:
            erase_shared_source(db, source)
        assert not app.shared_intellectual_source.sources()
    finally:
        worker.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "unknown"},
        {"account": "foreign"},
        {"owner_directory": "/different"},
        {"extra": True},
        {"event_store_path": "relative.sqlite"},
    ],
)
def test_wrong_binding_or_closed_shape_is_rejected_before_credentials(
    shared, tmp_path, changes
):
    path = configuration(tmp_path, shared, **changes)
    with pytest.raises((ValueError, OSError)):
        load(path, tmp_path)


@pytest.mark.parametrize(
    "kind", ["missing", "empty", "foreign_schema", "unsafe_mode", "linked", "symlink"]
)
def test_source_admission_never_initializes_a_missing_or_invalid_store(
    shared, tmp_path, kind
):
    events = shared[0]
    source = events.database_path
    if kind == "missing":
        source = tmp_path / "absent.sqlite"
    elif kind in {"empty", "foreign_schema"}:
        source = tmp_path / "invalid.sqlite"
        source.write_bytes(b"" if kind == "empty" else b"not a SQLite database")
        source.chmod(0o600)
    elif kind == "unsafe_mode":
        source.chmod(0o644)
    elif kind == "linked":
        os.link(source, tmp_path / "linked.sqlite")
    else:
        source = tmp_path / "alias.sqlite"
        source.symlink_to(events.database_path)
    before = source.read_bytes() if source.exists() else None
    path = configuration(tmp_path, shared, event_store_path=str(source))
    with pytest.raises((ValueError, OSError, RuntimeError)):
        load(path, tmp_path)
    assert (source.read_bytes() if source.exists() else None) == before


def test_public_private_ledger_alias_is_rejected(shared, tmp_path):
    path = configuration(tmp_path, shared)
    with pytest.raises(ValueError, match="distinct"):
        from_config(
            path,
            account="account-1",
            owner_directory=tmp_path / "public" / "owner",
            public_paths=(shared[1].path,),
            clock=lambda: NOW,
        )


def test_default_readiness_never_opens_source_or_credentials(monkeypatch, capsys):
    monkeypatch.setattr(
        ops,
        "read_protected_worker_file",
        lambda *_a, **_k: pytest.fail("inert readiness read a protected file"),
    )
    assert ops.main(["readiness"]) == 0
    assert json.loads(capsys.readouterr().out)["ready"] is False


@pytest.mark.parametrize("invalid", ["ledger", "empty_ledger", "owner", "graph"])
def test_invalid_current_controls_or_graph_blocks_source_startup(
    shared, tmp_path, invalid
):
    path = configuration(tmp_path, shared)
    if invalid == "ledger":
        with shared[1]._open() as db, db:
            db.execute("UPDATE privacy_schema_metadata SET version=99")
    elif invalid == "empty_ledger":
        empty = tmp_path / "empty-ledger.sqlite"
        empty.write_bytes(b"")
        empty.chmod(0o600)
        path = configuration(tmp_path, shared, ledger_path=str(empty))
    elif invalid == "owner":
        import sqlite3

        with sqlite3.connect(tmp_path / "public" / "owner" / "owner.sqlite") as db:
            db.execute("UPDATE owner_schema SET version=99")
    else:
        with shared[0]._open() as db, db:
            db.execute(
                "DELETE FROM protected_event_taints WHERE event_id=?", (shared[3],)
            )
    with pytest.raises(ValueError):
        load(path, tmp_path)


def test_bad_shared_source_is_rejected_before_threads_credential_access(
    shared, tmp_path, monkeypatch
):
    path = configuration(tmp_path, shared, account="wrong-account")
    monkeypatch.setattr(
        ops,
        "read_protected_worker_file",
        lambda *_a, **_k: pytest.fail("credential read before source validation"),
    )
    with pytest.raises(ValueError):
        ops._runtime(
            {
                "shared_intellectual_config": str(path),
                "account_ref": "account-1",
                "owner_directory": str(tmp_path / "public" / "owner"),
            }
        )


def test_duplicate_binding_field_and_cross_role_store_alias_are_rejected(
    shared, tmp_path
):
    path = configuration(tmp_path, shared)
    original = path.read_text()
    path.chmod(0o600)
    path.write_text(
        original.replace(
            '"account": "account-1"', '"account": "account-1", "account": "account-1"'
        )
    )
    path.chmod(0o400)
    with pytest.raises(ValueError):
        load(path, tmp_path)
    path = configuration(tmp_path, shared, ledger_path=str(shared[0].database_path))
    with pytest.raises(ValueError, match="distinct"):
        load(path, tmp_path)
