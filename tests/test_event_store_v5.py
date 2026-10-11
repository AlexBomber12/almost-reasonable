"""Minimal additive release schema, rollback and genuine v4 compatibility."""

import sqlite3
import pytest
from app.core.store import EventStore, FaultPoint, schema_objects, EventStoreError
from app.privacy.recovery import create_sqlite_backup, _schema_version, BackupError
from test_agent_output_store import operation, persist
from app.core.agent_output_store import AgentOutputStore
from test_event_store import principal, private_input, NOW


def populated_v4(path):
    store = EventStore(path)
    store.append_private_input(principal("a"), private_input("a", "synthetic source"))
    outputs = AgentOutputStore(store)
    outputs.reserve_reply(principal("a"), operation())
    reply = persist(outputs)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE agent_reply_resolutions")
        db.execute("UPDATE schema_metadata SET version=4")
        db.execute("PRAGMA user_version=4")
    return reply


def test_additive_migration_preserves_held_drafts_and_v4_backup(tmp_path):
    path = tmp_path / "v4.db"
    reply = populated_v4(path)
    assert _schema_version(path) == 4
    assert (
        create_sqlite_backup(path, tmp_path / "v4-backup.db", 4, NOW).schema_version
        == 4
    )
    with sqlite3.connect(path) as db:
        before = {
            name: db.execute(f"SELECT * FROM {name}").fetchall()
            for name in (
                "event_envelopes",
                "private_payloads",
                "agent_reply_operations",
                "agent_reply_payloads",
                "agent_reply_sources",
            )
        }
    upgraded = EventStore(path)
    assert upgraded.diagnostics().schema_version == 5
    assert AgentOutputStore(upgraded).read_reply(principal("a"), "reply-1") == reply
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM agent_reply_resolutions").fetchall() == []
        for name, rows in before.items():
            assert db.execute(f"SELECT * FROM {name}").fetchall() == rows


def test_v5_migration_fault_rolls_back_schema_and_metadata(tmp_path):
    path = tmp_path / "v4.db"
    populated_v4(path)
    with sqlite3.connect(path) as db:
        schema = schema_objects(db)

    def crash(point):
        if point is FaultPoint.BEFORE_V5_MIGRATION_COMMIT:
            raise RuntimeError("synthetic migration crash")

    with pytest.raises(RuntimeError):
        EventStore(path, fault_injector=crash)
    with sqlite3.connect(path) as db:
        assert schema_objects(db) == schema
        assert db.execute("PRAGMA user_version").fetchone() == (4,)
        assert db.execute("SELECT version FROM schema_metadata").fetchone() == (4,)
    assert EventStore(path).diagnostics().schema_version == 5


@pytest.mark.parametrize(
    "mutation",
    [
        "DROP TABLE agent_reply_resolutions",
        "CREATE TRIGGER release_shadow BEFORE INSERT ON agent_reply_resolutions BEGIN SELECT 1; END",
        "CREATE INDEX unreviewed ON agent_reply_resolutions(outcome)",
    ],
)
def test_exact_schema_and_backup_reject_release_schema_drift(tmp_path, mutation):
    store = EventStore(tmp_path / "events.db")
    with sqlite3.connect(store.database_path) as db:
        db.execute(mutation)
    with pytest.raises(EventStoreError):
        EventStore(store.database_path)
    with pytest.raises(BackupError):
        _schema_version(store.database_path)


@pytest.mark.parametrize("deleted", [False, True])
def test_genuine_v4_quarantine_uses_real_adapter_and_current_deletion_ledger(
    tmp_path, deleted
):
    from app.privacy.application import (
        EventStoreRestoreAdapter,
        EventStoreErasureAdapter,
    )
    from app.privacy.recovery import QuarantinedRestore
    from app.privacy.ledger import PrivacyLedger
    from app.privacy.deletion import DeletionCoordinator
    from test_privacy_deletion import context, request

    path = tmp_path / "genuine-v4.db"
    populated_v4(path)
    backup = tmp_path / "genuine-v4-backup.db"
    create_sqlite_backup(path, backup, 4, NOW)
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    if deleted:
        upgraded = EventStore(path)
        result = DeletionCoordinator(ledger, EventStoreErasureAdapter(upgraded)).delete(
            context(), request(content_reference="event-a")
        )
        assert result.disposition.value == "applied"
    quarantine = tmp_path / "quarantine.db"
    restored = QuarantinedRestore(ledger, 4, EventStoreRestoreAdapter).restore(
        backup, quarantine, NOW
    )
    assert restored.activation_eligible
    assert _schema_version(quarantine) == 4
    with sqlite3.connect(f"file:{quarantine}?immutable=1", uri=True) as db:
        assert db.execute("SELECT count(*) FROM agent_reply_payloads").fetchone() == (
            (0 if deleted else 1),
        )
        assert db.execute("SELECT count(*) FROM private_payloads").fetchone() == (
            (0 if deleted else 1),
        )
    assert quarantine.stat().st_mode & 0o777 == 0o400
