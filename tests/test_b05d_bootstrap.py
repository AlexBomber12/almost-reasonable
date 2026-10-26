"""Research startup must never initialize an empty substitute production store."""

from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.core.store import EventStore
from app.owner_control.store import OwnerStore
from app.privacy.ledger import PrivacyLedger
from app.private_web.auth import AuthStore
from app.private_web.cli import _require_initialized_research_stores
from app.trusted.public_research_worker import build_worker


@pytest.fixture
def stores(tmp_path):
    args = SimpleNamespace(
        auth_db=str(tmp_path / "auth.db"),
        event_db=str(tmp_path / "events.db"),
        ipc_state_db=str(tmp_path / "ipc.db"),
        privacy_ledger=str(tmp_path / "ledger.db"),
        owner_directory=str(tmp_path / "owner"),
    )
    AuthStore(args.auth_db)
    EventStore(args.event_db)
    EventStore(args.ipc_state_db)
    PrivacyLedger(args.privacy_ledger)
    OwnerStore(args.owner_directory)
    return args


def test_existing_initialized_stores_are_accepted_without_changes(stores):
    paths = [
        Path(getattr(stores, key))
        for key in ("auth_db", "event_db", "ipc_state_db", "privacy_ledger")
    ]
    paths.append(Path(stores.owner_directory) / "owner.sqlite")
    before = [p.read_bytes() for p in paths]
    _require_initialized_research_stores(stores)
    assert [p.read_bytes() for p in paths] == before


@pytest.mark.parametrize(
    "name", ["auth_db", "event_db", "ipc_state_db", "privacy_ledger", "owner_directory"]
)
@pytest.mark.parametrize("contents", [b"", b"not a SQLite store"])
def test_empty_or_invalid_application_stores_are_never_initialized(
    stores, name, contents
):
    path = Path(getattr(stores, name))
    if name == "owner_directory":
        path = path / "owner.sqlite"
    path.write_bytes(contents)
    with pytest.raises(SystemExit, match="initialized application stores"):
        _require_initialized_research_stores(stores)
    assert path.read_bytes() == contents


@pytest.mark.parametrize("table", ["auth_schema", "owner_schema"])
def test_wrong_authentication_schema_is_not_repaired(stores, table):
    path = (
        Path(stores.auth_db)
        if table == "auth_schema"
        else Path(stores.owner_directory) / "owner.sqlite"
    )
    with sqlite3.connect(path) as db:
        db.execute(f"DELETE FROM {table}")
    with pytest.raises(SystemExit, match="initialized application stores"):
        _require_initialized_research_stores(stores)


@pytest.mark.parametrize("contents", [b"", b"not a SQLite store"])
def test_empty_research_provider_store_is_never_initialized(tmp_path, contents):
    path = tmp_path / "provider.sqlite3"
    path.write_bytes(contents)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="initialized research provider database"):
        build_worker(
            socket_path=tmp_path / "worker.sock",
            provider_database=path,
            pilot_stop_path=tmp_path / "stop.json",
            profile_path=tmp_path / "profile.json",
            authority_journal=tmp_path / "authority.sqlite3",
            project_id="proj_synthetic",
            organization_id="org-synthetic",
        )
    assert path.read_bytes() == contents
    assert not (tmp_path / "authority.sqlite3").exists()
