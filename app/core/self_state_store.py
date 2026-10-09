"""Participant-scoped persistence for versioned mutable intellectual state."""

import base64
import binascii
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
import re
import sqlite3
import unicodedata
from urllib.parse import unquote_to_bytes

from .core_self import CORE_SELF_VERSION
from .events import (
    AudienceType,
    EventChannel,
    EventType,
    ProvenanceType,
    TrustedPrincipalContext,
    VerificationStatus,
)
from .self_state import (
    BeliefCandidate,
    BeliefCandidateChange,
    BeliefStance,
    BeliefStatus,
    DecisionOrigin,
    EvidenceRole,
    GoalCandidate,
    GoalCandidateChange,
    GoalStatus,
    InterestCandidate,
    InterestCandidateChange,
    InterestStatus,
    PrivateCandidate,
    ProtectedEvidence,
    RevisionContext,
    RevisionKind,
    StateRevision,
    StateScope,
    StateTransitionResult,
    StateType,
    TransitionStatus,
)
from .store import (
    MAX_STATE_TEXT_BYTES,
    EventStore,
    EventStoreError,
    EventValidationError,
)


MAX_STATE_LIST_ITEMS = 20
MAX_PRIVATE_STATE_ITEMS_PER_RETRIEVAL = 100
MAX_REVISION_HISTORY_PER_RETRIEVAL = 100
MAX_EVIDENCE_PER_REVISION = 50

_CORE_SELF_VERSION = re.compile(r"core-self-v[1-9][0-9]{0,8}\Z")
_BASE64_ONLY_TEXT = re.compile(r"[A-Za-z0-9+/_-]{8,}={0,2}\Z")
_HEX_ONLY_TEXT = re.compile(r"(?:[A-Fa-f0-9]{2}){4,}\Z")
_PERCENT_ESCAPE = re.compile(r"%[A-Fa-f0-9]{2}")
_MAX_PERCENT_DECODING_DEPTH = 3
_MIXED_SCRIPT_TOKEN = re.compile(
    r"(?i)(?=[^\W_]*[a-z])(?=[^\W_]*[\u0370-\u058f])[^\W_]+"
)
_DISALLOWED_STATE_TEXT = (
    re.compile(r"\b[a-z][a-z0-9+.-]{1,20}://", re.IGNORECASE),
    re.compile(r"\bwww\.", re.IGNORECASE),
    re.compile(
        r"\b(?:api[ _-]?key|access[ _-]?token|password|secret)\b\s*[:=]",
        re.IGNORECASE,
    ),
    re.compile(r"\bbearer\s+[a-z0-9._~-]{8,}", re.IGNORECASE),
    re.compile(r"\b(?:sk|pk)-[a-z0-9_-]{16,}\b", re.IGNORECASE),
    re.compile(r"\bgh[pousr]_[a-z0-9]{20,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
        r"(?:com|net|org|io|dev|app|ru|рф)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?m)^\s*(?:system|developer|assistant|tool|admin|administrator)\s*:",
        re.IGNORECASE,
    ),
    re.compile(r"</?tool\b|\b(?:tool|function)_call\b|\[/?inst\]", re.IGNORECASE),
    re.compile(
        r"\bignore\s+(?:(?:all\s+)?(?:previous|prior)\s+)?instructions\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:disregard|discard|forget|override|obey|follow)\b.{0,40}"
        r"\b(?:directive|instruction|message|prompt|rule|policy)\w*\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:игнорир\w*|забуд\w*|отброс\w*|переопредел\w*|следуй\w*|"
        r"подчин\w*)\b.{0,60}\b(?:инструкц\w*|директив\w*|сообщен\w*|"
        r"правил\w*|политик\w*)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?m)^\s*(?:(?:goal|objective|instruction|request)\s*:\s*)?"
        r"(?:(?:please|kindly)\s+)?"
        r"(?:deploy|publish|post|send|delete|execute|run|call|invoke|"
        r"разверни|опубликуй|отправь|удали|выполни|запусти|вызови)"
        r"(?=\s|[:,.!?]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:run|execute|call|invoke|access|read)\s+(?:the\s+)?"
        r"(?:shell|terminal|tool|command|filesystem|secret)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:send|transmit|upload|publish|post|export|share|reveal)\b.{0,50}"
        r"\b(?:private|secret|credential|record|data|prompt)\w*\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bbase64\s*:", re.IGNORECASE),
    re.compile(r"\b(?:bypass|without)\s+(?:approval|authorization)\b", re.IGNORECASE),
    re.compile(r"\b[a-z0-9+/]{80,}={0,2}\b", re.IGNORECASE),
)


class SelfStateError(EventStoreError):
    """A sanitized mutable-state storage failure."""


class SelfStateValidationError(ValueError):
    """A state request failed closed validation."""


class SelfStateConflictError(SelfStateError):
    """The expected state revision is no longer current."""


class SelfStateIdentityConflictError(SelfStateError):
    """An operation identity is assigned to a different logical change."""


class SelfStateFaultPoint(StrEnum):
    AFTER_STATE_PAYLOAD_INSERT = "after_state_payload_insert"


