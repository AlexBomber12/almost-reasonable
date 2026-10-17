"""Application-owned SQLite storage for participant-private input events."""

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .shared_schema import SCHEMA_V6 as _SCHEMA_V6

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


SCHEMA_VERSION = 8
DEFAULT_BUSY_TIMEOUT_MS = 5_000
MAX_BUSY_TIMEOUT_MS = 2_147_483_647
MAX_PRIVATE_PAYLOAD_BYTES = 64 * 1024
MAX_STATE_TEXT_BYTES = 4096
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
    BEFORE_V2_MIGRATION_COMMIT = "before_v2_migration_commit"
    BEFORE_V3_MIGRATION_COMMIT = "before_v3_migration_commit"
    BEFORE_V4_MIGRATION_COMMIT = "before_v4_migration_commit"
    BEFORE_V5_MIGRATION_COMMIT = "before_v5_migration_commit"
    BEFORE_V6_MIGRATION_COMMIT = "before_v6_migration_commit"
    BEFORE_V7_MIGRATION_COMMIT = "before_v7_migration_commit"
    BEFORE_V8_MIGRATION_COMMIT = "before_v8_migration_commit"
    AFTER_REPLY_PAYLOAD_INSERT = "after_reply_payload_insert"
    AFTER_REPLY_COMMIT = "after_reply_commit"
    AFTER_PAYLOAD_INSERT = "after_payload_insert"
    AFTER_INPUT_ENVELOPE_INSERT = "after_input_envelope_insert"


@dataclass(frozen=True, slots=True)
class AppendResult:
    status: AppendStatus


@dataclass(frozen=True, slots=True)
class StoreDiagnostics:
    schema_version: int
    journal_mode: str
    foreign_keys_enabled: bool
    busy_timeout_ms: int


