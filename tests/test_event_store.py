from dataclasses import fields
from datetime import UTC, datetime, timedelta, timezone, tzinfo
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from app.core import store as store_module
from app.core.events import (
    AudienceType,
    AuthenticatedSourceReference,
    EventAudience,
    EventChannel,
    EventType,
    ParticipantId,
    PrivateConversationEvent,
    PrivateInputEvent,
    Provenance,
    ProvenanceType,
    TrustedPrincipalContext,
    VerificationStatus,
)
from app.core.store import (
    AppendStatus,
    EventStore,
    EventStoreBusyError,
    EventStoreError,
    EventIdentityConflictError,
    EventValidationError,
    FaultPoint,
    UnsupportedSchemaVersionError,
)


NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def principal(name, *, owner=False, source=None):
    return TrustedPrincipalContext(
        principal_id=ParticipantId(f"participant-{name}"),
        authenticated_source=AuthenticatedSourceReference(source or f"session-{name}"),
        is_owner=owner,
    )


def private_input(
    name,
    content,
    *,
    source_message_id=None,
    audience=None,
    event_type=EventType.PARTICIPANT_INPUT,
    channel=EventChannel.PRIVATE_CHAT,
    provenance=None,
    occurred_at=NOW,
):
    return PrivateInputEvent(
        event_id=f"event-{name}",
        payload_id=f"payload-{name}",
        source_message_id=source_message_id or f"message-{name}",
        occurred_at=occurred_at,
        event_type=event_type,
        channel=channel,
        correlation_id=f"correlation-{name}",
        audience=audience or EventAudience.private_for(ParticipantId("participant-a")),
        provenance=provenance
        or Provenance(
            source_type=ProvenanceType.PARTICIPANT_ASSERTION,
            verification=VerificationStatus.UNVERIFIED,
        ),
        content=content,
    )


def table_count(path, table):
    with sqlite3.connect(path) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def sqlite_operational_error(code, message):
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    return error


class ScheduledPragmaConnection:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.result = None

    def execute(self, statement):
        assert statement == "PRAGMA journal_mode = WAL"
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.result = outcome
        return self

    def fetchone(self):
        return (self.result,)


def test_first_initialization_creates_versioned_wal_database(tmp_path):
    database = tmp_path / "events.sqlite3"

    store = EventStore(database, busy_timeout_ms=321)

    assert database.is_file()
    assert store.diagnostics().schema_version == 13
    assert store.diagnostics().journal_mode == "wal"
    assert store.diagnostics().foreign_keys_enabled is True
    assert store.diagnostics().busy_timeout_ms == 321
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "schema_metadata",
            "private_payloads",
            "event_envelopes",
            "protected_event_taints",
        } <= tables
        envelope_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(event_envelopes)")
        }
        assert "content" not in envelope_columns
        assert "payload_id" in envelope_columns
        primary_key_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(event_envelopes)")
            if row[5]
        }
        unique_indexes = [
            row[1]
            for row in connection.execute("PRAGMA index_list(event_envelopes)")
            if row[2]
        ]
        unique_column_sets = {
            tuple(
                row[2]
                for row in connection.execute(f"PRAGMA index_info('{index_name}')")
            )
            for index_name in unique_indexes
        }
        assert primary_key_columns == {"event_id"}
        assert (
            "audience_principal_id",
            "authenticated_source_ref",
            "source_message_id",
        ) in unique_column_sets
        assert (
            "authenticated_source_ref",
            "source_message_id",
        ) not in unique_column_sets


@pytest.mark.parametrize("preexisting_mode", [None, 0o644])
def test_database_and_wal_sidecars_are_owner_only(tmp_path, preexisting_mode):
    database = tmp_path / "events.sqlite3"
    if preexisting_mode is not None:
        database.touch()
        database.chmod(preexisting_mode)
    previous_umask = os.umask(0o022)
    connection = None
    try:
        store = EventStore(database)
        connection = store._open()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE schema_metadata SET version = version")
        modes = {
            path.name: stat.S_IMODE(path.stat().st_mode)
            for path in (
                database,
                tmp_path / "events.sqlite3-wal",
                tmp_path / "events.sqlite3-shm",
            )
        }
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()
        os.umask(previous_umask)

    assert modes == {
        "events.sqlite3": 0o600,
        "events.sqlite3-wal": 0o600,
        "events.sqlite3-shm": 0o600,
    }


