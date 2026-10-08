"""Application-owned SQLite storage for participant-private input events."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .events import (
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


SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5_000
MAX_BUSY_TIMEOUT_MS = 2_147_483_647
MAX_PRIVATE_PAYLOAD_BYTES = 64 * 1024
MAX_PRIVATE_EVENTS_PER_RETRIEVAL = 100
WAL_RETRY_INTERVAL_SECONDS = 0.01
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class EventStoreError(RuntimeError):
    """A sanitized event-store failure that contains no persisted content."""


class EventStoreBusyError(EventStoreError):
    """The configured SQLite lock wait expired."""


class EventValidationError(ValueError):
    """Input at the internal storage boundary failed closed validation."""


class EventIdentityConflictError(EventStoreError):
    """A global event ID is already assigned to another logical event."""


class UnsupportedSchemaVersionError(EventStoreError):
    """The database schema is not understood by this application version."""

    def __init__(self, version: int):
        super().__init__(f"unsupported event store schema version: {version}")


class AppendStatus(StrEnum):
    CREATED = "created"
    DUPLICATE = "duplicate"


class FaultPoint(StrEnum):
    """Deterministic fault locations used by local transactional tests."""

    BEFORE_INITIALIZATION_LOCK = "before_initialization_lock"
    AFTER_PAYLOAD_INSERT = "after_payload_insert"


@dataclass(frozen=True, slots=True)
class AppendResult:
    status: AppendStatus


@dataclass(frozen=True, slots=True)
class StoreDiagnostics:
    schema_version: int
    journal_mode: str
    foreign_keys_enabled: bool
    busy_timeout_ms: int


_SCHEMA = (
    """
    CREATE TABLE schema_metadata (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        version INTEGER NOT NULL
            CHECK (typeof(version) = 'integer' AND version >= 1)
    )
    """,
    f"""
    CREATE TABLE private_payloads (
        payload_id TEXT PRIMARY KEY,
        content TEXT NOT NULL CHECK (
            typeof(content) = 'text'
            AND length(CAST(content AS BLOB)) BETWEEN 1
                AND {MAX_PRIVATE_PAYLOAD_BYTES}
        ),
        audience_type TEXT NOT NULL
            CHECK (audience_type = 'private_participant'),
        audience_principal_id TEXT NOT NULL,
        provenance_type TEXT NOT NULL
            CHECK (provenance_type = 'participant_assertion'),
        verification_status TEXT NOT NULL
            CHECK (verification_status = 'unverified'),
        created_at TEXT NOT NULL,
        retention_status TEXT NOT NULL DEFAULT 'active'
            CHECK (retention_status IN ('active', 'deleted'))
    )
    """,
    """
    CREATE TABLE event_envelopes (
        event_id TEXT PRIMARY KEY,
        occurred_at TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (event_type = 'participant_input'),
        channel TEXT NOT NULL CHECK (channel = 'private_chat'),
        correlation_id TEXT NOT NULL,
        authenticated_source_ref TEXT NOT NULL,
        source_message_id TEXT NOT NULL,
        audience_type TEXT NOT NULL
            CHECK (audience_type = 'private_participant'),
        audience_principal_id TEXT NOT NULL,
        provenance_type TEXT NOT NULL
            CHECK (provenance_type = 'participant_assertion'),
        verification_status TEXT NOT NULL
            CHECK (verification_status = 'unverified'),
        payload_id TEXT UNIQUE
            REFERENCES private_payloads(payload_id) ON DELETE SET NULL,
        UNIQUE (
            audience_principal_id,
            authenticated_source_ref,
            source_message_id
        )
    )
    """,
    """
    CREATE TABLE protected_event_taints (
        event_id TEXT PRIMARY KEY
            REFERENCES event_envelopes(event_id) ON DELETE CASCADE,
        source_event_id TEXT NOT NULL
            REFERENCES event_envelopes(event_id),
        source_principal_id TEXT NOT NULL,
        source_provenance_type TEXT NOT NULL
            CHECK (source_provenance_type = 'participant_assertion')
    )
    """,
    """
    CREATE INDEX event_envelopes_private_scope_time
    ON event_envelopes(audience_type, audience_principal_id, occurred_at, event_id)
    """,
)


class EventStore:
    """Purpose-specific private event persistence and retrieval boundary."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        fault_injector: Callable[[FaultPoint], None] | None = None,
    ):
        if (
            type(busy_timeout_ms) is not int
            or not 1 <= busy_timeout_ms <= MAX_BUSY_TIMEOUT_MS
        ):
            raise ValueError("busy_timeout_ms is outside SQLite's supported range")
        database_path = Path(database_path)
        if database_path == Path(":memory:"):
            raise ValueError("in-memory databases are not supported")
        self._database_path = database_path.resolve()
        self._busy_timeout_ms = busy_timeout_ms
        self._fault_injector = fault_injector
        self._verify_database_parent_security()
        self._secure_database_file()
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def _verify_database_parent_security(self) -> None:
        try:
            parent_status = self._database_path.parent.stat()
        except OSError:
            raise EventStoreError("event store operation failed") from None
        security = (
            stat.S_ISDIR(parent_status.st_mode),
            parent_status.st_uid == os.geteuid(),
            bool(parent_status.st_mode & (stat.S_IWGRP | stat.S_IWOTH)),
        )
        if security != (True, True, False):
            raise EventStoreError("event store operation failed")

    def _secure_database_file(self) -> None:
        try:
            descriptor = os.open(
                self._database_path,
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        except OSError:
            raise EventStoreError("event store operation failed") from None

    def _verify_database_file_security(self) -> None:
        try:
            database_status = self._database_path.stat()
        except OSError:
            raise EventStoreError("event store operation failed") from None
        security = (
            stat.S_ISREG(database_status.st_mode),
            database_status.st_uid == os.geteuid(),
            stat.S_IMODE(database_status.st_mode),
        )
        if security != (True, True, 0o600):
            raise EventStoreError("event store operation failed")

    def _secure_database_sidecars(self) -> None:
        for suffix in ("-wal", "-shm"):
            sidecar_path = Path(f"{self._database_path}{suffix}")
            try:
                descriptor = os.open(sidecar_path, os.O_RDWR | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue
            except OSError:
                raise EventStoreError("event store operation failed") from None
            try:
                sidecar_status = os.fstat(descriptor)
                identity = (
                    stat.S_ISREG(sidecar_status.st_mode),
                    sidecar_status.st_uid == os.geteuid(),
                )
                if identity != (True, True):
                    raise EventStoreError("event store operation failed")
                os.fchmod(descriptor, 0o600)
            except OSError:
                raise EventStoreError("event store operation failed") from None
            finally:
                os.close(descriptor)

    def _open(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self._database_path,
                timeout=self._busy_timeout_ms / 1_000,
                isolation_level=None,
            )
        except sqlite3.Error as error:
            raise self._translated_error(error) from None
        try:
            self._verify_database_file_security()
            self._secure_database_sidecars()
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except EventStoreError:
            connection.close()
            raise
        except sqlite3.Error as error:
            connection.close()
            raise self._translated_error(error) from None

    def _initialize(self) -> None:
        connection = self._open()
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, SCHEMA_VERSION}:
                raise UnsupportedSchemaVersionError(version)
            self._enable_wal(connection)
            self._secure_database_sidecars()
            if self._fault_injector is not None:
                self._fault_injector(FaultPoint.BEFORE_INITIALIZATION_LOCK)
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in {0, SCHEMA_VERSION}:
                    raise UnsupportedSchemaVersionError(version)
                if version == 0:
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_metadata(singleton, version) VALUES (1, ?)",
                        (SCHEMA_VERSION,),
                    )
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    return
                self._verify_current_schema(connection)
        except sqlite3.Error as error:
            raise self._translated_error(error) from None
        finally:
            connection.close()

    def _enable_wal(self, connection: sqlite3.Connection) -> None:
        deadline = time.monotonic() + self._busy_timeout_ms / 1_000
        while True:
            try:
                journal_mode = connection.execute(
                    "PRAGMA journal_mode = WAL"
                ).fetchone()
                if journal_mode != ("wal",):
                    raise EventStoreError("event store operation failed")
                return
            except sqlite3.Error as error:
                error_code = getattr(error, "sqlite_errorcode", -1) & 0xFF
                if error_code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(WAL_RETRY_INTERVAL_SECONDS, remaining))

    @staticmethod
    def _verify_current_schema(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(version)
        metadata_version = connection.execute(
            "SELECT COALESCE(MAX(version), -1) FROM schema_metadata"
        ).fetchone()[0]
        if type(metadata_version) is not int:
            raise EventStoreError("event store operation failed")
        if metadata_version != SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(metadata_version)

    def diagnostics(self) -> StoreDiagnostics:
        connection = self._open()
        try:
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
            return StoreDiagnostics(
                schema_version=schema_version,
                journal_mode=journal_mode,
                foreign_keys_enabled=foreign_keys == 1,
                busy_timeout_ms=busy_timeout,
            )
        except sqlite3.Error as error:
            raise self._translated_error(error) from None
        finally:
            connection.close()

    def append_private_input(
        self,
        context: TrustedPrincipalContext,
        event: PrivateInputEvent,
    ) -> AppendResult:
        self._validate_context(context)
        timestamp = self._validate_private_input(context, event)
        connection = self._open()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                self._verify_current_schema(connection)
                existing_event = connection.execute(
                    """
                    SELECT
                        audience_principal_id,
                        authenticated_source_ref,
                        source_message_id
                    FROM event_envelopes
                    WHERE event_id = ?
                    """,
                    (event.event_id,),
                ).fetchone()
                source_identity = (
                    context.principal_id.value,
                    context.authenticated_source.value,
                    event.source_message_id,
                )
                if existing_event is not None:
                    if existing_event == source_identity:
                        return AppendResult(AppendStatus.DUPLICATE)
                    raise EventIdentityConflictError("event identity conflict")
                replay = connection.execute(
                    """
                    SELECT 1
                    FROM event_envelopes
                    WHERE audience_principal_id = ?
                      AND authenticated_source_ref = ?
                      AND source_message_id = ?
                    LIMIT 1
                    """,
                    source_identity,
                ).fetchone()
                if replay is not None:
                    return AppendResult(AppendStatus.DUPLICATE)

                connection.execute(
                    """
                    INSERT INTO private_payloads(
                        payload_id,
                        content,
                        audience_type,
                        audience_principal_id,
                        provenance_type,
                        verification_status,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.payload_id,
                        event.content,
                        event.audience.kind.value,
                        event.audience.principal_id.value,
                        event.provenance.source_type.value,
                        event.provenance.verification.value,
                        timestamp,
                    ),
                )
                if self._fault_injector is not None:
                    self._fault_injector(FaultPoint.AFTER_PAYLOAD_INSERT)
                connection.execute(
                    """
                    INSERT INTO event_envelopes(
                        event_id,
                        occurred_at,
                        event_type,
                        channel,
                        correlation_id,
                        authenticated_source_ref,
                        source_message_id,
                        audience_type,
                        audience_principal_id,
                        provenance_type,
                        verification_status,
                        payload_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        timestamp,
                        event.event_type.value,
                        event.channel.value,
                        event.correlation_id,
                        context.authenticated_source.value,
                        event.source_message_id,
                        event.audience.kind.value,
                        event.audience.principal_id.value,
                        event.provenance.source_type.value,
                        event.provenance.verification.value,
                        event.payload_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO protected_event_taints(
                        event_id,
                        source_event_id,
                        source_principal_id,
                        source_provenance_type
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.event_id,
                        context.principal_id.value,
                        event.provenance.source_type.value,
                    ),
                )
                return AppendResult(AppendStatus.CREATED)
        except sqlite3.Error as error:
            raise self._translated_error(error) from None
        finally:
            connection.close()

    def retrieve_private_events(
        self, context: TrustedPrincipalContext
    ) -> tuple[PrivateConversationEvent, ...]:
        self._validate_context(context)
        connection = self._open()
        try:
            with connection:
                connection.execute("BEGIN")
                self._verify_current_schema(connection)
                rows = connection.execute(
                    """
                    SELECT
                        event.event_id,
                        event.occurred_at,
                        event.event_type,
                        event.channel,
                        event.correlation_id,
                        event.audience_type,
                        event.audience_principal_id,
                        event.provenance_type,
                        event.verification_status,
                        payload.provenance_type,
                        payload.verification_status,
                        taint.source_event_id,
                        taint.source_principal_id,
                        taint.source_provenance_type,
                        CASE
                            WHEN typeof(payload.content) = 'text'
                             AND length(CAST(payload.content AS BLOB))
                                 BETWEEN 1 AND ?
                            THEN payload.content
                        END AS content
                    FROM event_envelopes AS event
                    JOIN private_payloads AS payload
                      ON payload.payload_id = event.payload_id
                    LEFT JOIN protected_event_taints AS taint
                      ON taint.event_id = event.event_id
                    WHERE event.audience_type = 'private_participant'
                      AND event.audience_principal_id = ?
                      AND payload.audience_type = event.audience_type
                      AND payload.audience_principal_id = event.audience_principal_id
                      AND payload.retention_status = 'active'
                    ORDER BY event.occurred_at DESC, event.event_id DESC
                    LIMIT ?
                    """,
                    (
                        MAX_PRIVATE_PAYLOAD_BYTES,
                        context.principal_id.value,
                        MAX_PRIVATE_EVENTS_PER_RETRIEVAL,
                    ),
                ).fetchall()
                rows.reverse()
            return tuple(self._conversation_event(row) for row in rows)
        except (OverflowError, TypeError, ValueError):
            raise EventStoreError("event store operation failed") from None
        except sqlite3.Error as error:
            raise self._translated_error(error) from None
        finally:
            connection.close()

    @staticmethod
    def _conversation_event(row: tuple[object, ...]) -> PrivateConversationEvent:
        if any(type(value) is not str for value in row):
            raise EventStoreError("event store operation failed")
        EventStore._validate_payload_content(row[14])
        occurred_at = datetime.fromisoformat(row[1])
        if EventStore._serialize_time(occurred_at) != row[1]:
            raise ValueError("noncanonical stored timestamp")
        provenance = Provenance(
            source_type=ProvenanceType(row[7]),
            verification=VerificationStatus(row[8]),
        )
        expected_provenance = Provenance(
            source_type=ProvenanceType.PARTICIPANT_ASSERTION,
            verification=VerificationStatus.UNVERIFIED,
        )
        payload_provenance = Provenance(
            source_type=ProvenanceType(row[9]),
            verification=VerificationStatus(row[10]),
        )
        if provenance != expected_provenance or payload_provenance != provenance:
            raise ValueError("invalid stored provenance")
        taint_identity = (row[11], row[12], row[13])
        expected_taint_identity = (
            row[0],
            row[6],
            provenance.source_type.value,
        )
        if taint_identity != expected_taint_identity:
            raise ValueError("invalid protected taint")
        return PrivateConversationEvent(
            event_id=row[0],
            occurred_at=occurred_at,
            event_type=EventType(row[2]),
            channel=EventChannel(row[3]),
            correlation_id=row[4],
            audience=AudienceType(row[5]),
            provenance=provenance,
            content=row[14],
        )

    @staticmethod
    def _validate_context(context: TrustedPrincipalContext) -> None:
        if not isinstance(context, TrustedPrincipalContext):
            raise EventValidationError("trusted principal context is required")
        if not isinstance(context.principal_id, ParticipantId):
            raise EventValidationError("invalid principal identity")
        if not isinstance(context.authenticated_source, AuthenticatedSourceReference):
            raise EventValidationError("invalid authenticated source")
        if type(context.is_owner) is not bool:
            raise EventValidationError("invalid owner marker")
        EventStore._require_token("principal identity", context.principal_id.value)
        EventStore._require_token(
            "authenticated source", context.authenticated_source.value
        )

    @staticmethod
    def _validate_private_input(
        context: TrustedPrincipalContext, event: PrivateInputEvent
    ) -> str:
        if not isinstance(event, PrivateInputEvent):
            raise EventValidationError("private input event is required")
        EventStore._require_token("event id", event.event_id)
        EventStore._require_token("payload id", event.payload_id)
        EventStore._require_token("source message id", event.source_message_id)
        EventStore._require_token("correlation id", event.correlation_id)
        if not isinstance(event.occurred_at, datetime):
            raise EventValidationError("invalid event timestamp")
        if event.occurred_at.tzinfo is None:
            raise EventValidationError("event timestamp must include a timezone")
        try:
            offset = event.occurred_at.utcoffset()
        except (OverflowError, TypeError, ValueError):
            raise EventValidationError(
                "event timestamp cannot be represented in UTC"
            ) from None
        if offset is None:
            raise EventValidationError("event timestamp must include a timezone")
        if event.event_type is not EventType.PARTICIPANT_INPUT:
            raise EventValidationError("unsupported event type")
        if event.channel is not EventChannel.PRIVATE_CHAT:
            raise EventValidationError("unsupported event channel")
        if not isinstance(event.audience, EventAudience):
            raise EventValidationError("invalid event audience")
        if event.audience.kind is not AudienceType.PRIVATE_PARTICIPANT:
            raise EventValidationError("unsupported audience type")
        if not isinstance(event.audience.principal_id, ParticipantId):
            raise EventValidationError("invalid audience principal")
        if event.audience.principal_id != context.principal_id:
            raise EventValidationError("audience principal does not match context")
        if not isinstance(event.provenance, Provenance):
            raise EventValidationError("invalid event provenance")
        if not isinstance(event.provenance.source_type, ProvenanceType):
            raise EventValidationError("unsupported provenance type")
        if not isinstance(event.provenance.verification, VerificationStatus):
            raise EventValidationError("unsupported verification status")
        expected = Provenance(
            ProvenanceType.PARTICIPANT_ASSERTION,
            VerificationStatus.UNVERIFIED,
        )
        if event.provenance != expected:
            raise EventValidationError(
                "private input must be an unverified participant assertion"
            )
        EventStore._validate_payload_content(event.content)
        try:
            return EventStore._serialize_time(event.occurred_at)
        except (OverflowError, TypeError, ValueError):
            raise EventValidationError(
                "event timestamp cannot be represented in UTC"
            ) from None

    @staticmethod
    def _validate_payload_content(content: object) -> None:
        if not isinstance(content, str) or not content:
            raise EventValidationError("invalid payload content")
        if len(content) > MAX_PRIVATE_PAYLOAD_BYTES:
            raise EventValidationError("invalid payload content")
        try:
            encoded_content = content.encode("utf-8")
        except UnicodeEncodeError:
            raise EventValidationError("invalid payload content") from None
        if len(encoded_content) > MAX_PRIVATE_PAYLOAD_BYTES:
            raise EventValidationError("invalid payload content")

    @staticmethod
    def _require_token(field: str, value: object) -> None:
        if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
            raise EventValidationError(f"invalid {field}")

    @staticmethod
    def _serialize_time(value: datetime) -> str:
        return (
            value.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _translated_error(error: sqlite3.Error) -> EventStoreError:
        error_code = getattr(error, "sqlite_errorcode", -1) & 0xFF
        if error_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return EventStoreBusyError("event store is busy")
        return EventStoreError("event store operation failed")