_SCHEMA_V1 = (
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


_SCHEMA_V2 = (
    """
    CREATE TABLE state_items (
        item_key INTEGER PRIMARY KEY,
        item_id TEXT NOT NULL,
        state_type TEXT NOT NULL
            CHECK (state_type IN ('interest', 'belief', 'goal')),
        scope TEXT NOT NULL
            CHECK (scope IN ('private_candidate', 'shared_agent')),
        audience_type TEXT NOT NULL
            CHECK (audience_type IN ('private_participant', 'shared_agent')),
        audience_principal_id TEXT NOT NULL,
        current_revision INTEGER NOT NULL
            CHECK (typeof(current_revision) = 'integer' AND current_revision >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (
            (scope = 'private_candidate'
             AND audience_type = 'private_participant'
             AND length(audience_principal_id) > 0)
            OR
            (scope = 'shared_agent'
             AND audience_type = 'shared_agent'
             AND audience_principal_id = '')
        ),
        UNIQUE (scope, audience_principal_id, item_id)
    )
    """,
    """
    CREATE TABLE state_operations (
        operation_key INTEGER PRIMARY KEY,
        operation_id TEXT NOT NULL,
        scope TEXT NOT NULL CHECK (scope = 'private_candidate'),
        audience_principal_id TEXT NOT NULL,
        item_key INTEGER NOT NULL
            REFERENCES state_items(item_key) ON DELETE CASCADE,
        revision_number INTEGER NOT NULL
            CHECK (typeof(revision_number) = 'integer' AND revision_number >= 1),
        UNIQUE (scope, audience_principal_id, operation_id)
    )
    """,
    """
    CREATE TABLE state_revisions (
        revision_key INTEGER PRIMARY KEY,
        item_key INTEGER NOT NULL
            REFERENCES state_items(item_key) ON DELETE CASCADE,
        operation_key INTEGER NOT NULL UNIQUE
            REFERENCES state_operations(operation_key) ON DELETE CASCADE,
        revision_number INTEGER NOT NULL
            CHECK (typeof(revision_number) = 'integer' AND revision_number >= 1),
        previous_revision_number INTEGER,
        previous_status TEXT,
        new_status TEXT NOT NULL CHECK (
            new_status IN ('active', 'suspended', 'completed', 'abandoned')
        ),
        previous_confidence REAL,
        new_confidence REAL,
        revision_kind TEXT NOT NULL CHECK (
            revision_kind IN (
                'evidence_backed',
                'tentative_interpretation',
                'operational_transition'
            )
        ),
        decision_origin TEXT NOT NULL
            CHECK (decision_origin IN ('deterministic', 'model')),
        decided_at TEXT NOT NULL,
        core_self_version TEXT NOT NULL,
        model_identifier TEXT,
        prompt_version TEXT,
        CHECK (
            previous_status IS NULL
            OR previous_status IN ('active', 'suspended', 'completed', 'abandoned')
        ),
        CHECK (
            previous_confidence IS NULL
            OR (typeof(previous_confidence) = 'real'
                AND previous_confidence BETWEEN 0.0 AND 1.0)
        ),
        CHECK (
            new_confidence IS NULL
            OR (typeof(new_confidence) = 'real'
                AND new_confidence BETWEEN 0.0 AND 1.0)
        ),
        CHECK (
            (decision_origin = 'deterministic'
             AND model_identifier IS NULL
             AND prompt_version IS NULL)
            OR
            (decision_origin = 'model'
             AND model_identifier IS NOT NULL
             AND prompt_version IS NOT NULL)
        ),
        UNIQUE (item_key, revision_number),
        CHECK (
            (revision_number = 1 AND previous_revision_number IS NULL)
            OR previous_revision_number = revision_number - 1
        )
    )
    """,
    f"""
    CREATE TABLE state_revision_payloads (
        revision_key INTEGER PRIMARY KEY
            REFERENCES state_revisions(revision_key) ON DELETE CASCADE,
        state_type TEXT NOT NULL
            CHECK (state_type IN ('interest', 'belief', 'goal')),
        revision_reason TEXT NOT NULL CHECK (
            typeof(revision_reason) = 'text'
            AND length(CAST(revision_reason AS BLOB)) BETWEEN 1
                AND {MAX_STATE_TEXT_BYTES}
        ),
        review_at TEXT NOT NULL,
        topic TEXT,
        interest_strength INTEGER,
        interest_status TEXT,
        interest_reason TEXT,
        proposition TEXT,
        belief_stance TEXT,
        belief_confidence REAL,
        belief_status TEXT,
        belief_verification TEXT,
        goal_description TEXT,
        goal_rationale TEXT,
        goal_priority INTEGER,
        goal_status TEXT,
        goal_review_condition TEXT,
        retention_status TEXT NOT NULL DEFAULT 'active'
            CHECK (retention_status IN ('active', 'deleted')),
        CHECK (topic IS NULL OR (
            typeof(topic) = 'text'
            AND length(CAST(topic AS BLOB)) BETWEEN 1 AND {MAX_STATE_TEXT_BYTES}
        )),
        CHECK (interest_reason IS NULL OR (
            typeof(interest_reason) = 'text'
            AND length(CAST(interest_reason AS BLOB)) BETWEEN 1
                AND {MAX_STATE_TEXT_BYTES}
        )),
        CHECK (proposition IS NULL OR (
            typeof(proposition) = 'text'
            AND length(CAST(proposition AS BLOB)) BETWEEN 1
                AND {MAX_STATE_TEXT_BYTES}
        )),
        CHECK (goal_description IS NULL OR (
            typeof(goal_description) = 'text'
            AND length(CAST(goal_description AS BLOB)) BETWEEN 1
                AND {MAX_STATE_TEXT_BYTES}
        )),
        CHECK (goal_rationale IS NULL OR (
            typeof(goal_rationale) = 'text'
            AND length(CAST(goal_rationale AS BLOB)) BETWEEN 1
                AND {MAX_STATE_TEXT_BYTES}
        )),
        CHECK (goal_review_condition IS NULL OR (
            typeof(goal_review_condition) = 'text'
            AND length(CAST(goal_review_condition AS BLOB)) BETWEEN 1
                AND {MAX_STATE_TEXT_BYTES}
        )),
        CHECK (interest_strength IS NULL OR (
            typeof(interest_strength) = 'integer'
            AND interest_strength BETWEEN 0 AND 100
        )),
        CHECK (belief_confidence IS NULL OR (
            typeof(belief_confidence) = 'real'
            AND belief_confidence BETWEEN 0.0 AND 1.0
        )),
        CHECK (goal_priority IS NULL OR (
            typeof(goal_priority) = 'integer'
            AND goal_priority BETWEEN 0 AND 100
        )),
        CHECK (
            (state_type = 'interest'
             AND topic IS NOT NULL
             AND interest_strength IS NOT NULL
             AND interest_status IN ('active', 'suspended', 'abandoned')
             AND interest_reason IS NOT NULL
             AND proposition IS NULL AND belief_stance IS NULL
             AND belief_confidence IS NULL AND belief_status IS NULL
             AND belief_verification IS NULL
             AND goal_description IS NULL AND goal_rationale IS NULL
             AND goal_priority IS NULL AND goal_status IS NULL
             AND goal_review_condition IS NULL)
            OR
            (state_type = 'belief'
             AND topic IS NULL AND interest_strength IS NULL
             AND interest_status IS NULL AND interest_reason IS NULL
             AND proposition IS NOT NULL
             AND belief_stance IN ('supports', 'opposes', 'uncertain')
             AND belief_confidence IS NOT NULL
             AND belief_status IN ('active', 'suspended', 'abandoned')
             AND belief_verification = 'unverified'
             AND goal_description IS NULL AND goal_rationale IS NULL
             AND goal_priority IS NULL AND goal_status IS NULL
             AND goal_review_condition IS NULL)
            OR
            (state_type = 'goal'
             AND topic IS NULL AND interest_strength IS NULL
             AND interest_status IS NULL AND interest_reason IS NULL
             AND proposition IS NULL AND belief_stance IS NULL
             AND belief_confidence IS NULL AND belief_status IS NULL
             AND belief_verification IS NULL
             AND goal_description IS NOT NULL AND goal_rationale IS NOT NULL
             AND goal_priority IS NOT NULL
             AND goal_status IN ('active', 'suspended', 'completed', 'abandoned')
             AND goal_review_condition IS NOT NULL)
        )
    )
    """,
    f"""
    CREATE TABLE state_payload_alternatives (
        revision_key INTEGER NOT NULL
            REFERENCES state_revision_payloads(revision_key) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        alternative TEXT NOT NULL CHECK (
            typeof(alternative) = 'text'
            AND length(CAST(alternative AS BLOB)) BETWEEN 1
                AND {MAX_STATE_TEXT_BYTES}
        ),
        PRIMARY KEY (revision_key, ordinal)
    )
    """,
    f"""
    CREATE TABLE state_payload_constraints (
        revision_key INTEGER NOT NULL
            REFERENCES state_revision_payloads(revision_key) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        constraint_text TEXT NOT NULL CHECK (
            typeof(constraint_text) = 'text'
            AND length(CAST(constraint_text AS BLOB)) BETWEEN 1
                AND {MAX_STATE_TEXT_BYTES}
        ),
        PRIMARY KEY (revision_key, ordinal)
    )
    """,
    """
    CREATE TABLE protected_state_provenance (
        revision_key INTEGER NOT NULL
            REFERENCES state_revisions(revision_key) ON DELETE CASCADE,
        event_id TEXT NOT NULL
            REFERENCES event_envelopes(event_id),
        evidence_role TEXT NOT NULL
            CHECK (evidence_role IN ('initiating', 'supporting', 'counter')),
        audience_type TEXT NOT NULL CHECK (audience_type = 'private_participant'),
        source_principal_id TEXT NOT NULL,
        source_provenance_type TEXT NOT NULL
            CHECK (source_provenance_type = 'participant_assertion'),
        verification_status TEXT NOT NULL CHECK (verification_status = 'unverified'),
        retention_status TEXT NOT NULL DEFAULT 'active'
            CHECK (retention_status IN ('active', 'invalidated', 'deleted')),
        PRIMARY KEY (revision_key, event_id, evidence_role)
    )
    """,
    """
    CREATE INDEX state_items_private_scope_updated
    ON state_items(scope, audience_principal_id, updated_at, item_id)
    """,
    """
    CREATE INDEX state_revisions_item_revision
    ON state_revisions(item_key, revision_number)
    """,
    """
    CREATE INDEX protected_state_provenance_event
    ON protected_state_provenance(event_id, revision_key)
    """,
)


_SCHEMA_V3 = (
    """
    CREATE TABLE provider_spend_suspensions (
        provider TEXT PRIMARY KEY CHECK (provider = 'openai'),
        confirmed_at TEXT NOT NULL,
        provider_error_code TEXT NOT NULL CHECK (
            provider_error_code IN (
                'project_spend_limit_exceeded',
                'organization_spend_limit_exceeded'
            )
        ),
        provider_config_version TEXT NOT NULL,
        attempt_id TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE provider_attempt_reservations (
        attempt_id TEXT PRIMARY KEY,
        correlation_id TEXT NOT NULL,
        provider TEXT NOT NULL CHECK (provider = 'openai'),
        reserved_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE provider_usage_attempts (
        attempt_id TEXT PRIMARY KEY,
        correlation_id TEXT NOT NULL,
        provider TEXT NOT NULL CHECK (provider = 'openai'),
        model_identifier TEXT NOT NULL,
        provider_config_version TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        core_self_version TEXT NOT NULL,
        provider_response_id TEXT,
        started_at TEXT NOT NULL,
        duration_ms INTEGER NOT NULL CHECK (
            typeof(duration_ms) = 'integer' AND duration_ms >= 0
        ),
        input_tokens INTEGER CHECK (
            input_tokens IS NULL
            OR (typeof(input_tokens) = 'integer' AND input_tokens >= 0)
        ),
        cached_input_tokens INTEGER CHECK (
            cached_input_tokens IS NULL
            OR (typeof(cached_input_tokens) = 'integer'
                AND cached_input_tokens >= 0)
        ),
        output_tokens INTEGER CHECK (
            output_tokens IS NULL
            OR (typeof(output_tokens) = 'integer' AND output_tokens >= 0)
        ),
        total_tokens INTEGER CHECK (
            total_tokens IS NULL
            OR (typeof(total_tokens) = 'integer' AND total_tokens >= 0)
        ),
        outcome TEXT NOT NULL,
        retry_count INTEGER NOT NULL CHECK (
            typeof(retry_count) = 'integer' AND retry_count >= 0
        ),
        estimated_cost TEXT,
        pricing_version TEXT,
        currency TEXT,
        cost_estimate_status TEXT NOT NULL CHECK (
            cost_estimate_status IN (
                'estimated',
                'usage_unavailable',
                'usage_incomplete',
                'pricing_unavailable'
            )
        ),
        provider_error_code TEXT,
        CHECK (
            (cost_estimate_status = 'estimated'
             AND estimated_cost IS NOT NULL
             AND pricing_version IS NOT NULL
             AND currency = 'USD')
            OR
            (cost_estimate_status != 'estimated'
             AND estimated_cost IS NULL
             AND pricing_version IS NULL
             AND currency IS NULL)
        )
    )
    """,
    """
    CREATE INDEX provider_usage_correlation
    ON provider_usage_attempts(correlation_id, started_at, attempt_id)
    """,
)


_SCHEMA_V4 = (
    """
    CREATE TABLE agent_reply_operations (
        operation_id TEXT PRIMARY KEY,
        audience_principal_id TEXT NOT NULL,
        initiating_event_id TEXT NOT NULL REFERENCES event_envelopes(event_id),
        conversation_id TEXT NOT NULL,
        correlation_id TEXT NOT NULL,
        model_identifier TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        core_self_version TEXT NOT NULL,
        source_count INTEGER NOT NULL CHECK (source_count BETWEEN 1 AND 100),
        internal_decision TEXT NOT NULL DEFAULT 'pending'
            CHECK (internal_decision IN ('pending', 'accepted_internal')),
        result_state TEXT NOT NULL DEFAULT 'pending'
            CHECK (result_state IN ('pending', 'persisted')),
        delivery_state TEXT NOT NULL DEFAULT 'held' CHECK (delivery_state = 'held'),
        retention_status TEXT NOT NULL DEFAULT 'active'
            CHECK (retention_status IN ('active', 'invalidated', 'deleted')),
        CHECK ((internal_decision = 'pending' AND result_state = 'pending')
            OR (internal_decision = 'accepted_internal' AND result_state = 'persisted'))
    )
    """,
    f"""
    CREATE TABLE agent_reply_payloads (
        operation_id TEXT PRIMARY KEY
            REFERENCES agent_reply_operations(operation_id) ON DELETE CASCADE,
        content TEXT NOT NULL CHECK (typeof(content) = 'text'
            AND length(CAST(content AS BLOB)) BETWEEN 1 AND {MAX_PRIVATE_PAYLOAD_BYTES}),
        retention_status TEXT NOT NULL DEFAULT 'active'
            CHECK (retention_status IN ('active', 'deleted'))
    )
    """,
    """
    CREATE TABLE agent_reply_sources (
        operation_id TEXT NOT NULL
            REFERENCES agent_reply_operations(operation_id) ON DELETE CASCADE,
        event_id TEXT NOT NULL REFERENCES event_envelopes(event_id),
        source_principal_id TEXT NOT NULL,
        retention_status TEXT NOT NULL DEFAULT 'active'
            CHECK (retention_status IN ('active', 'invalidated', 'deleted')),
        PRIMARY KEY (operation_id, event_id)
    )
    """,
    """
    CREATE INDEX agent_reply_sources_event ON agent_reply_sources(event_id, operation_id)
    """,
)


_SCHEMA_V5 = (
    """
    CREATE TABLE agent_reply_resolutions (
        operation_id TEXT PRIMARY KEY REFERENCES agent_reply_operations(operation_id),
        outcome TEXT NOT NULL CHECK (outcome IN
            ('completed', 'no_action', 'failed', 'invalid', 'unsupported', 'skipped')),
        release_id TEXT UNIQUE,
        CHECK ((outcome = 'completed' AND release_id IS NOT NULL)
            OR (outcome <> 'completed' AND release_id IS NULL))
    )
    """,
    """
    CREATE TABLE event_input_sequences (
        sequence INTEGER PRIMARY KEY CHECK (sequence > 0),
        event_id TEXT NOT NULL UNIQUE REFERENCES event_envelopes(event_id) ON DELETE CASCADE
    )
    """,
    """
    INSERT INTO event_input_sequences(sequence, event_id)
    SELECT ROW_NUMBER() OVER (ORDER BY rowid), event_id FROM event_envelopes
    """,
    """
    CREATE TRIGGER event_input_sequence_append AFTER INSERT ON event_envelopes
    BEGIN
        INSERT INTO event_input_sequences(sequence, event_id)
        VALUES ((SELECT COALESCE(MAX(sequence), 0) + 1 FROM event_input_sequences), NEW.event_id);
    END
    """,
)


_SCHEMA_V7 = (
    """ALTER TABLE agent_reply_operations ADD COLUMN generic_count INTEGER NOT NULL
        DEFAULT 0 CHECK(generic_count BETWEEN 0 AND 16)""",
    """CREATE TABLE generic_memory_items (
        item_id TEXT NOT NULL,
        audience_principal_id TEXT NOT NULL,
        current_revision INTEGER NOT NULL CHECK(current_revision BETWEEN 1 AND 32),
        PRIMARY KEY(audience_principal_id,item_id)
    )""",
    """CREATE TABLE generic_memory_operations (
        operation_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        audience_principal_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision >= 1),
        expected_revision INTEGER,
        source_count INTEGER NOT NULL CHECK(source_count BETWEEN 1 AND 100),
        parent_count INTEGER NOT NULL CHECK(parent_count BETWEEN 0 AND 16),
        context_count INTEGER NOT NULL CHECK(context_count BETWEEN 0 AND 16),
        status TEXT NOT NULL CHECK(status IN ('active','invalidated')),
        decided_at TEXT NOT NULL,
        model_identifier TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        core_self_version TEXT NOT NULL,
        graph_digest TEXT NOT NULL CHECK(length(graph_digest)=64),
        PRIMARY KEY(audience_principal_id,operation_id),
        UNIQUE(audience_principal_id,item_id,revision),
        FOREIGN KEY(audience_principal_id,item_id)
            REFERENCES generic_memory_items(audience_principal_id,item_id)
    )""",
    """CREATE TABLE generic_memory_payloads (
        operation_id TEXT NOT NULL,
        audience_principal_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('unresolved_question','candidate_useful_memory','contradiction_counterevidence')),
        statement TEXT NOT NULL CHECK(length(CAST(statement AS BLOB)) BETWEEN 1 AND 4096),
        confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
        verification TEXT NOT NULL CHECK(verification='unverified'),
        reason TEXT NOT NULL CHECK(length(CAST(reason AS BLOB)) BETWEEN 1 AND 4096),
        review_at TEXT NOT NULL,
        review_state TEXT NOT NULL CHECK(review_state='pending_review'),
        binding TEXT NOT NULL,
        PRIMARY KEY(audience_principal_id,operation_id),
        FOREIGN KEY(audience_principal_id,operation_id)
            REFERENCES generic_memory_operations(audience_principal_id,operation_id)
    )""",
    """CREATE TABLE generic_memory_sources (
        operation_id TEXT NOT NULL,
        event_id TEXT NOT NULL REFERENCES event_envelopes(event_id),
        role TEXT NOT NULL CHECK(role IN ('initiating','supporting','counterevidence','contextual')),
        source_principal_id TEXT NOT NULL,
        retention_status TEXT NOT NULL CHECK(retention_status IN ('active','invalidated')),
        PRIMARY KEY(source_principal_id,operation_id,event_id,role),
        FOREIGN KEY(source_principal_id,operation_id)
            REFERENCES generic_memory_operations(audience_principal_id,operation_id)
    )""",
    """CREATE TABLE generic_memory_parents (
        operation_id TEXT NOT NULL,
        parent_operation_id TEXT NOT NULL,
        audience_principal_id TEXT NOT NULL,
        PRIMARY KEY(audience_principal_id,operation_id,parent_operation_id),
        FOREIGN KEY(audience_principal_id,operation_id)
            REFERENCES generic_memory_operations(audience_principal_id,operation_id),
        FOREIGN KEY(audience_principal_id,parent_operation_id)
            REFERENCES generic_memory_operations(audience_principal_id,operation_id),
        CHECK(operation_id <> parent_operation_id)
    )""",
    """CREATE TABLE generic_memory_context (
        operation_id TEXT NOT NULL,
        context_operation_id TEXT NOT NULL,
        audience_principal_id TEXT NOT NULL,
        PRIMARY KEY(audience_principal_id,operation_id,context_operation_id),
        FOREIGN KEY(audience_principal_id,operation_id)
            REFERENCES generic_memory_operations(audience_principal_id,operation_id),
        FOREIGN KEY(audience_principal_id,context_operation_id)
            REFERENCES generic_memory_operations(audience_principal_id,operation_id),
        CHECK(operation_id <> context_operation_id)
    )""",
    """CREATE TABLE protected_reply_generic (
        operation_id TEXT NOT NULL REFERENCES agent_reply_operations(operation_id),
        generic_operation_id TEXT NOT NULL,
        generic_principal_id TEXT NOT NULL,
        PRIMARY KEY(operation_id,generic_principal_id,generic_operation_id),
        FOREIGN KEY(generic_principal_id,generic_operation_id)
            REFERENCES generic_memory_operations(audience_principal_id,operation_id)
    )""",
    "CREATE INDEX generic_memory_sources_event ON generic_memory_sources(event_id)",
    "CREATE INDEX generic_memory_parents_parent ON generic_memory_parents(parent_operation_id)",
    "CREATE INDEX generic_memory_context_source ON generic_memory_context(context_operation_id)",
    "CREATE INDEX generic_reply_count ON agent_reply_operations(operation_id) WHERE generic_count>0",
)


_SCHEMA_V8 = (
    """CREATE TABLE agent_reply_resolutions_v8 (
        operation_id TEXT PRIMARY KEY REFERENCES agent_reply_operations(operation_id),
        outcome TEXT NOT NULL CHECK (outcome IN
            ('completed', 'no_action', 'failed', 'invalid', 'unsupported', 'skipped',
             'unavailable', 'stopped', 'rejected', 'spend_suspended')),
        release_id TEXT UNIQUE,
        CHECK ((outcome = 'completed' AND release_id IS NOT NULL)
            OR (outcome <> 'completed' AND release_id IS NULL))
    )""",
    """INSERT INTO agent_reply_resolutions_v8
       SELECT operation_id, outcome, release_id FROM agent_reply_resolutions""",
    "DROP TABLE agent_reply_resolutions",
    "ALTER TABLE agent_reply_resolutions_v8 RENAME TO agent_reply_resolutions",
)


def schema_objects(connection: sqlite3.Connection) -> dict[tuple[str, str, str], str]:
    """Exact application objects, including explicit indexes and triggers."""
    return {
        (kind, name, table): " ".join(sql.split())
        for kind, name, table, sql in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        )
    }


