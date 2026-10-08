"""Application-owned SQLite storage for participant-private input events."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
import re
import sqlite3

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
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class EventStoreError(RuntimeError):
    """A sanitized event-store failure that contains no persisted content."""


class EventStoreBusyError(EventStoreError):
    """The configured SQLite lock wait expired."""


class EventValidationError(ValueError):
    """Input at the internal storage boundary failed closed validation."""


class UnsupportedSchemaVersionError(EventStoreError):
    """The database schema is not understood by this application version."""

    def __init__(self, version: int):
        super().__init__(f"unsupported event store schema version: {version}")


class AppendStatus(StrEnum):
    CREATED = "created"
    DUPLICATE = "duplicate"


class FaultPoint(StrEnum):
    """Deterministic fault locations used by local transactional tests."""

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
        version INTEGER NOT NULL CHECK (version >= 1)
    )
    """,
    """
    CREATE TABLE private_payloads (
        payload_id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        audience_type TEXT NOT NULL
            CHECK (audience_type = 'private_participant'),
        audience_principal_id TEXT NOT NULL,
        provenance_type TEXT NOT NULL CHECK (provenance_type IN (
            'direct_observation',
            'participant_assertion',
            'external_claim',
            'model_inference',
            'independently_verified_claim'
        )),
        verification_status TEXT NOT NULL
            CHECK (verification_status IN ('unverified', 'verified')),
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
        provenance_type TEXT NOT NULL CHECK (provenance_type IN (
            'direct_observation',
            'participant_assertion',
            'external_claim',
            'model_inference',
            'independently_verified_claim'
        )),
        verification_status TEXT NOT NULL
            CHECK (verification_status IN ('unverified', 'verified')),
        payload_id TEXT UNIQUE
            REFERENCES private_payloads(payload_id) ON DELETE SET NULL,
        UNIQUE (authenticated_source_ref, source_message_id)
    )
    """,
    """
    CREATE TABLE protected_event_taints (
        event_id TEXT PRIMARY KEY
            REFERENCES event_envelopes(event_id) ON DELETE CASCADE,
        source_event_id TEXT NOT NULL
            REFERENCES event_envelopes(event_id),
        source_principal_id TEXT NOT NULL,
        source_provenance_type TEXT NOT NULL CHECK (source_provenance_type IN (
            'direct_observation',
            'participant_assertion',
            'external_claim',
            'model_inference',
            'independently_verified_claim'
        ))
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
        if not isinstance(busy_timeout_ms, int) or busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self._database_path = Path(database_path)
        self._busy_timeout_ms = busy_timeout_ms
        self._fault_injector = fault_injector
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

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
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except sqlite3.Error as error:
            connection.close()
            raise self._translated_error(error) from None

    def _initialize(self) -> None:
        connection = self._open()
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, SCHEMA_VERSION}:
                raise UnsupportedSchemaVersionError(version)
            connection.execute("PRAGMA journal_mode = WAL")
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if version == 0:
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_metadata(singleton, version) VALUES (1, ?)",
                        (SCHEMA_VERSION,),
                    )
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    return
                metadata_version = connection.execute(
                    "SELECT COALESCE(MAX(version), -1) FROM schema_metadata"
                ).fetchone()[0]
                if metadata_version != SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(metadata_version)
        except sqlite3.Error as error:
            raise self._translated_error(error) from None
        finally:
            connection.close()

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
        self._validate_private_input(context, event)
        connection = self._open()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                duplicate = connection.execute(
                    """
                    SELECT 1
                    FROM event_envelopes
                    WHERE event_id = ?
                       OR (authenticated_source_ref = ? AND source_message_id = ?)
                    LIMIT 1
                    """,
                    (
                        event.event_id,
                        context.authenticated_source.value,
                        event.source_message_id,
                    ),
                ).fetchone()
                if duplicate is not None:
                    return AppendResult(AppendStatus.DUPLICATE)

                timestamp = self._serialize_time(event.occurred_at)
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
            rows = connection.execute(
                """
                SELECT
                    event.event_id,
                    event.occurred_at,
                    event.event_type,
                    event.channel,
                    event.correlation_id,
                    event.audience_type,
                    event.provenance_type,
                    event.verification_status,
                    payload.content
                FROM event_envelopes AS event
                JOIN private_payloads AS payload
                  ON payload.payload_id = event.payload_id
                WHERE event.audience_type = 'private_participant'
                  AND event.audience_principal_id = ?
                  AND payload.audience_type = event.audience_type
                  AND payload.audience_principal_id = event.audience_principal_id
                  AND payload.retention_status = 'active'
                ORDER BY event.occurred_at, event.event_id
                """,
                (context.principal_id.value,),
            ).fetchall()
            return tuple(self._conversation_event(row) for row in rows)
        except sqlite3.Error as error:
            raise self._translated_error(error) from None
        finally:
            connection.close()

    @staticmethod
    def _conversation_event(row: tuple[object, ...]) -> PrivateConversationEvent:
        return PrivateConversationEvent(
            event_id=row[0],
            occurred_at=datetime.fromisoformat(row[1]),
            event_type=EventType(row[2]),
            channel=EventChannel(row[3]),
            correlation_id=row[4],
            audience=AudienceType(row[5]),
            provenance=Provenance(
                source_type=ProvenanceType(row[6]),
                verification=VerificationStatus(row[7]),
            ),
            content=row[8],
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
    ) -> None:
        if not isinstance(event, PrivateInputEvent):
            raise EventValidationError("private input event is required")
        EventStore._require_token("event id", event.event_id)
        EventStore._require_token("payload id", event.payload_id)
        EventStore._require_token("source message id", event.source_message_id)
        EventStore._require_token("correlation id", event.correlation_id)
        if not isinstance(event.occurred_at, datetime):
            raise EventValidationError("invalid event timestamp")
        if event.occurred_at.tzinfo is None or event.occurred_at.utcoffset() is None:
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
        if not isinstance(event.content, str) or not event.content:
            raise EventValidationError("invalid payload content")

    @staticmethod
    def _require_token(field: str, value: object) -> None:
        if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
            raise EventValidationError(f"invalid {field}")

    @staticmethod
    def _serialize_time(value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _translated_error(error: sqlite3.Error) -> EventStoreError:
        error_code = getattr(error, "sqlite_errorcode", -1) & 0xFF
        if error_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return EventStoreBusyError("event store is busy")
        return EventStoreError("event store operation failed")