class SelfStateStore:
    """Narrow state-transition API over the existing application database."""

    def __init__(
        self,
        event_store: EventStore,
        *,
        fault_injector: Callable[[SelfStateFaultPoint], None] | None = None,
    ):
        if not isinstance(event_store, EventStore):
            raise TypeError("event_store must be an EventStore")
        self._events = event_store
        self._fault_injector = fault_injector

    def create_interest_candidate(
        self, context: TrustedPrincipalContext, change: InterestCandidateChange
    ) -> StateTransitionResult:
        return self._apply(context, StateType.INTEREST, change, creating=True)

    def revise_interest_candidate(
        self, context: TrustedPrincipalContext, change: InterestCandidateChange
    ) -> StateTransitionResult:
        return self._apply(context, StateType.INTEREST, change, creating=False)

    def create_belief_candidate(
        self, context: TrustedPrincipalContext, change: BeliefCandidateChange
    ) -> StateTransitionResult:
        return self._apply(context, StateType.BELIEF, change, creating=True)

    def revise_belief_candidate(
        self, context: TrustedPrincipalContext, change: BeliefCandidateChange
    ) -> StateTransitionResult:
        return self._apply(context, StateType.BELIEF, change, creating=False)

    def create_goal_candidate(
        self, context: TrustedPrincipalContext, change: GoalCandidateChange
    ) -> StateTransitionResult:
        return self._apply(context, StateType.GOAL, change, creating=True)

    def revise_goal_candidate(
        self, context: TrustedPrincipalContext, change: GoalCandidateChange
    ) -> StateTransitionResult:
        return self._apply(context, StateType.GOAL, change, creating=False)

    def retrieve_private_candidates(
        self, context: TrustedPrincipalContext
    ) -> tuple[PrivateCandidate, ...]:
        self._validate_context(context)
        connection = self._events._open()
        try:
            with connection:
                connection.execute("BEGIN")
                self._events._verify_current_schema(connection)
                rows = connection.execute(
                    """
                    SELECT
                        item.item_key,
                        item.item_id,
                        item.state_type,
                        item.scope,
                        item.created_at,
                        item.current_revision,
                        revision.revision_key,
                        payload.review_at,
                        payload.topic,
                        payload.interest_strength,
                        payload.interest_status,
                        payload.interest_reason,
                        payload.proposition,
                        payload.belief_stance,
                        payload.belief_confidence,
                        payload.belief_status,
                        payload.belief_verification,
                        payload.goal_description,
                        payload.goal_rationale,
                        payload.goal_priority,
                        payload.goal_status,
                        payload.goal_review_condition,
                        (SELECT COUNT(*) FROM protected_state_provenance AS source
                         WHERE source.revision_key = revision.revision_key
                           AND source.evidence_role = 'supporting'
                           AND source.retention_status = 'active'),
                        (SELECT COUNT(*) FROM protected_state_provenance AS source
                         WHERE source.revision_key = revision.revision_key
                           AND source.evidence_role = 'counter'
                           AND source.retention_status = 'active')
                    FROM state_items AS item
                    JOIN state_revisions AS revision
                      ON revision.item_key = item.item_key
                     AND revision.revision_number = item.current_revision
                    JOIN state_revision_payloads AS payload
                      ON payload.revision_key = revision.revision_key
                    WHERE item.scope = 'private_candidate'
                      AND item.audience_type = 'private_participant'
                      AND item.audience_principal_id = ?
                      AND payload.retention_status = 'active'
                    ORDER BY revision.revision_key DESC, item.item_id DESC
                    LIMIT ?
                    """,
                    (
                        context.principal_id.value,
                        MAX_PRIVATE_STATE_ITEMS_PER_RETRIEVAL,
                    ),
                ).fetchall()
                rows.reverse()
                return tuple(self._snapshot_from_row(connection, row) for row in rows)
        except (OverflowError, TypeError, ValueError):
            raise EventStoreError("event store operation failed") from None
        except sqlite3.Error as error:
            raise self._events._translated_error(error) from None
        finally:
            connection.close()

    def retrieve_shared_state(self) -> tuple[PrivateCandidate, ...]:
        """Return the separately represented shared state, initially empty.

        This PR intentionally exposes no shared-state write path. The return type
        can be widened when the future abstract-to-shared transition is added.
        """

        connection = self._events._open()
        try:
            with connection:
                connection.execute("BEGIN")
                self._events._verify_current_schema(connection)
                exists = connection.execute(
                    """
                    SELECT 1 FROM state_items
                    WHERE scope = 'shared_agent' AND audience_type = 'shared_agent'
                    LIMIT 1
                    """
                ).fetchone()
                if exists is not None:
                    raise EventStoreError("event store operation failed")
                return ()
        except sqlite3.Error as error:
            raise self._events._translated_error(error) from None
        finally:
            connection.close()

    def retrieve_private_revision_history(
        self,
        context: TrustedPrincipalContext,
        state_type: StateType,
        item_id: str,
    ) -> tuple[StateRevision, ...]:
        self._validate_context(context)
        self._validate_lookup(state_type, item_id)
        connection = self._events._open()
        try:
            with connection:
                connection.execute("BEGIN")
                self._events._verify_current_schema(connection)
                rows = connection.execute(
                    """
                    SELECT
                        item.item_key,
                        item.item_id,
                        item.state_type,
                        item.scope,
                        item.created_at,
                        revision.revision_number,
                        revision.revision_key,
                        payload.review_at,
                        payload.topic,
                        payload.interest_strength,
                        payload.interest_status,
                        payload.interest_reason,
                        payload.proposition,
                        payload.belief_stance,
                        payload.belief_confidence,
                        payload.belief_status,
                        payload.belief_verification,
                        payload.goal_description,
                        payload.goal_rationale,
                        payload.goal_priority,
                        payload.goal_status,
                        payload.goal_review_condition,
                        (SELECT COUNT(*) FROM protected_state_provenance AS source
                         WHERE source.revision_key = revision.revision_key
                           AND source.evidence_role = 'supporting'
                           AND source.retention_status = 'active'),
                        (SELECT COUNT(*) FROM protected_state_provenance AS source
                         WHERE source.revision_key = revision.revision_key
                           AND source.evidence_role = 'counter'
                           AND source.retention_status = 'active'),
                        revision.previous_revision_number,
                        revision.previous_status,
                        revision.new_status,
                        revision.previous_confidence,
                        revision.new_confidence,
                        payload.revision_reason,
                        revision.decided_at,
                        revision.core_self_version,
                        revision.revision_kind,
                        revision.decision_origin,
                        revision.model_identifier,
                        revision.prompt_version
                    FROM state_items AS item
                    JOIN state_revisions AS revision ON revision.item_key = item.item_key
                    JOIN state_revision_payloads AS payload
                      ON payload.revision_key = revision.revision_key
                    WHERE item.scope = 'private_candidate'
                      AND item.audience_type = 'private_participant'
                      AND item.audience_principal_id = ?
                      AND item.state_type = ?
                      AND item.item_id = ?
                      AND payload.retention_status = 'active'
                    ORDER BY revision.revision_number DESC
                    LIMIT ?
                    """,
                    (
                        context.principal_id.value,
                        state_type.value,
                        item_id,
                        MAX_REVISION_HISTORY_PER_RETRIEVAL,
                    ),
                ).fetchall()
                rows.reverse()
                return tuple(self._revision_from_row(connection, row) for row in rows)
        except (OverflowError, TypeError, ValueError):
            raise EventStoreError("event store operation failed") from None
        except sqlite3.Error as error:
            raise self._events._translated_error(error) from None
        finally:
            connection.close()

    def inspect_private_revision_evidence(
        self,
        context: TrustedPrincipalContext,
        state_type: StateType,
        item_id: str,
        revision: int,
    ) -> tuple[ProtectedEvidence, ...]:
        self._validate_context(context)
        self._validate_lookup(state_type, item_id)
        if type(revision) is not int or revision < 1:
            raise SelfStateValidationError("invalid revision")
        connection = self._events._open()
        try:
            with connection:
                connection.execute("BEGIN")
                self._events._verify_current_schema(connection)
                rows = connection.execute(
                    """
                    SELECT
                        source.event_id,
                        source.evidence_role,
                        source.audience_type,
                        source.source_principal_id,
                        source.source_provenance_type,
                        source.verification_status,
                        event.event_type,
                        event.channel,
                        event.audience_type,
                        event.audience_principal_id,
                        event.provenance_type,
                        event.verification_status,
                        payload.audience_type,
                        payload.audience_principal_id,
                        payload.provenance_type,
                        payload.verification_status,
                        payload.retention_status,
                        taint.source_event_id,
                        taint.source_principal_id,
                        taint.source_provenance_type
                    FROM state_items AS item
                    JOIN state_revisions AS revision ON revision.item_key = item.item_key
                    JOIN protected_state_provenance AS source
                      ON source.revision_key = revision.revision_key
                    LEFT JOIN event_envelopes AS event
                      ON event.event_id = source.event_id
                    LEFT JOIN private_payloads AS payload
                      ON payload.payload_id = event.payload_id
                    LEFT JOIN protected_event_taints AS taint
                      ON taint.event_id = event.event_id
                    WHERE item.scope = 'private_candidate'
                      AND item.audience_type = 'private_participant'
                      AND item.audience_principal_id = ?
                      AND item.state_type = ?
                      AND item.item_id = ?
                      AND revision.revision_number = ?
                      AND source.retention_status = 'active'
                    ORDER BY source.evidence_role, source.event_id
                    LIMIT ?
                    """,
                    (
                        context.principal_id.value,
                        state_type.value,
                        item_id,
                        revision,
                        MAX_EVIDENCE_PER_REVISION,
                    ),
                ).fetchall()
            return tuple(
                self._protected_evidence(row, context.principal_id.value)
                for row in rows
            )
        except (TypeError, ValueError):
            raise EventStoreError("event store operation failed") from None
        except sqlite3.Error as error:
            raise self._events._translated_error(error) from None
        finally:
            connection.close()

    def _apply(
        self,
        context: TrustedPrincipalContext,
        state_type: StateType,
        change: InterestCandidateChange | BeliefCandidateChange | GoalCandidateChange,
        *,
        creating: bool,
    ) -> StateTransitionResult:
        self._validate_context(context)
        self._validate_change(state_type, change, creating=creating)
        connection = self._events._open()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                self._events._verify_current_schema(connection)
                duplicate = self._find_operation(
                    connection,
                    context.principal_id.value,
                    change.revision.operation_id,
                )
                if duplicate is not None:
                    if self._logical_change(
                        self._stored_change(connection, duplicate)
                    ) != self._logical_change(change):
                        raise SelfStateIdentityConflictError(
                            "operation identity conflict"
                        )
                    return StateTransitionResult(
                        TransitionStatus.DUPLICATE,
                        self._snapshot_for_revision(connection, duplicate),
                    )

                sources = self._validate_evidence(connection, context, change)
                item = connection.execute(
                    """
                    SELECT
                        item.item_key,
                        item.state_type,
                        item.current_revision,
                        item.created_at,
                        revision.revision_key
                    FROM state_items AS item
                    LEFT JOIN state_revisions AS revision
                      ON revision.item_key = item.item_key
                     AND revision.revision_number = item.current_revision
                    WHERE item.scope = 'private_candidate'
                      AND item.audience_principal_id = ?
                      AND item.item_id = ?
                    """,
                    (context.principal_id.value, change.item_id),
                ).fetchone()
                decided_at = self._serialize_time(change.revision.decided_at)
                if creating:
                    if item is not None:
                        raise SelfStateConflictError("state revision conflict")
                    cursor = connection.execute(
                        """
                        INSERT INTO state_items(
                            item_id, state_type, scope, audience_type,
                            audience_principal_id, current_revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                        """,
                        (
                            change.item_id,
                            state_type.value,
                            StateScope.PRIVATE_CANDIDATE.value,
                            AudienceType.PRIVATE_PARTICIPANT.value,
                            context.principal_id.value,
                            decided_at,
                            decided_at,
                        ),
                    )
                    item_key = cursor.lastrowid
                    previous_revision = None
                    previous_status = None
                    previous_confidence = None
                    revision_number = 1
                else:
                    if item is None or item[1] != state_type.value:
                        raise SelfStateConflictError("state revision conflict")
                    item_key = item[0]
                    expected = change.revision.expected_revision
                    if item[2] != expected:
                        raise SelfStateConflictError("state revision conflict")
                    previous = self._snapshot_for_item_revision(
                        connection, item_key, expected
                    )
                    previous_change = self._stored_change(connection, item[4])
                    if self._semantic_change(previous_change) == self._semantic_change(
                        change
                    ):
                        raise SelfStateValidationError(
                            "state revision has no semantic change"
                        )
                    previous_revision = expected
                    previous_status = previous.status.value
                    previous_confidence = (
                        previous.confidence
                        if isinstance(previous, BeliefCandidate)
                        else None
                    )
                    revision_number = expected + 1

                operation_key = connection.execute(
                    """
                    INSERT INTO state_operations(
                        operation_id, scope, audience_principal_id,
                        item_key, revision_number
                    ) VALUES (?, 'private_candidate', ?, ?, ?)
                    """,
                    (
                        change.revision.operation_id,
                        context.principal_id.value,
                        item_key,
                        revision_number,
                    ),
                ).lastrowid
                revision_key = self._insert_revision(
                    connection,
                    item_key,
                    operation_key,
                    revision_number,
                    previous_revision,
                    previous_status,
                    previous_confidence,
                    change,
                )
                self._insert_payload(connection, revision_key, state_type, change)
                if self._fault_injector is not None:
                    self._fault_injector(SelfStateFaultPoint.AFTER_STATE_PAYLOAD_INSERT)
                self._insert_lists(connection, revision_key, change)
                self._insert_provenance(connection, revision_key, sources)
                updated = connection.execute(
                    """
                    UPDATE state_items
                    SET current_revision = ?, updated_at = ?
                    WHERE item_key = ? AND current_revision = ?
                    """,
                    (
                        revision_number,
                        decided_at,
                        item_key,
                        0 if previous_revision is None else previous_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise SelfStateConflictError("state revision conflict")
                status = (
                    TransitionStatus.CREATED if creating else TransitionStatus.REVISED
                )
                return StateTransitionResult(
                    status, self._snapshot_for_revision(connection, revision_key)
                )
        except SelfStateValidationError:
            raise
        except (OverflowError, TypeError, ValueError):
            raise EventStoreError("event store operation failed") from None
        except sqlite3.Error as error:
            raise self._events._translated_error(error) from None
        finally:
            connection.close()

    @staticmethod
    def _find_operation(
        connection: sqlite3.Connection, principal_id: str, operation_id: str
    ) -> int | None:
        row = connection.execute(
            """
            SELECT revision.revision_key
            FROM state_operations AS operation
            JOIN state_revisions AS revision
              ON revision.operation_key = operation.operation_key
            WHERE operation.scope = 'private_candidate'
              AND operation.audience_principal_id = ?
              AND operation.operation_id = ?
            """,
            (principal_id, operation_id),
        ).fetchone()
        return None if row is None else row[0]

    @staticmethod
    def _logical_change(
        change: InterestCandidateChange | BeliefCandidateChange | GoalCandidateChange,
    ) -> tuple[object, ...]:
        revision = change.revision
        common = (
            type(change),
            change.item_id,
            tuple(sorted(change.supporting_event_ids)),
            change.review_at,
            revision.operation_id,
            revision.expected_revision,
            revision.reason,
            revision.decided_at,
            revision.initiating_event_id,
            revision.kind,
            revision.decision_origin,
            revision.model_identifier,
            revision.prompt_version,
        )
        if isinstance(change, InterestCandidateChange):
            return common + (
                change.topic,
                change.strength,
                change.status,
                change.reason_for_interest,
            )
        if isinstance(change, BeliefCandidateChange):
            return common + (
                change.proposition,
                change.stance,
                change.confidence,
                change.status,
                tuple(sorted(change.conflicting_event_ids)),
                change.alternatives,
                change.verification,
            )
        return common + (
            change.description,
            change.rationale,
            change.priority,
            change.status,
            change.constraints,
            change.review_condition,
        )

    @staticmethod
    def _semantic_change(
        change: InterestCandidateChange | BeliefCandidateChange | GoalCandidateChange,
    ) -> tuple[object, ...]:
        common = (
            type(change),
            change.item_id,
            tuple(sorted(change.supporting_event_ids)),
            change.review_at,
            change.revision.initiating_event_id,
        )
        if isinstance(change, InterestCandidateChange):
            return common + (
                change.topic,
                change.strength,
                change.status,
                change.reason_for_interest,
            )
        if isinstance(change, BeliefCandidateChange):
            return common + (
                change.proposition,
                change.stance,
                change.confidence,
                change.status,
                tuple(sorted(change.conflicting_event_ids)),
                change.alternatives,
                change.verification,
            )
        return common + (
            change.description,
            change.rationale,
            change.priority,
            change.status,
            change.constraints,
            change.review_condition,
        )

    def _stored_change(
        self, connection: sqlite3.Connection, revision_key: int
    ) -> InterestCandidateChange | BeliefCandidateChange | GoalCandidateChange:
        row = connection.execute(
            """
            SELECT
                item.item_id,
                item.state_type,
                revision.previous_revision_number,
                payload.revision_reason,
                revision.decided_at,
                revision.revision_kind,
                revision.decision_origin,
                revision.model_identifier,
                revision.prompt_version,
                payload.review_at,
                payload.topic,
                payload.interest_strength,
                payload.interest_status,
                payload.interest_reason,
                payload.proposition,
                payload.belief_stance,
                payload.belief_confidence,
                payload.belief_status,
                payload.belief_verification,
                payload.goal_description,
                payload.goal_rationale,
                payload.goal_priority,
                payload.goal_status,
                payload.goal_review_condition,
                operation.operation_id
            FROM state_revisions AS revision
            JOIN state_items AS item ON item.item_key = revision.item_key
            JOIN state_operations AS operation
              ON operation.operation_key = revision.operation_key
            JOIN state_revision_payloads AS payload
              ON payload.revision_key = revision.revision_key
            WHERE revision.revision_key = ?
              AND payload.retention_status = 'active'
            """,
            (revision_key,),
        ).fetchone()
        if row is None:
            raise EventStoreError("event store operation failed")
        evidence = connection.execute(
            """
            SELECT evidence_role, event_id
            FROM protected_state_provenance
            WHERE revision_key = ? AND retention_status = 'active'
            ORDER BY event_id
            LIMIT ?
            """,
            (revision_key, MAX_EVIDENCE_PER_REVISION + 1),
        ).fetchall()
        initiating = [item[1] for item in evidence if item[0] == "initiating"]
        if len(initiating) != 1:
            raise EventStoreError("event store operation failed")
        supporting = tuple(item[1] for item in evidence if item[0] == "supporting")
        counter = tuple(item[1] for item in evidence if item[0] == "counter")
        revision = RevisionContext(
            operation_id=row[24],
            expected_revision=row[2],
            reason=row[3],
            decided_at=self._parse_time(row[4]),
            initiating_event_id=initiating[0],
            kind=RevisionKind(row[5]),
            decision_origin=DecisionOrigin(row[6]),
            model_identifier=row[7],
            prompt_version=row[8],
        )
        review_at = self._parse_time(row[9])
        state_type = StateType(row[1])
        if state_type is StateType.INTEREST:
            return InterestCandidateChange(
                row[0],
                row[10],
                row[11],
                InterestStatus(row[12]),
                row[13],
                supporting,
                review_at,
                revision,
            )
        if state_type is StateType.BELIEF:
            return BeliefCandidateChange(
                row[0],
                row[14],
                BeliefStance(row[15]),
                row[16],
                BeliefStatus(row[17]),
                supporting,
                counter,
                self._payload_list(
                    connection,
                    "state_payload_alternatives",
                    "alternative",
                    revision_key,
                ),
                VerificationStatus(row[18]),
                review_at,
                revision,
            )
        return GoalCandidateChange(
            row[0],
            row[19],
            row[20],
            row[21],
            GoalStatus(row[22]),
            self._payload_list(
                connection,
                "state_payload_constraints",
                "constraint_text",
                revision_key,
            ),
            supporting,
            row[23],
            review_at,
            revision,
        )

    @staticmethod
    def _insert_revision(
        connection: sqlite3.Connection,
        item_key: int,
        operation_key: int,
        revision_number: int,
        previous_revision: int | None,
        previous_status: str | None,
        previous_confidence: float | None,
        change: InterestCandidateChange | BeliefCandidateChange | GoalCandidateChange,
    ) -> int:
        if isinstance(change, BeliefCandidateChange):
            new_status = change.status.value
            new_confidence = change.confidence
        else:
            new_status = change.status.value
            new_confidence = None
        return connection.execute(
            """
            INSERT INTO state_revisions(
                item_key, operation_key, revision_number,
                previous_revision_number, previous_status, new_status,
                previous_confidence, new_confidence, revision_kind,
                decision_origin, decided_at, core_self_version,
                model_identifier, prompt_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_key,
                operation_key,
                revision_number,
                previous_revision,
                previous_status,
                new_status,
                previous_confidence,
                new_confidence,
                change.revision.kind.value,
                change.revision.decision_origin.value,
                EventStore._serialize_time(change.revision.decided_at),
                CORE_SELF_VERSION,
                change.revision.model_identifier,
                change.revision.prompt_version,
            ),
        ).lastrowid

    @staticmethod
    def _insert_payload(
        connection: sqlite3.Connection,
        revision_key: int,
        state_type: StateType,
        change: InterestCandidateChange | BeliefCandidateChange | GoalCandidateChange,
    ) -> None:
        values: list[object | None] = [None] * 14
        if isinstance(change, InterestCandidateChange):
            values[0:4] = [
                change.topic,
                change.strength,
                change.status.value,
                change.reason_for_interest,
            ]
        elif isinstance(change, BeliefCandidateChange):
            values[4:9] = [
                change.proposition,
                change.stance.value,
                change.confidence,
                change.status.value,
                change.verification.value,
            ]
        else:
            values[9:14] = [
                change.description,
                change.rationale,
                change.priority,
                change.status.value,
                change.review_condition,
            ]
        connection.execute(
            """
            INSERT INTO state_revision_payloads(
                revision_key, state_type, revision_reason, review_at,
                topic, interest_strength, interest_status, interest_reason,
                proposition, belief_stance, belief_confidence, belief_status,
                belief_verification, goal_description, goal_rationale,
                goal_priority, goal_status, goal_review_condition
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_key,
                state_type.value,
                change.revision.reason,
                EventStore._serialize_time(change.review_at),
                *values,
            ),
        )

    @staticmethod
    def _insert_lists(
        connection: sqlite3.Connection,
        revision_key: int,
        change: InterestCandidateChange | BeliefCandidateChange | GoalCandidateChange,
    ) -> None:
        if isinstance(change, BeliefCandidateChange):
            connection.executemany(
                """
                INSERT INTO state_payload_alternatives(
                    revision_key, ordinal, alternative
                ) VALUES (?, ?, ?)
                """,
                (
                    (revision_key, ordinal, value)
                    for ordinal, value in enumerate(change.alternatives)
                ),
            )
        if isinstance(change, GoalCandidateChange):
            connection.executemany(
                """
                INSERT INTO state_payload_constraints(
                    revision_key, ordinal, constraint_text
                ) VALUES (?, ?, ?)
                """,
                (
                    (revision_key, ordinal, value)
                    for ordinal, value in enumerate(change.constraints)
                ),
            )

    @staticmethod
    def _insert_provenance(
        connection: sqlite3.Connection,
        revision_key: int,
        sources: tuple[tuple[str, EvidenceRole], ...],
    ) -> None:
        connection.executemany(
            """
            INSERT INTO protected_state_provenance(
                revision_key, event_id, evidence_role, audience_type,
                source_principal_id, source_provenance_type,
                verification_status
            )
            SELECT ?, ?, ?, event.audience_type, taint.source_principal_id,
                   taint.source_provenance_type, event.verification_status
            FROM event_envelopes AS event
            JOIN protected_event_taints AS taint ON taint.event_id = event.event_id
            WHERE event.event_id = ?
            """,
            (
                (revision_key, event_id, role.value, event_id)
                for event_id, role in sources
            ),
        )

    def _validate_evidence(
        self,
        connection: sqlite3.Connection,
        context: TrustedPrincipalContext,
        change: InterestCandidateChange | BeliefCandidateChange | GoalCandidateChange,
    ) -> tuple[tuple[str, EvidenceRole], ...]:
        supporting = change.supporting_event_ids
        counter = (
            change.conflicting_event_ids
            if isinstance(change, BeliefCandidateChange)
            else ()
        )
        requested = (
            (change.revision.initiating_event_id, EvidenceRole.INITIATING),
            *((event_id, EvidenceRole.SUPPORTING) for event_id in supporting),
            *((event_id, EvidenceRole.COUNTER) for event_id in counter),
        )
        for event_id, _ in requested:
            row = connection.execute(
                """
                SELECT
                    event.event_type,
                    event.channel,
                    event.audience_type,
                    event.audience_principal_id,
                    event.provenance_type,
                    event.verification_status,
                    payload.audience_type,
                    payload.audience_principal_id,
                    payload.provenance_type,
                    payload.verification_status,
                    payload.retention_status,
                    taint.source_event_id,
                    taint.source_principal_id,
                    taint.source_provenance_type
                FROM event_envelopes AS event
                JOIN private_payloads AS payload ON payload.payload_id = event.payload_id
                LEFT JOIN protected_event_taints AS taint
                  ON taint.event_id = event.event_id
                WHERE event.event_id = ?
                """,
                (event_id,),
            ).fetchone()
            expected = (
                EventType.PARTICIPANT_INPUT.value,
                EventChannel.PRIVATE_CHAT.value,
                AudienceType.PRIVATE_PARTICIPANT.value,
                context.principal_id.value,
                ProvenanceType.PARTICIPANT_ASSERTION.value,
                VerificationStatus.UNVERIFIED.value,
                AudienceType.PRIVATE_PARTICIPANT.value,
                context.principal_id.value,
                ProvenanceType.PARTICIPANT_ASSERTION.value,
                VerificationStatus.UNVERIFIED.value,
                "active",
                event_id,
                context.principal_id.value,
                ProvenanceType.PARTICIPANT_ASSERTION.value,
            )
            if row != expected:
                raise SelfStateValidationError("invalid evidence reference")
        return requested

    def _snapshot_for_revision(
        self, connection: sqlite3.Connection, revision_key: int
    ) -> PrivateCandidate:
        row = connection.execute(
            """
            SELECT
                item.item_key,
                item.item_id,
                item.state_type,
                item.scope,
                item.created_at,
                revision.revision_number,
                revision.revision_key,
                payload.review_at,
                payload.topic,
                payload.interest_strength,
                payload.interest_status,
                payload.interest_reason,
                payload.proposition,
                payload.belief_stance,
                payload.belief_confidence,
                payload.belief_status,
                payload.belief_verification,
                payload.goal_description,
                payload.goal_rationale,
                payload.goal_priority,
                payload.goal_status,
                payload.goal_review_condition,
                (SELECT COUNT(*) FROM protected_state_provenance AS source
                 WHERE source.revision_key = revision.revision_key
                   AND source.evidence_role = 'supporting'
                   AND source.retention_status = 'active'),
                (SELECT COUNT(*) FROM protected_state_provenance AS source
                 WHERE source.revision_key = revision.revision_key
                   AND source.evidence_role = 'counter'
                   AND source.retention_status = 'active')
            FROM state_revisions AS revision
            JOIN state_items AS item ON item.item_key = revision.item_key
            JOIN state_revision_payloads AS payload
              ON payload.revision_key = revision.revision_key
            WHERE revision.revision_key = ?
              AND payload.retention_status = 'active'
            """,
            (revision_key,),
        ).fetchone()
        if row is None:
            raise EventStoreError("event store operation failed")
        return self._snapshot_from_row(connection, row)

    def _snapshot_for_item_revision(
        self, connection: sqlite3.Connection, item_key: int, revision: int
    ) -> PrivateCandidate:
        row = connection.execute(
            """
            SELECT revision_key FROM state_revisions
            WHERE item_key = ? AND revision_number = ?
            """,
            (item_key, revision),
        ).fetchone()
        if row is None:
            raise EventStoreError("event store operation failed")
        return self._snapshot_for_revision(connection, row[0])

    def _snapshot_from_row(
        self, connection: sqlite3.Connection, row: tuple[object, ...]
    ) -> PrivateCandidate:
        created_at = self._parse_time(row[4])
        review_at = self._parse_time(row[7])
        state_type = StateType(row[2])
        scope = StateScope(row[3])
        if scope is not StateScope.PRIVATE_CANDIDATE:
            raise ValueError("unexpected state scope")
        self._require_token("stored item id", row[1])
        common = {
            "item_id": row[1],
            "created_at": created_at,
            "review_at": review_at,
            "revision": row[5],
            "supporting_evidence_count": row[22],
            "scope": scope,
        }
        if state_type is StateType.INTEREST:
            self._validate_text("stored topic", row[8])
            self._validate_priority("stored interest strength", row[9])
            self._validate_text("stored interest reason", row[11])
            return InterestCandidate(
                topic=row[8],
                strength=row[9],
                status=InterestStatus(row[10]),
                reason_for_interest=row[11],
                **common,
            )
        if state_type is StateType.BELIEF:
            self._validate_text("stored proposition", row[12])
            if type(row[14]) is not float or not 0.0 <= row[14] <= 1.0:
                raise ValueError("malformed stored state")
            return BeliefCandidate(
                proposition=row[12],
                stance=BeliefStance(row[13]),
                confidence=row[14],
                status=BeliefStatus(row[15]),
                alternatives=self._payload_list(
                    connection,
                    "state_payload_alternatives",
                    "alternative",
                    row[6],
                ),
                verification=VerificationStatus(row[16]),
                conflicting_evidence_count=row[23],
                **common,
            )
        self._validate_text("stored goal description", row[17])
        self._validate_text("stored goal rationale", row[18])
        self._validate_priority("stored goal priority", row[19])
        self._validate_text("stored review condition", row[21])
        return GoalCandidate(
            description=row[17],
            rationale=row[18],
            priority=row[19],
            status=GoalStatus(row[20]),
            constraints=self._payload_list(
                connection,
                "state_payload_constraints",
                "constraint_text",
                row[6],
            ),
            review_condition=row[21],
            **common,
        )

    def _revision_from_row(
        self, connection: sqlite3.Connection, row: tuple[object, ...]
    ) -> StateRevision:
        state = self._snapshot_from_row(connection, row[:24])
        if row[26] != state.status.value:
            raise ValueError("malformed stored revision")
        if type(row[31]) is not str or _CORE_SELF_VERSION.fullmatch(row[31]) is None:
            raise ValueError("malformed stored revision")
        self._validate_text("stored revision reason", row[29])
        kind = RevisionKind(row[32])
        decision_origin = DecisionOrigin(row[33])
        if decision_origin is DecisionOrigin.DETERMINISTIC:
            if row[34] is not None or row[35] is not None:
                raise ValueError("malformed stored revision")
        else:
            self._require_token("stored model identifier", row[34])
            self._require_token("stored prompt version", row[35])
        return StateRevision(
            item_id=row[1],
            state_type=StateType(row[2]),
            revision=row[5],
            previous_revision=row[24],
            previous_status=row[25],
            new_status=row[26],
            previous_confidence=row[27],
            new_confidence=row[28],
            reason=row[29],
            supporting_evidence_count=row[22],
            counterevidence_count=row[23],
            decided_at=self._parse_time(row[30]),
            review_at=self._parse_time(row[7]),
            core_self_version=row[31],
            kind=kind,
            decision_origin=decision_origin,
            model_identifier=row[34],
            prompt_version=row[35],
            state=state,
        )

    @staticmethod
    def _payload_list(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        revision_key: int,
    ) -> tuple[str, ...]:
        allowed = {
            ("state_payload_alternatives", "alternative"),
            ("state_payload_constraints", "constraint_text"),
        }
        if (table, column) not in allowed:
            raise EventStoreError("event store operation failed")
        rows = connection.execute(
            f"SELECT {column} FROM {table} "
            "WHERE revision_key = ? ORDER BY ordinal LIMIT ?",
            (revision_key, MAX_STATE_LIST_ITEMS + 1),
        ).fetchall()
        if len(rows) > MAX_STATE_LIST_ITEMS or any(
            type(row[0]) is not str for row in rows
        ):
            raise EventStoreError("event store operation failed")
        values = tuple(row[0] for row in rows)
        try:
            for value in values:
                SelfStateStore._validate_text("stored list item", value)
        except SelfStateValidationError:
            raise EventStoreError("event store operation failed") from None
        return values

    @staticmethod
    def _protected_evidence(
        row: tuple[object, ...], expected_principal_id: str
    ) -> ProtectedEvidence:
        if any(type(value) is not str for value in row):
            raise ValueError("malformed evidence")
        expected = (
            AudienceType.PRIVATE_PARTICIPANT.value,
            expected_principal_id,
            ProvenanceType.PARTICIPANT_ASSERTION.value,
            VerificationStatus.UNVERIFIED.value,
            EventType.PARTICIPANT_INPUT.value,
            EventChannel.PRIVATE_CHAT.value,
            AudienceType.PRIVATE_PARTICIPANT.value,
            expected_principal_id,
            ProvenanceType.PARTICIPANT_ASSERTION.value,
            VerificationStatus.UNVERIFIED.value,
            AudienceType.PRIVATE_PARTICIPANT.value,
            expected_principal_id,
            ProvenanceType.PARTICIPANT_ASSERTION.value,
            VerificationStatus.UNVERIFIED.value,
            "active",
            row[0],
            expected_principal_id,
            ProvenanceType.PARTICIPANT_ASSERTION.value,
        )
        if row[2:] != expected:
            raise ValueError("malformed evidence")
        return ProtectedEvidence(
            event_id=row[0],
            role=EvidenceRole(row[1]),
            audience=AudienceType(row[2]),
            source_principal_id=row[3],
            provenance=ProvenanceType(row[4]),
            verification=VerificationStatus(row[5]),
        )

    @staticmethod
    def _validate_context(context: TrustedPrincipalContext) -> None:
        try:
            EventStore._validate_context(context)
        except EventValidationError as error:
            raise SelfStateValidationError(str(error)) from None

    @staticmethod
    def _validate_lookup(state_type: StateType, item_id: str) -> None:
        if not isinstance(state_type, StateType):
            raise SelfStateValidationError("invalid state type")
        SelfStateStore._require_token("item id", item_id)

    @staticmethod
    def _validate_change(
        state_type: StateType,
        change: InterestCandidateChange | BeliefCandidateChange | GoalCandidateChange,
        *,
        creating: bool,
    ) -> None:
        expected_class = {
            StateType.INTEREST: InterestCandidateChange,
            StateType.BELIEF: BeliefCandidateChange,
            StateType.GOAL: GoalCandidateChange,
        }[state_type]
        if not isinstance(change, expected_class):
            raise SelfStateValidationError("invalid state change type")
        SelfStateStore._require_token("item id", change.item_id)
        revision = change.revision
        if not isinstance(revision, RevisionContext):
            raise SelfStateValidationError("invalid revision context")
        SelfStateStore._require_token("operation id", revision.operation_id)
        if creating:
            if revision.expected_revision is not None:
                raise SelfStateValidationError("create must not expect a revision")
        elif (
            type(revision.expected_revision) is not int
            or revision.expected_revision < 1
        ):
            raise SelfStateValidationError("revision must have a positive expectation")
        SelfStateStore._validate_text("revision reason", revision.reason)
        SelfStateStore._validate_time("decision time", revision.decided_at)
        SelfStateStore._require_token(
            "initiating event id", revision.initiating_event_id
        )
        if not isinstance(revision.kind, RevisionKind):
            raise SelfStateValidationError("invalid revision kind")
        if not isinstance(revision.decision_origin, DecisionOrigin):
            raise SelfStateValidationError("invalid decision origin")
        if revision.decision_origin is DecisionOrigin.DETERMINISTIC:
            if (
                revision.model_identifier is not None
                or revision.prompt_version is not None
            ):
                raise SelfStateValidationError(
                    "deterministic revision has model metadata"
                )
        else:
            SelfStateStore._require_token("model identifier", revision.model_identifier)
            SelfStateStore._require_token("prompt version", revision.prompt_version)
        SelfStateStore._validate_time("review time", change.review_at)
        SelfStateStore._validate_event_ids(
            "supporting evidence", change.supporting_event_ids
        )
        counter_count = 0
        if isinstance(change, BeliefCandidateChange):
            SelfStateStore._validate_event_ids(
                "conflicting evidence",
                change.conflicting_event_ids,
                allow_empty=True,
            )
            counter_count = len(change.conflicting_event_ids)
        if (
            len(change.supporting_event_ids) + counter_count + 1
            > MAX_EVIDENCE_PER_REVISION
        ):
            raise SelfStateValidationError("too many evidence references")

        if isinstance(change, InterestCandidateChange):
            SelfStateStore._validate_text("topic", change.topic)
            SelfStateStore._validate_priority("interest strength", change.strength)
            if not isinstance(change.status, InterestStatus):
                raise SelfStateValidationError("invalid interest status")
            SelfStateStore._validate_text("interest reason", change.reason_for_interest)
        elif isinstance(change, BeliefCandidateChange):
            SelfStateStore._validate_text("proposition", change.proposition)
            if not isinstance(change.stance, BeliefStance):
                raise SelfStateValidationError("invalid belief stance")
            if (
                type(change.confidence) is not float
                or not 0.0 <= change.confidence <= 1.0
            ):
                raise SelfStateValidationError("invalid belief confidence")
            if not isinstance(change.status, BeliefStatus):
                raise SelfStateValidationError("invalid belief status")
            if change.verification is not VerificationStatus.UNVERIFIED:
                raise SelfStateValidationError("belief cannot become verified")
            if set(change.supporting_event_ids) & set(change.conflicting_event_ids):
                raise SelfStateValidationError("evidence roles conflict")
            SelfStateStore._validate_text_list("alternatives", change.alternatives)
        else:
            SelfStateStore._validate_text("goal description", change.description)
            SelfStateStore._validate_text("goal rationale", change.rationale)
            SelfStateStore._validate_priority("goal priority", change.priority)
            if not isinstance(change.status, GoalStatus):
                raise SelfStateValidationError("invalid goal status")
            SelfStateStore._validate_text_list("constraints", change.constraints)
            SelfStateStore._validate_text("review condition", change.review_condition)

    @staticmethod
    def _validate_priority(field: str, value: object) -> None:
        if type(value) is not int or not 0 <= value <= 100:
            raise SelfStateValidationError(f"invalid {field}")

    @staticmethod
    def _validate_event_ids(
        field: str, values: object, *, allow_empty: bool = False
    ) -> None:
        minimum = 0 if allow_empty else 1
        if (
            type(values) is not tuple
            or not minimum <= len(values) <= MAX_EVIDENCE_PER_REVISION
        ):
            raise SelfStateValidationError(f"invalid {field}")
        for value in values:
            SelfStateStore._require_token(field, value)
        if len(set(values)) != len(values):
            raise SelfStateValidationError(f"invalid {field}")

    @staticmethod
    def _validate_text_list(field: str, values: object) -> None:
        if type(values) is not tuple or len(values) > MAX_STATE_LIST_ITEMS:
            raise SelfStateValidationError(f"invalid {field}")
        for value in values:
            SelfStateStore._validate_text(field, value)

    @staticmethod
    def _validate_text(field: str, value: object) -> None:
        if not isinstance(value, str) or not value:
            raise SelfStateValidationError(f"invalid {field}")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise SelfStateValidationError(f"invalid {field}") from None
        if len(encoded) > MAX_STATE_TEXT_BYTES:
            raise SelfStateValidationError(f"invalid {field}")
        normalized = unicodedata.normalize("NFKC", value)
        representations = [normalized]
        decoded = normalized
        for _ in range(_MAX_PERCENT_DECODING_DEPTH):
            if _PERCENT_ESCAPE.search(decoded) is None:
                break
            try:
                decoded = unquote_to_bytes(decoded).decode("utf-8")
            except UnicodeDecodeError:
                raise SelfStateValidationError(f"invalid {field}") from None
            decoded = unicodedata.normalize("NFKC", decoded)
            representations.append(decoded)
        if _PERCENT_ESCAPE.search(decoded) is not None:
            raise SelfStateValidationError(f"invalid {field}")
        inspection_representations = representations + [
            unicodedata.normalize(
                "NFKC",
                "".join(
                    character
                    for character in unicodedata.normalize("NFKD", representation)
                    if not unicodedata.category(character).startswith("M")
                ),
            )
            for representation in representations
        ]
        for representation in inspection_representations:
            if any(
                (ord(character) < 32 or unicodedata.category(character) in {"Cf", "Cs"})
                and character not in "\n\t"
                for character in representation
            ):
                raise SelfStateValidationError(f"invalid {field}")
            compact = representation.strip()
            if not compact:
                raise SelfStateValidationError(f"invalid {field}")
            if (
                SelfStateStore._decodes_to_text(compact)
                or _MIXED_SCRIPT_TOKEN.search(representation) is not None
                or any(
                    pattern.search(representation) for pattern in _DISALLOWED_STATE_TEXT
                )
            ):
                raise SelfStateValidationError(f"invalid {field}")

    @staticmethod
    def _decodes_to_text(value: str) -> bool:
        candidate = value.translate(str.maketrans("", "", " \n\t"))
        if _HEX_ONLY_TEXT.fullmatch(candidate) is not None:
            return SelfStateStore._bytes_are_printable_text(bytes.fromhex(candidate))
        if len(candidate) % 4 != 0 or _BASE64_ONLY_TEXT.fullmatch(candidate) is None:
            return False
        try:
            decoded = base64.b64decode(candidate, altchars=b"-_", validate=True)
        except binascii.Error:
            return False
        return SelfStateStore._bytes_are_printable_text(decoded)

    @staticmethod
    def _bytes_are_printable_text(value: bytes) -> bool:
        try:
            decoded = value.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return all(
            character.isprintable() or character in "\n\t" for character in decoded
        )

    @staticmethod
    def _require_token(field: str, value: object) -> None:
        try:
            EventStore._require_token(field, value)
        except EventValidationError as error:
            raise SelfStateValidationError(str(error)) from None

    @staticmethod
    def _validate_time(field: str, value: object) -> None:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise SelfStateValidationError(f"invalid {field}")
        try:
            if value.utcoffset() is None:
                raise SelfStateValidationError(f"invalid {field}")
            EventStore._serialize_time(value)
        except (OverflowError, TypeError, ValueError):
            raise SelfStateValidationError(f"invalid {field}") from None

    @staticmethod
    def _serialize_time(value: datetime) -> str:
        return EventStore._serialize_time(value)

    @staticmethod
    def _parse_time(value: object) -> datetime:
        if type(value) is not str:
            raise ValueError("malformed timestamp")
        parsed = datetime.fromisoformat(value)
        if EventStore._serialize_time(parsed) != value:
            raise ValueError("noncanonical timestamp")
        return parsed