def _expected_schemas() -> dict[int, dict[tuple[str, str, str], str]]:
    expected = {}
    with sqlite3.connect(":memory:") as connection:
        for version, statements in enumerate(
            (
                _SCHEMA_V1,
                _SCHEMA_V2,
                _SCHEMA_V3,
                _SCHEMA_V4,
                _SCHEMA_V5,
                _SCHEMA_V6,
                _SCHEMA_V7,
                _SCHEMA_V8,
            ),
            start=1,
        ):
            for statement in statements:
                connection.execute(statement)
            expected[version] = schema_objects(connection)
    return expected


_EXPECTED_SCHEMAS = _expected_schemas()


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
            if version not in {0, 1, 2, 3, 4, 5, 6, 7, SCHEMA_VERSION}:
                raise UnsupportedSchemaVersionError(version)
            self._enable_wal(connection)
            self._secure_database_sidecars()
            if self._fault_injector is not None:
                self._fault_injector(FaultPoint.BEFORE_INITIALIZATION_LOCK)
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in {0, 1, 2, 3, 4, 5, 6, 7, SCHEMA_VERSION}:
                    raise UnsupportedSchemaVersionError(version)
                if (
                    connection.execute("PRAGMA foreign_key_check").fetchone()
                    is not None
                ):
                    raise EventStoreError("event store operation failed")
                if version == 0:
                    if schema_objects(connection):
                        raise EventStoreError("event store operation failed")
                    for statement in (
                        *_SCHEMA_V1,
                        *_SCHEMA_V2,
                        *_SCHEMA_V3,
                        *_SCHEMA_V4,
                        *_SCHEMA_V5,
                        *_SCHEMA_V6,
                        *_SCHEMA_V7,
                        *_SCHEMA_V8,
                    ):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_metadata(singleton, version) VALUES (1, ?)",
                        (SCHEMA_VERSION,),
                    )
                elif version < SCHEMA_VERSION:
                    self._verify_schema_version(connection, version)
                    migrations = (
                        (2, _SCHEMA_V2, FaultPoint.BEFORE_V2_MIGRATION_COMMIT),
                        (3, _SCHEMA_V3, FaultPoint.BEFORE_V3_MIGRATION_COMMIT),
                        (4, _SCHEMA_V4, FaultPoint.BEFORE_V4_MIGRATION_COMMIT),
                        (5, _SCHEMA_V5, FaultPoint.BEFORE_V5_MIGRATION_COMMIT),
                        (6, _SCHEMA_V6, FaultPoint.BEFORE_V6_MIGRATION_COMMIT),
                        (7, _SCHEMA_V7, FaultPoint.BEFORE_V7_MIGRATION_COMMIT),
                        (8, _SCHEMA_V8, FaultPoint.BEFORE_V8_MIGRATION_COMMIT),
                    )
                    for target, statements, fault_point in migrations:
                        if target > version:
                            for statement in statements:
                                connection.execute(statement)
                            if self._fault_injector is not None:
                                self._fault_injector(fault_point)
                    connection.execute(
                        "UPDATE schema_metadata SET version = ? WHERE singleton = 1",
                        (SCHEMA_VERSION,),
                    )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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
        EventStore._verify_schema_version(connection, SCHEMA_VERSION)

    @staticmethod
    def _verify_schema_version(
        connection: sqlite3.Connection, expected_version: int
    ) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != expected_version:
            raise UnsupportedSchemaVersionError(version)
        metadata_version = connection.execute(
            "SELECT COALESCE(MAX(version), -1) FROM schema_metadata"
        ).fetchone()[0]
        if type(metadata_version) is not int:
            raise EventStoreError("event store operation failed")
        if metadata_version != expected_version:
            raise UnsupportedSchemaVersionError(metadata_version)
        if schema_objects(connection) != _EXPECTED_SCHEMAS[expected_version]:
            raise EventStoreError("event store operation failed")

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
                if self._fault_injector is not None:
                    self._fault_injector(FaultPoint.AFTER_INPUT_ENVELOPE_INSERT)
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
        return self._retrieve_private_events(context)

    def retrieve_private_events_in_commit_order(
        self,
        context: TrustedPrincipalContext,
        through_event_id: str | None = None,
        *,
        _connection: sqlite3.Connection | None = None,
    ) -> tuple[PrivateConversationEvent, ...]:
        """Read a scoped prefix of durable input commits, bounded before selection."""
        if through_event_id is not None:
            self._require_token("through event id", through_event_id)
        return self._retrieve_private_events(
            context, through_event_id, commit_order=True, _connection=_connection
        )

    def _retrieve_private_events(
        self,
        context: TrustedPrincipalContext,
        through_event_id: str | None = None,
        *,
        commit_order: bool = False,
        _connection: sqlite3.Connection | None = None,
    ) -> tuple[PrivateConversationEvent, ...]:
        self._validate_context(context)
        ordering = (
            "sequence.sequence DESC"
            if commit_order
            else "event.occurred_at DESC, event.event_id DESC"
        )
        connection = _connection if _connection is not None else self._open()
        try:
            with nullcontext() if _connection is not None else connection:
                if _connection is None:
                    connection.execute("BEGIN")
                self._verify_current_schema(connection)
                rows = connection.execute(
                    f"""
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
                    JOIN event_input_sequences AS sequence ON sequence.event_id=event.event_id
                    JOIN private_payloads AS payload
                      ON payload.payload_id = event.payload_id
                    LEFT JOIN protected_event_taints AS taint
                      ON taint.event_id = event.event_id
                    WHERE event.audience_type = 'private_participant'
                      AND event.audience_principal_id = ?
                      AND payload.audience_type = event.audience_type
                      AND payload.audience_principal_id = event.audience_principal_id
                      AND payload.retention_status = 'active'
                      AND (? IS NULL OR sequence.sequence <= (
                          SELECT boundary.sequence FROM event_input_sequences boundary
                          JOIN event_envelopes initiating USING(event_id)
                          WHERE boundary.event_id=? AND initiating.audience_principal_id=?
                      ))
                    ORDER BY {ordering}
                    LIMIT ?
                    """,
                    (
                        MAX_PRIVATE_PAYLOAD_BYTES,
                        context.principal_id.value,
                        through_event_id,
                        through_event_id,
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
            if _connection is None:
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
