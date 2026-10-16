"""INT-19 worker outcome migration keeps older private databases readable."""

import sqlite3

import pytest

from app.core.store import EventStore, FaultPoint, _SCHEMA_V5, schema_objects


def test_v7_worker_outcome_migration_is_atomic(tmp_path):
    path = tmp_path / "events.db"
    EventStore(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE agent_reply_resolutions")
        db.execute(_SCHEMA_V5[0])
        db.execute("UPDATE schema_metadata SET version=7")
        db.execute("PRAGMA user_version=7")
        before = schema_objects(db)

    def interrupt(point):
        if point is FaultPoint.BEFORE_V8_MIGRATION_COMMIT:
            raise RuntimeError("synthetic migration interruption")

    with pytest.raises(RuntimeError, match="synthetic migration interruption"):
        EventStore(path, fault_injector=interrupt)
    with sqlite3.connect(path) as db:
        assert schema_objects(db) == before
        assert db.execute("PRAGMA user_version").fetchone() == (7,)
        assert db.execute("SELECT version FROM schema_metadata").fetchone() == (7,)
    upgraded = EventStore(path)
    assert upgraded.diagnostics().schema_version == 8
    with upgraded._open() as db:
        assert (
            "spend_suspended"
            in db.execute(
                "SELECT sql FROM sqlite_master WHERE name='agent_reply_resolutions'"
            ).fetchone()[0]
        )
