"""Read-only exact and complete cognition views over the existing schema.

The core retrieval APIs intentionally cap model-facing results at 100 records.
Cognition must not mistake those presentation bounds for authoritative identity
or ranking bounds, so this adapter performs participant-scoped read-only queries.
It creates no tables and exposes no mutation path.
"""

from dataclasses import dataclass, replace
from datetime import datetime
import sqlite3

from app.core.events import PrivateConversationEvent, TrustedPrincipalContext
from app.core.self_state import (
    BeliefCandidateChange,
    DecisionOrigin,
    GoalCandidateChange,
    InterestCandidateChange,
    PrivateCandidate,
    StateTransitionResult,
    TransitionStatus,
)
from app.core.self_state_store import (
    SelfStateIdentityConflictError,
    SelfStateStore,
)
from app.core.store import EventStore, MAX_PRIVATE_PAYLOAD_BYTES


StateChange = InterestCandidateChange | BeliefCandidateChange | GoalCandidateChange


@dataclass(frozen=True, slots=True)
class PrivateCandidateRead:
    candidate: PrivateCandidate
    decided_at: datetime
    decision_origin: DecisionOrigin
    revision_reason: str
    core_self_version: str
    model_identifier: str | None
    prompt_version: str | None
    evidence_event_ids: tuple[str, ...]
    has_invalidated_evidence: bool


class _BufferedRows:
    def __init__(self, rows: tuple[tuple[str], ...]):
        self._rows = rows

    def fetchall(self) -> list[tuple[str]]:
        return list(self._rows)


class _BatchedPayloadReader:
    """Supplies already-loaded list payloads to the core snapshot decoder."""

    def __init__(self, payloads: dict[tuple[str, int], tuple[tuple[str], ...]]):
        self._payloads = payloads

    def execute(self, query: str, parameters: tuple[object, ...]) -> _BufferedRows:
        table = (
            "state_payload_alternatives"
            if "state_payload_alternatives" in query
            else "state_payload_constraints"
        )
        return _BufferedRows(self._payloads.get((table, parameters[0]), ()))