def test_existing_wal_sidecars_are_tightened_before_reuse(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    holder = store._open()
    try:
        holder.execute("BEGIN")
        holder.execute("SELECT version FROM schema_metadata").fetchall()
        sidecars = (
            tmp_path / "events.sqlite3-wal",
            tmp_path / "events.sqlite3-shm",
        )
        for sidecar in sidecars:
            sidecar.chmod(0o644)

        reopened = EventStore(database)
        reopened.append_private_input(
            principal("a"), private_input("secured-sidecars", "private content")
        )

        assert [stat.S_IMODE(sidecar.stat().st_mode) for sidecar in sidecars] == [
            0o600,
            0o600,
        ]
    finally:
        holder.rollback()
        holder.close()


def test_invalid_wal_sidecar_is_rejected(tmp_path):
    database = tmp_path / "events.sqlite3"
    wal_path = tmp_path / "events.sqlite3-wal"
    wal_path.mkdir()

    with pytest.raises(EventStoreError, match="event store operation failed"):
        EventStore(database)


def test_nonregular_wal_sidecar_inode_is_rejected(tmp_path, monkeypatch):
    class DirectoryStatus:
        st_mode = stat.S_IFDIR | 0o700
        st_uid = os.geteuid()

    store = object.__new__(EventStore)
    store._database_path = tmp_path / "events.sqlite3"
    (tmp_path / "events.sqlite3-wal").touch()
    monkeypatch.setattr(store_module.os, "fstat", lambda _descriptor: DirectoryStatus())

    with pytest.raises(EventStoreError, match="event store operation failed"):
        store._secure_database_sidecars()


def test_wal_sidecar_permission_errors_are_sanitized(tmp_path, monkeypatch):
    store = object.__new__(EventStore)
    store._database_path = tmp_path / "events.sqlite3"
    (tmp_path / "events.sqlite3-wal").touch()

    def fail_to_chmod(*_args, **_kwargs):
        raise OSError("synthetic sidecar chmod failure")

    monkeypatch.setattr(store_module, "chmod_sqlite_metadata", fail_to_chmod)

    with pytest.raises(EventStoreError, match="event store operation failed"):
        store._secure_database_sidecars()


def test_sidecar_open_permission_errors_are_sanitized(tmp_path, monkeypatch):
    store = object.__new__(EventStore)
    store._database_path = tmp_path / "events.sqlite3"

    def denied(*_args, **_kwargs):
        raise PermissionError("synthetic sidecar access failure")

    monkeypatch.setattr(store_module, "sqlite_metadata_handle", denied)
    with pytest.raises(EventStoreError, match="event store operation failed"):
        store._secure_database_sidecars()


def test_database_in_an_untrusted_parent_is_rejected(tmp_path):
    shared_directory = tmp_path / "shared"
    shared_directory.mkdir(mode=0o777)
    shared_directory.chmod(0o777)

    with pytest.raises(EventStoreError, match="event store operation failed"):
        EventStore(shared_directory / "events.sqlite3")

    assert not (shared_directory / "events.sqlite3").exists()


def test_database_replacement_before_sqlite_open_is_rejected(tmp_path, monkeypatch):
    database = tmp_path / "events.sqlite3"
    real_connect = store_module.sqlite3.connect

    def replace_with_insecure_file(path, *args, **kwargs):
        database.chmod(0o644)
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", replace_with_insecure_file)

    with pytest.raises(EventStoreError, match="event store operation failed"):
        EventStore(database)

    assert stat.S_IMODE(database.stat().st_mode) == 0o644


def test_repeated_initialization_and_reopen_preserve_both_records(tmp_path):
    database = tmp_path / "events.sqlite3"
    context = principal("a")
    event = private_input("one", "synthetic private A content")

    first_store = EventStore(database)
    result = first_store.append_private_input(context, event)
    reopened_store = EventStore(database)

    assert result.status is AppendStatus.CREATED
    assert table_count(database, "event_envelopes") == 1
    assert table_count(database, "private_payloads") == 1
    assert reopened_store.retrieve_private_events(context) == (
        PrivateConversationEvent(
            event_id="event-one",
            occurred_at=NOW,
            event_type=EventType.PARTICIPANT_INPUT,
            channel=EventChannel.PRIVATE_CHAT,
            correlation_id="correlation-one",
            audience=AudienceType.PRIVATE_PARTICIPANT,
            provenance=Provenance(
                source_type=ProvenanceType.PARTICIPANT_ASSERTION,
                verification=VerificationStatus.UNVERIFIED,
            ),
            content="synthetic private A content",
        ),
    )


def test_relative_database_path_remains_bound_after_working_directory_change(
    tmp_path, monkeypatch
):
    original_directory = tmp_path / "original"
    other_directory = tmp_path / "other"
    original_directory.mkdir()
    other_directory.mkdir()
    monkeypatch.chdir(original_directory)
    store = EventStore("events.sqlite3")

    monkeypatch.chdir(other_directory)
    result = store.append_private_input(
        principal("a"), private_input("relative-path", "private content")
    )

    assert result.status is AppendStatus.CREATED
    assert store.database_path == original_directory / "events.sqlite3"
    assert not (other_directory / "events.sqlite3").exists()
    assert store.retrieve_private_events(principal("a"))[0].content == "private content"


def test_private_events_within_one_second_are_returned_chronologically(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    context = principal("a")
    start = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    fractionally_later = start.replace(microsecond=1)

    store.append_private_input(
        context,
        private_input("whole-second", "earlier", occurred_at=start),
    )
    store.append_private_input(
        context,
        private_input("fractional-second", "later", occurred_at=fractionally_later),
    )

    assert [event.content for event in store.retrieve_private_events(context)] == [
        "earlier",
        "later",
    ]
    with sqlite3.connect(database) as connection:
        stored_times = connection.execute(
            "SELECT occurred_at FROM event_envelopes ORDER BY occurred_at"
        ).fetchall()
    assert stored_times == [
        ("2026-01-02T03:04:05.000000Z",),
        ("2026-01-02T03:04:05.000001Z",),
    ]


def test_private_event_retrieval_returns_a_bounded_chronological_window(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    context = principal("a")
    window_size = store_module.MAX_PRIVATE_EVENTS_PER_RETRIEVAL
    for index in range(window_size + 1):
        store.append_private_input(
            context,
            private_input(
                f"window-{index:03d}",
                f"content-{index:03d}",
                occurred_at=NOW + timedelta(microseconds=index),
            ),
        )

    events = store.retrieve_private_events(context)

    assert len(events) == window_size
    assert [event.content for event in events] == [
        f"content-{index:03d}" for index in range(1, window_size + 1)
    ]


def test_schema_version_changed_before_initialization_lock_fails_closed(tmp_path):
    database = tmp_path / "events.sqlite3"

    def install_future_version(point):
        assert point is FaultPoint.BEFORE_INITIALIZATION_LOCK
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA user_version = 14")

    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="unsupported event store schema version: 14",
    ):
        EventStore(database, fault_injector=install_future_version)


def test_wal_transition_retries_busy_errors_until_success(monkeypatch):
    store = object.__new__(EventStore)
    store._busy_timeout_ms = 20
    connection = ScheduledPragmaConnection(
        [
            sqlite_operational_error(sqlite3.SQLITE_BUSY, "synthetic busy"),
            sqlite_operational_error(sqlite3.SQLITE_LOCKED, "synthetic locked"),
            "wal",
        ]
    )
    clock = iter([0.0, 0.0, 0.005])
    sleeps = []
    monkeypatch.setattr(store_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(store_module.time, "sleep", sleeps.append)

    store._enable_wal(connection)

    assert connection.calls == 3
    assert sleeps == [0.01, 0.01]


def test_wal_transition_busy_retry_stops_at_deadline(monkeypatch):
    store = object.__new__(EventStore)
    store._busy_timeout_ms = 10
    connection = ScheduledPragmaConnection(
        [
            sqlite_operational_error(sqlite3.SQLITE_BUSY, "first busy"),
            sqlite_operational_error(sqlite3.SQLITE_BUSY, "final busy"),
        ]
    )
    clock = iter([0.0, 0.0, 0.01])
    sleeps = []
    monkeypatch.setattr(store_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(store_module.time, "sleep", sleeps.append)

    with pytest.raises(sqlite3.OperationalError, match="final busy"):
        store._enable_wal(connection)

    assert connection.calls == 2
    assert sleeps == [0.01]


def test_wal_transition_does_not_retry_non_lock_errors(monkeypatch):
    store = object.__new__(EventStore)
    store._busy_timeout_ms = 10
    connection = ScheduledPragmaConnection(
        [sqlite_operational_error(sqlite3.SQLITE_IOERR, "synthetic I/O failure")]
    )
    sleeps = []
    monkeypatch.setattr(store_module.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(store_module.time, "sleep", sleeps.append)

    with pytest.raises(sqlite3.OperationalError, match="synthetic I/O failure"):
        store._enable_wal(connection)

    assert connection.calls == 1
    assert sleeps == []


def test_wal_transition_rejects_an_unchanged_journal_mode():
    store = object.__new__(EventStore)
    store._busy_timeout_ms = 10
    connection = ScheduledPragmaConnection(["delete"])

    with pytest.raises(EventStoreError, match="event store operation failed"):
        store._enable_wal(connection)

    assert connection.calls == 1


def test_future_schema_version_is_not_silently_interpreted(tmp_path):
    database = tmp_path / "future.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 14")

    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="unsupported event store schema version: 14",
    ):
        EventStore(database)


def test_missing_current_schema_is_reported_as_a_sanitized_store_error(tmp_path):
    database = tmp_path / "malformed.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 1")

    with pytest.raises(EventStoreError, match="event store operation failed"):
        EventStore(database)


def test_metadata_version_mismatch_fails_closed(tmp_path):
    database = tmp_path / "events.sqlite3"
    EventStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE schema_metadata SET version = 14")

    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="unsupported event store schema version: 14",
    ):
        EventStore(database)


def test_nonnumeric_schema_metadata_is_sanitized_at_every_boundary(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    store.append_private_input(
        principal("a"), private_input("metadata-canary", "private content")
    )
    metadata_canary = "private-metadata-canary"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE schema_metadata SET version = ?",
            (metadata_canary,),
        )

    operations = (
        lambda: EventStore(database),
        lambda: store.append_private_input(
            principal("a"), private_input("rejected", "not persisted")
        ),
        lambda: store.retrieve_private_events(principal("a")),
    )
    for operation in operations:
        with pytest.raises(EventStoreError) as captured:
            operation()
        assert str(captured.value) == "event store operation failed"
        assert metadata_canary not in repr(captured.value)

    assert table_count(database, "event_envelopes") == 1
    assert table_count(database, "private_payloads") == 1
    assert table_count(database, "protected_event_taints") == 1


def test_append_rechecks_future_schema_version_before_writing(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 14")

    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="unsupported event store schema version: 14",
    ):
        store.append_private_input(
            principal("a"), private_input("future-write", "not persisted")
        )

    assert table_count(database, "event_envelopes") == 0
    assert table_count(database, "private_payloads") == 0
    assert table_count(database, "protected_event_taints") == 0


def test_append_rechecks_schema_metadata_before_writing(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE schema_metadata SET version = 14")

    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="unsupported event store schema version: 14",
    ):
        store.append_private_input(
            principal("a"), private_input("metadata-write", "not persisted")
        )

    assert table_count(database, "event_envelopes") == 0
    assert table_count(database, "private_payloads") == 0
    assert table_count(database, "protected_event_taints") == 0


def test_retrieval_rechecks_future_schema_version_before_reading(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    store.append_private_input(
        principal("a"), private_input("future-read", "private canary")
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 14")

    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="unsupported event store schema version: 14",
    ):
        store.retrieve_private_events(principal("a"))


def test_retrieval_rechecks_schema_metadata_before_reading(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    store.append_private_input(
        principal("a"), private_input("metadata-read", "private canary")
    )
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE schema_metadata SET version = 14")

    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="unsupported event store schema version: 14",
    ):
        store.retrieve_private_events(principal("a"))


def test_private_participants_are_isolated_and_owner_has_no_bypass(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    participant_a = principal("a", owner=True)
    participant_b = principal("b")
    a_event = private_input("a", "A-only synthetic canary")
    b_event = private_input(
        "b",
        "B-only synthetic canary",
        audience=EventAudience.private_for(ParticipantId("participant-b")),
    )

    store.append_private_input(participant_a, a_event)
    store.append_private_input(participant_b, b_event)

    a_view = store.retrieve_private_events(participant_a)
    b_view = store.retrieve_private_events(participant_b)
    assert [record.content for record in a_view] == ["A-only synthetic canary"]
    assert [record.content for record in b_view] == ["B-only synthetic canary"]
    assert "B-only" not in repr(a_view)
    assert "A-only" not in repr(b_view)


def test_conversation_view_excludes_storage_and_protected_taint_metadata(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    context = principal("a")
    store.append_private_input(context, private_input("one", "private text"))

    [record] = store.retrieve_private_events(context)
    visible_fields = {field.name for field in fields(record)}

    assert visible_fields == {
        "event_id",
        "occurred_at",
        "event_type",
        "channel",
        "correlation_id",
        "audience",
        "provenance",
        "content",
    }
    assert table_count(database, "protected_event_taints") == 1
    assert "participant-a" not in repr(record)
    assert "session-a" not in repr(record)
    assert "message-one" not in repr(record)
    assert "payload-one" not in repr(record)


def test_payload_can_be_removed_without_erasing_envelope_or_taint(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    context = principal("a")
    store.append_private_input(context, private_input("one", "removable private text"))

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "DELETE FROM private_payloads WHERE payload_id = 'payload-one'"
        )
        payload_reference = connection.execute(
            "SELECT payload_id FROM event_envelopes WHERE event_id = 'event-one'"
        ).fetchone()[0]

    assert payload_reference is None
    assert table_count(database, "event_envelopes") == 1
    assert table_count(database, "protected_event_taints") == 1
    assert store.retrieve_private_events(context) == ()


@pytest.mark.parametrize("bad_context", [None, object(), "participant-a"])
def test_missing_or_invalid_context_is_rejected(tmp_path, bad_context):
    store = EventStore(tmp_path / "events.sqlite3")

    with pytest.raises(EventValidationError, match="trusted principal context"):
        store.retrieve_private_events(bad_context)


@pytest.mark.parametrize(
    "bad_context",
    [
        TrustedPrincipalContext(
            "participant-a", AuthenticatedSourceReference("source")
        ),
        TrustedPrincipalContext(ParticipantId("participant-a"), "source"),
        TrustedPrincipalContext(
            ParticipantId("participant-a"),
            AuthenticatedSourceReference("source"),
            is_owner="yes",
        ),
    ],
)
def test_malformed_typed_context_is_rejected(tmp_path, bad_context):
    store = EventStore(tmp_path / "events.sqlite3")

    with pytest.raises(EventValidationError):
        store.retrieve_private_events(bad_context)


def test_audience_must_match_the_trusted_principal(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")

    with pytest.raises(EventValidationError, match="audience principal"):
        store.append_private_input(
            principal("a"),
            private_input(
                "wrong-scope",
                "not persisted",
                audience=EventAudience.private_for(ParticipantId("participant-b")),
            ),
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"event_type": "unknown"}, "event type"),
        ({"channel": "unknown"}, "event channel"),
        (
            {"audience": EventAudience("public", ParticipantId("participant-a"))},
            "audience type",
        ),
        (
            {"provenance": Provenance("unknown", VerificationStatus.UNVERIFIED)},
            "provenance type",
        ),
        (
            {"provenance": Provenance(ProvenanceType.PARTICIPANT_ASSERTION, "unknown")},
            "verification status",
        ),
    ],
)
def test_unknown_event_categories_fail_closed(tmp_path, changes, message):
    store = EventStore(tmp_path / "events.sqlite3")
    event = private_input("invalid", "not persisted", **changes)

    with pytest.raises(EventValidationError, match=message):
        store.append_private_input(principal("a"), event)

    assert table_count(store.database_path, "event_envelopes") == 0
    assert table_count(store.database_path, "private_payloads") == 0


@pytest.mark.parametrize(
    "provenance",
    [
        Provenance(ProvenanceType.DIRECT_OBSERVATION, VerificationStatus.UNVERIFIED),
        Provenance(ProvenanceType.MODEL_INFERENCE, VerificationStatus.UNVERIFIED),
        Provenance(
            ProvenanceType.INDEPENDENTLY_VERIFIED_CLAIM,
            VerificationStatus.VERIFIED,
        ),
        Provenance(ProvenanceType.PARTICIPANT_ASSERTION, VerificationStatus.VERIFIED),
    ],
)
def test_private_participant_input_can_only_begin_as_unverified_assertion(
    tmp_path, provenance
):
    store = EventStore(tmp_path / "events.sqlite3")

    with pytest.raises(EventValidationError, match="unverified participant assertion"):
        store.append_private_input(
            principal("a"),
            private_input("provenance", "not knowledge", provenance=provenance),
        )


def test_schema_constraints_reject_scope_or_provenance_broadening(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    store.append_private_input(principal("a"), private_input("one", "private"))

    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE event_envelopes SET audience_type = 'public'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE private_payloads SET provenance_type = 'invented'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE event_envelopes SET verification_status = 'verified'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE schema_metadata SET version = 'invalid'")


def test_foreign_key_enforcement_rejects_an_orphan_taint(tmp_path):
    database = tmp_path / "events.sqlite3"
    EventStore(database)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO protected_event_taints(
                    event_id,
                    source_event_id,
                    source_principal_id,
                    source_provenance_type
                ) VALUES ('missing', 'missing', 'synthetic', 'participant_assertion')
                """
            )


def test_legitimate_same_principal_source_message_replay_is_idempotent(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    context = principal("a")
    original = private_input("one", "original private value")

    assert store.append_private_input(context, original).status is AppendStatus.CREATED
    assert (
        store.append_private_input(context, original).status is AppendStatus.DUPLICATE
    )
    replay = private_input(
        "two",
        "changed replay value",
        source_message_id=original.source_message_id,
    )
    assert store.append_private_input(context, replay).status is AppendStatus.DUPLICATE

    assert table_count(database, "event_envelopes") == 1
    assert table_count(database, "private_payloads") == 1
    assert store.retrieve_private_events(context)[0].content == "original private value"


def test_same_source_message_key_is_independent_between_principals(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    participant_a = principal("a", source="shared-source")
    participant_b = principal("b", source="shared-source")
    event_a = private_input(
        "a-shared-key",
        "A private collision canary",
        source_message_id="shared-message",
    )
    event_b = private_input(
        "b-shared-key",
        "B private collision canary",
        source_message_id="shared-message",
        audience=EventAudience.private_for(ParticipantId("participant-b")),
    )

    assert (
        store.append_private_input(participant_a, event_a).status
        is AppendStatus.CREATED
    )
    assert (
        store.append_private_input(participant_b, event_b).status
        is AppendStatus.CREATED
    )

    a_view = store.retrieve_private_events(participant_a)
    b_view = store.retrieve_private_events(participant_b)
    assert [record.content for record in a_view] == ["A private collision canary"]
    assert [record.content for record in b_view] == ["B private collision canary"]
    assert "B private" not in repr(a_view)
    assert "A private" not in repr(b_view)
    assert table_count(database, "event_envelopes") == 2
    assert table_count(database, "private_payloads") == 2
    assert table_count(database, "protected_event_taints") == 2


def test_global_event_id_collision_across_principals_fails_without_partial_write(
    tmp_path,
):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    participant_a = principal("a", source="shared-source")
    participant_b = principal("b", source="shared-source")
    original = private_input("global-id", "A original private canary")
    collision = private_input(
        "b-collision",
        "B rejected private canary",
        source_message_id="different-message",
        audience=EventAudience.private_for(ParticipantId("participant-b")),
    )
    object.__setattr__(collision, "event_id", original.event_id)
    store.append_private_input(participant_a, original)

    with pytest.raises(EventIdentityConflictError) as captured:
        store.append_private_input(participant_b, collision)

    assert str(captured.value) == "event identity conflict"
    assert "event-global-id" not in repr(captured.value)
    assert "B rejected private canary" not in repr(captured.value)
    assert table_count(database, "event_envelopes") == 1
    assert table_count(database, "private_payloads") == 1
    assert table_count(database, "protected_event_taints") == 1
    assert [
        record.content for record in store.retrieve_private_events(participant_a)
    ] == ["A original private canary"]
    assert store.retrieve_private_events(participant_b) == ()


def test_event_id_collision_with_different_same_principal_source_fails_privately(
    tmp_path,
):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    participant_a = principal("a")
    participant_b = principal("b")
    original = private_input("same-principal-id", "A retained private canary")
    collision = private_input(
        "different-source",
        "A rejected private canary",
        source_message_id="different-source-message",
    )
    object.__setattr__(collision, "event_id", original.event_id)
    store.append_private_input(participant_a, original)

    with pytest.raises(EventIdentityConflictError, match="event identity conflict"):
        store.append_private_input(participant_a, collision)

    assert table_count(database, "event_envelopes") == 1
    assert table_count(database, "private_payloads") == 1
    assert table_count(database, "protected_event_taints") == 1
    a_view = store.retrieve_private_events(participant_a)
    assert [record.content for record in a_view] == ["A retained private canary"]
    assert "rejected" not in repr(a_view)
    assert store.retrieve_private_events(participant_b) == ()


def test_transaction_rolls_back_payload_when_envelope_write_fails(tmp_path):
    database = tmp_path / "events.sqlite3"

    class SyntheticFailure(Exception):
        pass

    def fail_after_payload(point):
        if point is FaultPoint.BEFORE_INITIALIZATION_LOCK:
            return
        assert point is FaultPoint.AFTER_PAYLOAD_INSERT
        raise SyntheticFailure("controlled fault")

    store = EventStore(database, fault_injector=fail_after_payload)

    with pytest.raises(SyntheticFailure, match="controlled fault"):
        store.append_private_input(
            principal("a"), private_input("rollback", "synthetic private value")
        )

    assert table_count(database, "event_envelopes") == 0
    assert table_count(database, "private_payloads") == 0
    assert table_count(database, "protected_event_taints") == 0


def test_database_errors_are_sanitized_without_private_content(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE private_payloads")

    secret = "synthetic-private-canary-never-in-error"
    with pytest.raises(EventStoreError) as captured:
        store.append_private_input(principal("a"), private_input("broken", secret))

    assert str(captured.value) == "event store operation failed"
    assert secret not in repr(captured.value)


def test_retrieval_database_errors_are_sanitized(tmp_path, monkeypatch):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    original_connect = sqlite3.connect

    class FailingReadConnection(sqlite3.Connection):
        def execute(self, statement, parameters=()):
            if "FROM event_envelopes AS event" in statement:
                raise sqlite3.OperationalError("synthetic private query failure")
            return super().execute(statement, parameters)

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: original_connect(
            *args, **kwargs, factory=FailingReadConnection
        ),
    )

    with pytest.raises(EventStoreError, match="event store operation failed"):
        store.retrieve_private_events(principal("a"))


def test_malformed_persisted_rows_are_sanitized_during_retrieval(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    store.append_private_input(
        principal("a"), private_input("malformed-row", "private content")
    )
    persisted_canary = "malformed-persisted-timestamp-canary"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE event_envelopes SET occurred_at = ?",
            (persisted_canary,),
        )

    with pytest.raises(EventStoreError) as captured:
        store.retrieve_private_events(principal("a"))

    assert str(captured.value) == "event store operation failed"
    assert persisted_canary not in repr(captured.value)
    assert "private content" not in repr(captured.value)


@pytest.mark.parametrize(
    "persisted_timestamp",
    [
        "2026-01-02T03:04:05.000000",
        "2026-01-02T03:04:05.000000+00:00",
    ],
)
def test_noncanonical_persisted_timestamps_are_sanitized(tmp_path, persisted_timestamp):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    store.append_private_input(
        principal("a"), private_input("noncanonical-time", "private content")
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE event_envelopes SET occurred_at = ?",
            (persisted_timestamp,),
        )

    with pytest.raises(EventStoreError) as captured:
        store.retrieve_private_events(principal("a"))

    assert str(captured.value) == "event store operation failed"
    assert persisted_timestamp not in repr(captured.value)
    assert "private content" not in repr(captured.value)


def test_blob_payload_is_rejected_during_retrieval(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    store.append_private_input(principal("a"), private_input("blob-row", "replace me"))
    persisted_canary = b"private-blob-canary"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE private_payloads SET content = ?",
            (persisted_canary,),
        )

    with pytest.raises(EventStoreError) as captured:
        store.retrieve_private_events(principal("a"))

    assert str(captured.value) == "event store operation failed"
    assert repr(persisted_canary) not in repr(captured.value)


def test_oversized_persisted_payload_is_sanitized_before_retrieval(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    store.append_private_input(
        principal("a"), private_input("oversized-row", "replace me")
    )
    oversized_content = "x" * (store_module.MAX_PRIVATE_PAYLOAD_BYTES + 1)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE private_payloads SET content = ?",
            (oversized_content,),
        )

    with pytest.raises(EventStoreError) as captured:
        store.retrieve_private_events(principal("a"))

    assert str(captured.value) == "event store operation failed"
    assert oversized_content not in repr(captured.value)


@pytest.mark.parametrize(
    "corruption",
    [
        "UPDATE event_envelopes SET verification_status = 'verified'",
        "UPDATE private_payloads SET provenance_type = 'direct_observation'",
    ],
)
def test_impossible_persisted_provenance_is_sanitized(tmp_path, corruption):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    store.append_private_input(
        principal("a"), private_input("invalid-provenance", "private content")
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(corruption)

    with pytest.raises(EventStoreError) as captured:
        store.retrieve_private_events(principal("a"))

    assert str(captured.value) == "event store operation failed"
    assert "private content" not in repr(captured.value)


@pytest.mark.parametrize(
    "corruption",
    [
        "DELETE FROM protected_event_taints",
        "UPDATE protected_event_taints SET source_principal_id = 'participant-b'",
    ],
)
def test_missing_or_inconsistent_protected_taint_is_sanitized(tmp_path, corruption):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    store.append_private_input(
        principal("a"), private_input("invalid-taint", "private content")
    )
    with sqlite3.connect(database) as connection:
        connection.execute(corruption)

    with pytest.raises(EventStoreError) as captured:
        store.retrieve_private_events(principal("a"))

    assert str(captured.value) == "event store operation failed"
    assert "private content" not in repr(captured.value)


def test_connection_open_and_setup_errors_are_sanitized(tmp_path):
    with pytest.raises(EventStoreError, match="event store operation failed"):
        EventStore(tmp_path / "missing-parent" / "events.sqlite3")

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(EventStoreError, match="event store operation failed"):
        EventStore(corrupt)


def test_sqlite_connection_open_errors_are_sanitized(tmp_path, monkeypatch):
    def fail_to_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic open failure")

    monkeypatch.setattr(store_module.sqlite3, "connect", fail_to_connect)

    with pytest.raises(EventStoreError, match="event store operation failed"):
        EventStore(tmp_path / "events.sqlite3")


def test_secure_database_open_errors_are_sanitized(tmp_path, monkeypatch):
    def fail_to_open(*_args, **_kwargs):
        raise OSError("synthetic secure open failure")

    monkeypatch.setattr(store_module.os, "open", fail_to_open)

    with pytest.raises(EventStoreError, match="event store operation failed"):
        EventStore(tmp_path / "events.sqlite3")


def test_post_connect_file_stat_errors_are_sanitized(tmp_path, monkeypatch):
    database = tmp_path / "events.sqlite3"
    real_stat = Path.stat

    def fail_database_stat(path, *args, **kwargs):
        if path == database:
            raise OSError("synthetic file stat failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_database_stat)

    with pytest.raises(EventStoreError, match="event store operation failed"):
        EventStore(database)


def test_connection_pragma_failure_is_sanitized_and_closed(tmp_path, monkeypatch):
    closed = []

    class BrokenSetupConnection:
        def execute(self, _statement):
            raise sqlite3.DatabaseError("synthetic pragma failure")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        store_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: BrokenSetupConnection(),
    )

    with pytest.raises(EventStoreError, match="event store operation failed"):
        EventStore(tmp_path / "events.sqlite3")

    assert closed == [True]


def test_diagnostic_database_errors_are_sanitized(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "events.sqlite3")

    class BrokenConnection:
        def execute(self, _statement):
            raise sqlite3.DatabaseError("synthetic diagnostic failure")

        def close(self):
            pass

    monkeypatch.setattr(store, "_open", BrokenConnection)

    with pytest.raises(EventStoreError, match="event store operation failed"):
        store.diagnostics()


def test_deterministic_busy_conflict_does_not_corrupt_history(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database, busy_timeout_ms=1)
    context = principal("a")

    with sqlite3.connect(database, isolation_level=None) as locker:
        locker.execute("BEGIN IMMEDIATE")
        with pytest.raises(EventStoreBusyError, match="event store is busy"):
            store.append_private_input(
                context, private_input("busy", "not partially persisted")
            )
        locker.execute("ROLLBACK")

    assert table_count(database, "event_envelopes") == 0
    assert table_count(database, "private_payloads") == 0
    assert (
        store.append_private_input(
            context, private_input("after-busy", "written after conflict")
        ).status
        is AppendStatus.CREATED
    )
    assert [event.event_id for event in store.retrieve_private_events(context)] == [
        "event-after-busy"
    ]


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("event_id", ""),
        ("payload_id", object()),
        ("source_message_id", "contains spaces"),
        ("correlation_id", "x" * 129),
    ],
)
def test_invalid_identifiers_are_rejected(tmp_path, field_name, bad_value):
    store = EventStore(tmp_path / "events.sqlite3")
    event = private_input("invalid-id", "not persisted")
    object.__setattr__(event, field_name, bad_value)

    with pytest.raises(EventValidationError, match=field_name.replace("_", " ")):
        store.append_private_input(principal("a"), event)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"occurred_at": "not-a-time"}, "timestamp"),
        ({"occurred_at": datetime(2026, 1, 2)}, "timezone"),
    ],
)
def test_invalid_timestamps_are_rejected(tmp_path, changes, message):
    store = EventStore(tmp_path / "events.sqlite3")

    with pytest.raises(EventValidationError, match=message):
        store.append_private_input(
            principal("a"), private_input("bad-time", "not persisted", **changes)
        )


def test_timezone_with_no_utc_offset_is_rejected(tmp_path):
    class MissingOffset(tzinfo):
        def utcoffset(self, _value):
            return None

    store = EventStore(tmp_path / "events.sqlite3")
    timestamp = datetime(2026, 1, 2, tzinfo=MissingOffset())

    with pytest.raises(EventValidationError, match="timezone"):
        store.append_private_input(
            principal("a"),
            private_input("missing-offset", "not persisted", occurred_at=timestamp),
        )


def test_timestamp_that_overflows_during_utc_conversion_is_rejected(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    timestamp = datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))

    with pytest.raises(EventValidationError, match="represented in UTC"):
        store.append_private_input(
            principal("a"),
            private_input("utc-overflow", "not persisted", occurred_at=timestamp),
        )

    assert table_count(database, "event_envelopes") == 0
    assert table_count(database, "private_payloads") == 0
    assert table_count(database, "protected_event_taints") == 0


def test_timestamp_with_failing_utc_offset_is_rejected(tmp_path):
    class FailingOffset(tzinfo):
        def utcoffset(self, _value):
            raise ValueError("synthetic offset failure")

    store = EventStore(tmp_path / "events.sqlite3")
    timestamp = datetime(2026, 1, 2, tzinfo=FailingOffset())

    with pytest.raises(EventValidationError, match="represented in UTC"):
        store.append_private_input(
            principal("a"),
            private_input("failing-offset", "not persisted", occurred_at=timestamp),
        )


def test_timestamp_with_malformed_utc_offset_is_rejected(tmp_path):
    class MalformedOffset(tzinfo):
        def utcoffset(self, _value):
            return "not-an-offset"

    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    timestamp = datetime(2026, 1, 2, tzinfo=MalformedOffset())

    with pytest.raises(EventValidationError, match="represented in UTC"):
        store.append_private_input(
            principal("a"),
            private_input("malformed-offset", "not persisted", occurred_at=timestamp),
        )

    assert table_count(database, "event_envelopes") == 0
    assert table_count(database, "private_payloads") == 0
    assert table_count(database, "protected_event_taints") == 0


def test_timestamp_that_becomes_malformed_during_utc_conversion_is_rejected(tmp_path):
    class LateMalformedOffset(tzinfo):
        calls = 0

        def utcoffset(self, _value):
            self.calls += 1
            if self.calls == 1:
                return timedelta(0)
            return "not-an-offset"

    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    timestamp = datetime(2026, 1, 2, tzinfo=LateMalformedOffset())

    with pytest.raises(EventValidationError, match="represented in UTC"):
        store.append_private_input(
            principal("a"),
            private_input(
                "late-malformed-offset", "not persisted", occurred_at=timestamp
            ),
        )

    assert table_count(database, "event_envelopes") == 0
    assert table_count(database, "private_payloads") == 0
    assert table_count(database, "protected_event_taints") == 0


@pytest.mark.parametrize("content", [None, "", "\ud800"])
def test_invalid_payload_content_is_rejected(tmp_path, content):
    store = EventStore(tmp_path / "events.sqlite3")
    event = private_input("bad-content", "placeholder")
    object.__setattr__(event, "content", content)

    with pytest.raises(EventValidationError, match="payload content"):
        store.append_private_input(principal("a"), event)


def test_payload_at_byte_limit_is_persisted(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    content = "a" * store_module.MAX_PRIVATE_PAYLOAD_BYTES

    result = store.append_private_input(
        principal("a"), private_input("max-content", content)
    )

    assert result.status is AppendStatus.CREATED
    assert store.retrieve_private_events(principal("a"))[0].content == content


@pytest.mark.parametrize(
    "content",
    [
        "a" * (store_module.MAX_PRIVATE_PAYLOAD_BYTES + 1),
        "é" * (store_module.MAX_PRIVATE_PAYLOAD_BYTES // 2 + 1),
    ],
)
def test_payload_over_byte_limit_is_rejected_without_writes(tmp_path, content):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)

    with pytest.raises(EventValidationError, match="payload content"):
        store.append_private_input(
            principal("a"), private_input("oversized-content", content)
        )

    assert table_count(database, "event_envelopes") == 0
    assert table_count(database, "private_payloads") == 0
    assert table_count(database, "protected_event_taints") == 0


@pytest.mark.parametrize("busy_timeout_ms", [0, True, 1.5, 2_147_483_648])
def test_invalid_store_configuration_is_rejected(tmp_path, busy_timeout_ms):
    with pytest.raises(ValueError, match="busy_timeout_ms is outside SQLite"):
        EventStore(tmp_path / "events.sqlite3", busy_timeout_ms=busy_timeout_ms)


def test_in_memory_database_is_rejected():
    with pytest.raises(ValueError, match="in-memory databases are not supported"):
        EventStore(":memory:")


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (object(), "private input event"),
        ("audience", "event audience"),
        (
            EventAudience(AudienceType.PRIVATE_PARTICIPANT, "participant-a"),
            "audience principal",
        ),
        ("provenance", "event provenance"),
    ],
)
def test_malformed_event_objects_are_rejected(tmp_path, replacement, message):
    store = EventStore(tmp_path / "events.sqlite3")
    event = private_input("malformed", "not persisted")
    if message == "private input event":
        event = replacement
    elif message.startswith("event audience") or message.startswith(
        "audience principal"
    ):
        object.__setattr__(event, "audience", replacement)
    else:
        object.__setattr__(event, "provenance", replacement)

    with pytest.raises(EventValidationError, match=message):
        store.append_private_input(principal("a"), event)


def test_sidecar_unlinked_by_last_sqlite_close_during_security_check(
    tmp_path, monkeypatch
):
    store = EventStore(tmp_path / "events.sqlite3")
    holder = store._open()
    holder.execute("SELECT version FROM schema_metadata").fetchone()
    real_handle = store_module.sqlite_metadata_handle

    def close_last_connection_after_pin(path, **kwargs):
        descriptor = real_handle(path, **kwargs)
        if str(path).endswith("-wal"):
            holder.close()  # SQLite normally removes both optional sidecars.
        return descriptor

    monkeypatch.setattr(
        store_module, "sqlite_metadata_handle", close_last_connection_after_pin
    )
    try:
        store._secure_database_sidecars()
    finally:
        holder.close()
    assert not Path(str(store.database_path) + "-wal").exists()
