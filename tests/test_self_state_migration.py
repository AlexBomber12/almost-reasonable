from datetime import UTC, datetime
import sqlite3
from threading import Barrier, Thread

import pytest

from app.core import store as store_module
from app.core.store import EventStore, FaultPoint, UnsupportedSchemaVersionError


def create_v1_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in store_module._SCHEMA_V1:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_metadata(singleton, version) VALUES (1, 1)"
        )
        connection.execute(
            """
            INSERT INTO private_payloads(
                payload_id, content, audience_type, audience_principal_id,
                provenance_type, verification_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "payload-v1",
                "synthetic v1 private content",
                "private_participant",
                "participant-a",
                "participant_assertion",
                "unverified",
                "2026-01-02T03:04:05.000000Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO event_envelopes(
                event_id, occurred_at, event_type, channel, correlation_id,
                authenticated_source_ref, source_message_id, audience_type,
                audience_principal_id, provenance_type, verification_status,
                payload_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "event-v1",
                "2026-01-02T03:04:05.000000Z",
                "participant_input",
                "private_chat",
                "correlation-v1",
                "session-a",
                "message-v1",
                "private_participant",
                "participant-a",
                "participant_assertion",
                "unverified",
                "payload-v1",
            ),
        )
        connection.execute(
            """
            INSERT INTO protected_event_taints(
                event_id, source_event_id, source_principal_id,
                source_provenance_type
            ) VALUES (?, ?, ?, ?)
            """,
            ("event-v1", "event-v1", "participant-a", "participant_assertion"),
        )
        connection.execute("PRAGMA user_version = 1")


def test_v1_migrates_atomically_and_preserves_event_data(tmp_path):
    database = tmp_path / "events.sqlite3"
    create_v1_database(database)

    store = EventStore(database)
    reopened = EventStore(database)

    assert store.diagnostics().schema_version == 14
    assert reopened.diagnostics().schema_version == 14
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (14,)
        assert connection.execute("SELECT version FROM schema_metadata").fetchone() == (
            14,
        )
        assert connection.execute(
            "SELECT content, audience_principal_id FROM private_payloads"
        ).fetchone() == ("synthetic v1 private content", "participant-a")
        assert connection.execute(
            "SELECT event_id, payload_id FROM event_envelopes"
        ).fetchone() == ("event-v1", "payload-v1")
        assert connection.execute(
            "SELECT source_event_id, source_principal_id FROM protected_event_taints"
        ).fetchone() == ("event-v1", "participant-a")
        assert connection.execute("SELECT COUNT(*) FROM state_items").fetchone() == (0,)


def test_failed_v1_migration_leaves_recoverable_v1_database(tmp_path):
    database = tmp_path / "events.sqlite3"
    create_v1_database(database)

    def fail(point):
        if point is FaultPoint.BEFORE_INITIALIZATION_LOCK:
            return
        assert point is FaultPoint.BEFORE_V2_MIGRATION_COMMIT
        raise RuntimeError("synthetic migration failure")

    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        EventStore(database, fault_injector=fail)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("SELECT version FROM schema_metadata").fetchone() == (
            1,
        )
        assert connection.execute(
            "SELECT content FROM private_payloads"
        ).fetchone() == ("synthetic v1 private content",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "state_items" not in tables

    recovered = EventStore(database)
    assert recovered.diagnostics().schema_version == 14


def test_failed_v3_step_rolls_back_the_combined_v1_migration(tmp_path):
    database = tmp_path / "events.sqlite3"
    create_v1_database(database)

    def fail(point):
        if point in {
            FaultPoint.BEFORE_INITIALIZATION_LOCK,
            FaultPoint.BEFORE_V2_MIGRATION_COMMIT,
        }:
            return
        assert point is FaultPoint.BEFORE_V3_MIGRATION_COMMIT
        raise RuntimeError("synthetic v3 step failure")

    with pytest.raises(RuntimeError, match="synthetic v3 step failure"):
        EventStore(database, fault_injector=fail)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("SELECT version FROM schema_metadata").fetchone() == (
            1,
        )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "state_items" not in tables
        assert "provider_usage_attempts" not in tables


def test_migration_rejects_future_versions(tmp_path):
    database = tmp_path / "future.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 15")

    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="unsupported event store schema version: 15",
    ):
        EventStore(database)


def test_new_database_contains_state_schema_without_seed_state(tmp_path):
    database = tmp_path / "new.sqlite3"
    EventStore(database)

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "state_items",
            "state_operations",
            "state_revisions",
            "state_revision_payloads",
            "state_payload_alternatives",
            "state_payload_constraints",
            "protected_state_provenance",
        } <= tables
        assert connection.execute("SELECT COUNT(*) FROM state_items").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM state_revisions"
        ).fetchone() == (0,)


def test_migration_preserves_canonical_timestamp_text(tmp_path):
    database = tmp_path / "events.sqlite3"
    create_v1_database(database)
    EventStore(database)
    expected = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT occurred_at FROM event_envelopes WHERE event_id = 'event-v1'"
        ).fetchone()[0]
    assert datetime.fromisoformat(stored) == expected


def test_concurrent_v1_initialization_serializes_one_migration(tmp_path):
    database = tmp_path / "events.sqlite3"
    create_v1_database(database)
    barrier = Barrier(3)
    outcomes = []

    def initialize():
        barrier.wait()
        try:
            outcomes.append(EventStore(database).diagnostics().schema_version)
        except Exception as error:  # pragma-free assertion captures thread failures.
            outcomes.append(error)

    threads = [Thread(target=initialize) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert outcomes == [14, 14]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version FROM schema_metadata").fetchone() == (
            14,
        )
        assert connection.execute("SELECT COUNT(*) FROM state_items").fetchone() == (0,)
