"""Additive v4 migration with genuine event, revision and usage histories."""

import sqlite3
from shared_schema_helpers import remove_v6

import pytest

from app.core.agent_output_store import AgentOutputStore
from app.core.provider_spend import ProviderStateStore
from app.core.self_state import StateType
from app.core.self_state_store import SelfStateStore
from app.core.store import EventStore, EventStoreError, FaultPoint, schema_objects
from app.privacy.recovery import BackupError, _schema_version, create_sqlite_backup
from test_event_store import NOW, principal, private_input
from test_provider_spend import attempt
from test_self_state import interest_change


def populated_v3(path):
    store = EventStore(path)
    store.append_private_input(
        principal("a"), private_input("a", "synthetic private input")
    )
    states = SelfStateStore(store)
    states.create_interest_candidate(principal("a"), interest_change("event-a"))
    states.revise_interest_candidate(
        principal("a"),
        interest_change("event-a", operation="revise", expected=1, strength=70),
    )
    ProviderStateStore(store).record_attempt(attempt())
    with sqlite3.connect(path) as db:
        remove_v6(db)
        db.execute("DROP TRIGGER event_input_sequence_append")
        db.execute("DROP TABLE event_input_sequences")
        for table in (
            "agent_reply_resolutions",
            "agent_reply_sources",
            "agent_reply_payloads",
            "agent_reply_operations",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("UPDATE schema_metadata SET version=3")
        db.execute("PRAGMA user_version=3")
        original = {
            row[0]: db.execute(f'SELECT * FROM "{row[0]}"').fetchall()
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_metadata'"
            ).fetchall()
        }
    return original


def test_v3_migration_preserves_every_existing_row_and_history(tmp_path):
    path = tmp_path / "events.db"
    original = populated_v3(path)
    store = EventStore(path)
    assert store.diagnostics().schema_version == 6
    with sqlite3.connect(path) as db:
        for table, rows in original.items():
            assert db.execute(f'SELECT * FROM "{table}"').fetchall() == rows
    assert (
        store.retrieve_private_events(principal("a"))[0].content
        == "synthetic private input"
    )
    history = SelfStateStore(store).retrieve_private_revision_history(
        principal("a"), StateType.INTEREST, "interest-space"
    )
    assert len(history) == 2
    assert ProviderStateStore(store).usage_attempt_count() == 1
    assert AgentOutputStore(store).read_reply(principal("a"), "unreserved") is None


def test_v4_interruption_rolls_back_entire_migration(tmp_path):
    path = tmp_path / "events.db"
    populated_v3(path)
    with sqlite3.connect(path) as db:
        schema = schema_objects(db)

    def fail(point):
        if point is FaultPoint.BEFORE_V4_MIGRATION_COMMIT:
            raise RuntimeError("synthetic migration interruption")

    with pytest.raises(RuntimeError):
        EventStore(path, fault_injector=fail)
    with sqlite3.connect(path) as db:
        assert schema_objects(db) == schema
        assert db.execute("PRAGMA user_version").fetchone() == (3,)
        assert db.execute("SELECT version FROM schema_metadata").fetchone() == (3,)
    assert EventStore(path).diagnostics().schema_version == 6


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE unexpected(secret TEXT)",
        "CREATE TRIGGER unexpected AFTER INSERT ON private_payloads BEGIN SELECT 1; END",
        "DROP INDEX event_envelopes_private_scope_time",
        "DROP TABLE private_payloads",
    ],
)
def test_exact_schema_rejects_unexpected_or_incomplete_objects(tmp_path, sql):
    path = tmp_path / "events.db"
    store = EventStore(path)
    with sqlite3.connect(path) as db:
        db.execute(sql)
    with pytest.raises(EventStoreError):
        EventStore(path)
    with pytest.raises(EventStoreError):
        store.retrieve_private_events(principal("a"))
    with pytest.raises(EventStoreError):
        store.append_private_input(principal("a"), private_input("a", "synthetic"))
    with pytest.raises(BackupError):
        _schema_version(path)


def test_unversioned_nonempty_database_rejected(tmp_path):
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated(id INTEGER)")
    with pytest.raises(EventStoreError):
        EventStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (0,)
        assert len(schema_objects(db)) == 1


def test_v4_online_backup_preserves_private_reply_and_exact_schema(tmp_path):
    from test_agent_output_store import operation, persist

    path = tmp_path / "events.db"
    store = EventStore(path)
    store.append_private_input(principal("a"), private_input("a", "synthetic input"))
    outputs = AgentOutputStore(store)
    outputs.reserve_reply(principal("a"), operation())
    saved = persist(outputs)
    destination = tmp_path / "snapshot.db"
    assert create_sqlite_backup(path, destination, 6, NOW).schema_version == 6
    assert _schema_version(destination) == 6
    assert (
        AgentOutputStore(EventStore(destination)).read_reply(principal("a"), "reply-1")
        == saved
    )


@pytest.mark.parametrize("version", [1, 2, 3])
def test_earlier_backup_schemas_remain_exactly_supported(tmp_path, version):
    from app.core.store import _SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3

    source = tmp_path / "earlier.db"
    with sqlite3.connect(source) as db:
        for statements in (_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3)[:version]:
            for statement in statements:
                db.execute(statement)
        db.execute("INSERT INTO schema_metadata VALUES (1, ?)", (version,))
        db.execute(f"PRAGMA user_version={version}")
    assert _schema_version(source) == version
    destination = tmp_path / "snapshot.db"
    assert (
        create_sqlite_backup(source, destination, version, NOW).schema_version
        == version
    )
    assert _schema_version(destination) == version


def test_orphaned_foreign_keys_rejected_before_migration(tmp_path):
    path = tmp_path / "events.db"
    populated_v3(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE protected_state_provenance SET event_id='missing-event'")
    with pytest.raises(EventStoreError):
        EventStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (3,)
        assert not any(key[1] == "agent_reply_operations" for key in schema_objects(db))


def test_corrupt_revision_still_fails_closed_after_migration(tmp_path):
    path = tmp_path / "events.db"
    populated_v3(path)
    store = EventStore(path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(
            "UPDATE state_revisions SET previous_revision_number=12 WHERE revision_number=2"
        )
    with pytest.raises(EventStoreError):
        SelfStateStore(store).retrieve_private_revision_history(
            principal("a"), StateType.INTEREST, "interest-space"
        )