def retrieve_exact_private_event(
    event_store: EventStore,
    context: TrustedPrincipalContext,
    event_id: str,
) -> PrivateConversationEvent | None:
    """Retrieve one active event without applying a recent-history limit."""

    event_store._validate_context(context)
    connection = event_store._open()
    try:
        with connection:
            connection.execute("BEGIN")
            event_store._verify_current_schema(connection)
            row = connection.execute(
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
                WHERE event.event_id = ?
                  AND event.audience_type = 'private_participant'
                  AND event.audience_principal_id = ?
                  AND payload.audience_type = event.audience_type
                  AND payload.audience_principal_id = event.audience_principal_id
                  AND payload.retention_status = 'active'
                """,
                (
                    MAX_PRIVATE_PAYLOAD_BYTES,
                    event_id,
                    context.principal_id.value,
                ),
            ).fetchone()
        return None if row is None else event_store._conversation_event(row)
    except (OverflowError, TypeError, ValueError):
        raise event_store._translated_error(
            sqlite3.DatabaseError("invalid stored event")
        ) from None
    except sqlite3.Error as error:
        raise event_store._translated_error(error) from None
    finally:
        connection.close()


class CognitionReadStore:
    """Narrow read-only adapter used only by deterministic cognition code."""

    def __init__(self, event_store: EventStore, state_store: SelfStateStore):
        if event_store.database_path != state_store._events.database_path:
            raise ValueError("event and state stores must use the same database")
        self._events = event_store
        self._state = state_store

    def retrieve_exact_private_event(
        self, context: TrustedPrincipalContext, event_id: str
    ) -> PrivateConversationEvent | None:
        return retrieve_exact_private_event(self._events, context, event_id)

    def has_private_event_envelope(
        self, context: TrustedPrincipalContext, event_id: str
    ) -> bool:
        """Authorize retained event identity without requiring readable content."""

        self._events._validate_context(context)
        connection = self._events._open()
        try:
            with connection:
                connection.execute("BEGIN")
                self._events._verify_current_schema(connection)
                row = connection.execute(
                    """
                    SELECT 1
                    FROM event_envelopes AS event
                    WHERE event.event_id = ?
                      AND event.audience_type = 'private_participant'
                      AND event.audience_principal_id = ?
                    LIMIT 1
                    """,
                    (event_id, context.principal_id.value),
                ).fetchone()
                return row is not None
        except sqlite3.Error as error:
            raise self._events._translated_error(error) from None
        finally:
            connection.close()

    def retrieve_exact_private_candidate(
        self, context: TrustedPrincipalContext, item_id: str
    ) -> PrivateCandidate | None:
        rows = self._retrieve_candidate_rows(context, item_id=item_id)
        return None if not rows else rows[0]

    def retrieve_all_private_candidates(
        self, context: TrustedPrincipalContext
    ) -> tuple[PrivateCandidate, ...]:
        return self._retrieve_candidate_rows(context)

    def retrieve_all_private_candidate_reads(
        self,
        context: TrustedPrincipalContext,
        *,
        include_belief_history: bool = False,
    ) -> tuple[PrivateCandidateRead, ...]:
        """Read current candidates and optionally every active belief revision."""

        self._state._validate_context(context)
        connection = self._events._open()
        try:
            with connection:
                connection.execute("BEGIN")
                self._events._verify_current_schema(connection)
                rows = self._query_candidate_rows(
                    connection,
                    context.principal_id.value,
                    None,
                    include_belief_history=include_belief_history,
                )
                revision_filter = (
                    "AND (revision.revision_number = item.current_revision "
                    "OR item.state_type = 'belief')"
                    if include_belief_history
                    else "AND revision.revision_number = item.current_revision"
                )
                evidence_rows = connection.execute(
                    """
                    SELECT DISTINCT
                        revision.item_key, source.event_id, source.retention_status
                    FROM state_items AS item
                    JOIN state_revisions AS revision
                      ON revision.item_key = item.item_key
                    JOIN protected_state_provenance AS source
                      ON source.revision_key = revision.revision_key
                    WHERE item.scope = 'private_candidate'
                      AND item.audience_type = 'private_participant'
                      AND item.audience_principal_id = ?
                      AND source.retention_status IN ('active', 'invalidated')
                    ORDER BY revision.item_key, source.event_id,
                             source.retention_status
                    """,
                    (context.principal_id.value,),
                ).fetchall()
                payload_rows = connection.execute(
                    f"""
                    SELECT
                        'state_payload_alternatives', alternative.revision_key,
                        alternative.ordinal, alternative.alternative
                    FROM state_items AS item
                    JOIN state_revisions AS revision
                      ON revision.item_key = item.item_key
                    JOIN state_payload_alternatives AS alternative
                      ON alternative.revision_key = revision.revision_key
                    WHERE item.scope = 'private_candidate'
                      AND item.audience_type = 'private_participant'
                      AND item.audience_principal_id = ?
                      AND EXISTS (
                          SELECT 1
                          FROM state_revisions AS head_revision
                          JOIN state_revision_payloads AS head_payload
                            ON head_payload.revision_key = head_revision.revision_key
                          WHERE head_revision.item_key = item.item_key
                            AND head_revision.revision_number = item.current_revision
                            AND head_payload.retention_status = 'active'
                      )
                      {revision_filter}
                    UNION ALL
                    SELECT
                        'state_payload_constraints', constraint_value.revision_key,
                        constraint_value.ordinal, constraint_value.constraint_text
                    FROM state_items AS item
                    JOIN state_revisions AS revision
                      ON revision.item_key = item.item_key
                    JOIN state_payload_constraints AS constraint_value
                      ON constraint_value.revision_key = revision.revision_key
                    WHERE item.scope = 'private_candidate'
                      AND item.audience_type = 'private_participant'
                      AND item.audience_principal_id = ?
                      AND EXISTS (
                          SELECT 1
                          FROM state_revisions AS head_revision
                          JOIN state_revision_payloads AS head_payload
                            ON head_payload.revision_key = head_revision.revision_key
                          WHERE head_revision.item_key = item.item_key
                            AND head_revision.revision_number = item.current_revision
                            AND head_payload.retention_status = 'active'
                      )
                      {revision_filter}
                    ORDER BY 2, 1, 3
                    """,
                    (context.principal_id.value, context.principal_id.value),
                ).fetchall()
                evidence_by_item: dict[int, set[str]] = {}
                invalidated_items: set[int] = set()
                for item_key, event_id, retention_status in evidence_rows:
                    evidence_by_item.setdefault(item_key, set()).add(event_id)
                    if retention_status == "invalidated":
                        invalidated_items.add(item_key)
                payloads: dict[tuple[str, int], list[tuple[str]]] = {}
                for table, revision_key, _ordinal, value in payload_rows:
                    payloads.setdefault((table, revision_key), []).append((value,))
                payload_reader = _BatchedPayloadReader(
                    {key: tuple(values) for key, values in payloads.items()}
                )
                records: list[PrivateCandidateRead] = []
                for row in rows:
                    self._state._validate_text("stored revision reason", row[29])
                    if include_belief_history and row[2] == "belief":
                        candidate = self._state._snapshot_from_row(
                            payload_reader, row[:24]
                        )
                        self._state._validate_snapshot_header(
                            candidate, row[24], row[25]
                        )
                    else:
                        candidate = self._state._current_snapshot_from_row(
                            payload_reader, row
                        )
                    records.append(
                        PrivateCandidateRead(
                            candidate,
                            self._state._parse_time(row[27]),
                            DecisionOrigin(row[28]),
                            row[29],
                            row[30],
                            row[31],
                            row[32],
                            tuple(sorted(evidence_by_item.get(row[0], ()))),
                            row[0] in invalidated_items,
                        )
                    )
                return tuple(records)
        except (OverflowError, TypeError, ValueError):
            raise self._events._translated_error(
                sqlite3.DatabaseError("invalid stored state")
            ) from None
        except sqlite3.Error as error:
            raise self._events._translated_error(error) from None
        finally:
            connection.close()

    def retrieve_private_revision_references(
        self,
        context: TrustedPrincipalContext,
        event_id: str,
    ) -> tuple[tuple[str, str, int], ...]:
        """Return every private revision tainted by one source event."""

        self._state._validate_context(context)
        connection = self._events._open()
        try:
            with connection:
                connection.execute("BEGIN")
                self._events._verify_current_schema(connection)
                rows = connection.execute(
                    """
                    SELECT DISTINCT
                        item.state_type, item.item_id, revision.revision_number
                    FROM state_items AS item
                    JOIN state_revisions AS revision
                      ON revision.item_key = item.item_key
                    JOIN protected_state_provenance AS source
                      ON source.revision_key = revision.revision_key
                    WHERE item.scope = 'private_candidate'
                      AND item.audience_type = 'private_participant'
                      AND item.audience_principal_id = ?
                      AND source.event_id = ?
                      AND source.retention_status IN ('active', 'invalidated')
                    ORDER BY item.state_type, item.item_id, revision.revision_number
                    """,
                    (context.principal_id.value, event_id),
                ).fetchall()
                return tuple(rows)
        except sqlite3.Error as error:
            raise self._events._translated_error(error) from None
        finally:
            connection.close()

    def replay_private_operation(
        self,
        context: TrustedPrincipalContext,
        change: StateChange,
    ) -> StateTransitionResult | None:
        """Return a durable duplicate or reject a reused operation identity."""

        self._state._validate_context(context)
        connection = self._events._open()
        try:
            with connection:
                connection.execute("BEGIN")
                self._events._verify_current_schema(connection)
                revision_key = self._state._find_operation(
                    connection,
                    context.principal_id.value,
                    change.revision.operation_id,
                )
                if revision_key is None:
                    return None
                stored = self._state._stored_change(connection, revision_key)
                normalized = replace(
                    change,
                    revision=replace(
                        change.revision,
                        decided_at=stored.revision.decided_at,
                    ),
                )
                if self._state._logical_change(stored) != self._state._logical_change(
                    normalized
                ):
                    raise SelfStateIdentityConflictError("operation identity conflict")
                return StateTransitionResult(
                    TransitionStatus.DUPLICATE,
                    self._state._snapshot_for_revision(connection, revision_key),
                )
        except sqlite3.Error as error:
            raise self._events._translated_error(error) from None
        finally:
            connection.close()

    def _retrieve_candidate_rows(
        self,
        context: TrustedPrincipalContext,
        *,
        item_id: str | None = None,
    ) -> tuple[PrivateCandidate, ...]:
        self._state._validate_context(context)
        connection = self._events._open()
        try:
            with connection:
                connection.execute("BEGIN")
                self._events._verify_current_schema(connection)
                rows = self._query_candidate_rows(
                    connection, context.principal_id.value, item_id
                )
                return tuple(
                    self._state._current_snapshot_from_row(connection, row)
                    for row in rows
                )
        except (OverflowError, TypeError, ValueError):
            raise self._events._translated_error(
                sqlite3.DatabaseError("invalid stored state")
            ) from None
        except sqlite3.Error as error:
            raise self._events._translated_error(error) from None
        finally:
            connection.close()

    @staticmethod
    def _query_candidate_rows(
        connection: sqlite3.Connection,
        principal_id: str,
        item_id: str | None,
        *,
        include_belief_history: bool = False,
    ) -> list[tuple[object, ...]]:
        item_filter = "" if item_id is None else "AND item.item_id = ?"
        revision_filter = (
            "AND (revision.revision_number = item.current_revision "
            "OR item.state_type = 'belief')"
            if include_belief_history
            else "AND revision.revision_number = item.current_revision"
        )
        parameters: tuple[object, ...] = (principal_id,)
        if item_id is not None:
            parameters += (item_id,)
        return connection.execute(
            f"""
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
                        revision.new_status,
                        revision.new_confidence,
                        (SELECT MAX(candidate.revision_number)
                         FROM state_revisions AS candidate
                         WHERE candidate.item_key = item.item_key),
                        revision.decided_at,
                        revision.decision_origin,
                        payload.revision_reason,
                        revision.core_self_version,
                        revision.model_identifier,
                        revision.prompt_version
                    FROM state_items AS item
                    JOIN state_revisions AS revision
                      ON revision.item_key = item.item_key
                    JOIN state_revision_payloads AS payload
                      ON payload.revision_key = revision.revision_key
                    WHERE item.scope = 'private_candidate'
                      AND item.audience_type = 'private_participant'
                      AND item.audience_principal_id = ?
                      AND payload.retention_status = 'active'
                      AND EXISTS (
                          SELECT 1
                          FROM state_revisions AS head_revision
                          JOIN state_revision_payloads AS head_payload
                            ON head_payload.revision_key = head_revision.revision_key
                          WHERE head_revision.item_key = item.item_key
                            AND head_revision.revision_number = item.current_revision
                            AND head_payload.retention_status = 'active'
                      )
                      {revision_filter}
                      {item_filter}
                    ORDER BY revision.revision_key, item.item_id
                    """,
            parameters,
        ).fetchall()
