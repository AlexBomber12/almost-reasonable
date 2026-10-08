from dataclasses import fields
from datetime import UTC, datetime, tzinfo
import sqlite3

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


def test_first_initialization_creates_versioned_wal_database(tmp_path):
    database = tmp_path / "events.sqlite3"

    store = EventStore(database, busy_timeout_ms=321)

    assert database.is_file()
    assert store.diagnostics().schema_version == 1
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


def test_schema_version_changed_before_initialization_lock_fails_closed(tmp_path):
    database = tmp_path / "events.sqlite3"

    def install_future_version(point):
        assert point is FaultPoint.BEFORE_INITIALIZATION_LOCK
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA user_version = 2")

    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="unsupported event store schema version: 2",
    ):
        EventStore(database, fault_injector=install_future_version)


def test_future_schema_version_is_not_silently_interpreted(tmp_path):
    database = tmp_path / "future.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 2")

    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="unsupported event store schema version: 2",
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
        connection.execute("UPDATE schema_metadata SET version = 2")

    with pytest.raises(
        UnsupportedSchemaVersionError,
        match="unsupported event store schema version: 2",
    ):
        EventStore(database)


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


def test_retrieval_database_errors_are_sanitized(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = EventStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE event_envelopes")

    with pytest.raises(EventStoreError, match="event store operation failed"):
        store.retrieve_private_events(principal("a"))


def test_connection_open_and_setup_errors_are_sanitized(tmp_path):
    with pytest.raises(EventStoreError, match="event store operation failed"):
        EventStore(tmp_path / "missing-parent" / "events.sqlite3")

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(EventStoreError, match="event store operation failed"):
        EventStore(corrupt)


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


@pytest.mark.parametrize("content", [None, "", "\ud800"])
def test_invalid_payload_content_is_rejected(tmp_path, content):
    store = EventStore(tmp_path / "events.sqlite3")
    event = private_input("bad-content", "placeholder")
    object.__setattr__(event, "content", content)

    with pytest.raises(EventValidationError, match="payload content"):
        store.append_private_input(principal("a"), event)


def test_invalid_store_configuration_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="busy_timeout_ms must be positive"):
        EventStore(tmp_path / "events.sqlite3", busy_timeout_ms=0)


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
