"""Reviewed Stage 0 EventStore v6 erasure and quarantine boundaries.

Only event references with PRIVATE_PAYLOAD category are accepted. Metadata and
invalidated protected edges remain for identity/replay checks, never for model or
participant rendering. No database activation or route wiring is provided.
"""

from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
import json
import sqlite3
from threading import RLock
from typing import TypeVar

from app.core.agent_output_store import AgentOutputStore
from app.core.events import TrustedPrincipalContext
from app.core.self_state import StateType
from app.core.self_state_store import SelfStateStore
from app.core.store import EventStore, SCHEMA_VERSION
from app.core.generic_graph import validate_generic_graph, erase_generic_source
from app.cognition.narrative_store import (
    MAX_NARRATIVE_RECEIPTS,
    erase_narrative_source,
    validate_narrative_graph,
)
from app.core.shared_graph import (
    validate_shared_graph,
    erase_shared_source,
    erase_shared_review,
    erase_model_shared_candidates,
    revoke_shared_authorization,
)

from .deletion import DeletionCoordinator
from .ledger import PrivacyLedger
from .models import (
    ContentCategory,
    DeletionDisposition,
    DeletionRecord,
    DeletionRequest,
    ErasureReport,
    require_token,
    require_utc,
)


class ApplicationPrivacyError(RuntimeError):
    """A fixed, payload-free application validation reason."""


# Legacy private tables are validated here; v6 shared tables are validated by
# shared_graph and erased in the same transaction. Metadata-only tables include:
# schema_metadata and provider_{spend_suspensions,attempt_reservations,usage_attempts}.
# Provider records contain usage/control metadata, never prompts or model results.
_GRAPH_CHECKS = (
    (
        "missing_input_sequence",
        """
        SELECT 1 FROM event_envelopes e LEFT JOIN event_input_sequences s USING(event_id)
        WHERE s.event_id IS NULL
        """,
    ),
    (
        "invalid_reply_resolution",
        """
        SELECT 1 FROM agent_reply_resolutions r JOIN agent_reply_operations o USING(operation_id)
        WHERE (r.outcome='completed') <> (o.result_state='persisted')
           OR (r.outcome='completed' AND r.release_id <> 'release:' || r.operation_id)
        """,
    ),
    (
        "unsupported_shared_state",
        "SELECT 1 FROM state_items WHERE scope <> 'private_candidate'",
    ),
    (
        "invalid_event_provenance",
        """
        SELECT 1 FROM event_envelopes e LEFT JOIN protected_event_taints t USING(event_id)
        WHERE t.event_id IS NULL OR t.source_event_id <> e.event_id
           OR t.source_principal_id <> e.audience_principal_id
           OR t.source_provenance_type <> e.provenance_type
    """,
    ),
    (
        "invalid_private_payload",
        """
        SELECT 1 FROM private_payloads p LEFT JOIN event_envelopes e USING(payload_id)
        WHERE e.event_id IS NULL OR p.audience_principal_id <> e.audience_principal_id
           OR p.audience_type <> e.audience_type OR p.provenance_type <> e.provenance_type
           OR p.verification_status <> e.verification_status OR p.retention_status <> 'active'
    """,
    ),
    (
        "invalid_state_chain",
        """
        SELECT 1 FROM state_items i WHERE i.current_revision < 1
          OR i.current_revision <> (SELECT COUNT(*) FROM state_revisions r WHERE r.item_key=i.item_key)
          OR i.current_revision <> (SELECT MAX(revision_number) FROM state_revisions r WHERE r.item_key=i.item_key)
    """,
    ),
    (
        "invalid_state_operation",
        """
        SELECT 1 FROM state_operations o LEFT JOIN state_revisions r USING(operation_key)
        JOIN state_items i ON i.item_key=o.item_key
        WHERE r.revision_key IS NULL OR r.item_key <> o.item_key
           OR r.revision_number <> o.revision_number OR o.scope <> i.scope
           OR o.audience_principal_id <> i.audience_principal_id
    """,
    ),
    (
        "missing_state_provenance",
        """
        SELECT 1 FROM state_revisions r WHERE
          (SELECT COUNT(*) FROM protected_state_provenance s
           WHERE s.revision_key=r.revision_key AND s.evidence_role='initiating') <> 1
          OR NOT EXISTS (SELECT 1 FROM protected_state_provenance s
              WHERE s.revision_key=r.revision_key AND s.evidence_role IN ('supporting','counter'))
          OR (SELECT COUNT(*) FROM protected_state_provenance s
              WHERE s.revision_key=r.revision_key) > 50
    """,
    ),
    (
        "invalid_state_provenance",
        """
        SELECT 1 FROM protected_state_provenance s
        JOIN state_revisions r USING(revision_key) JOIN state_items i USING(item_key)
        JOIN event_envelopes e USING(event_id)
        WHERE s.source_principal_id <> i.audience_principal_id
           OR s.source_principal_id <> e.audience_principal_id
           OR s.audience_type <> e.audience_type OR s.source_provenance_type <> e.provenance_type
           OR s.verification_status <> e.verification_status
           OR (s.retention_status='active' AND e.payload_id IS NULL)
    """,
    ),
    (
        "invalid_state_payload",
        """
        SELECT 1 FROM state_revisions r JOIN state_items i USING(item_key)
        LEFT JOIN state_revision_payloads p USING(revision_key)
        WHERE (p.state_type <> i.state_type OR p.retention_status <> 'active')
           OR ((p.revision_key IS NULL) <> EXISTS (
               SELECT 1 FROM state_revisions h JOIN protected_state_provenance s USING(revision_key)
               WHERE h.item_key=i.item_key AND s.retention_status <> 'active'))
    """,
    ),
    (
        "missing_reply_provenance",
        """
        SELECT 1 FROM agent_reply_operations o WHERE o.source_count <>
          (SELECT COUNT(*) FROM agent_reply_sources s WHERE s.operation_id=o.operation_id)
          OR NOT EXISTS (SELECT 1 FROM agent_reply_sources s WHERE s.operation_id=o.operation_id
                         AND s.event_id=o.initiating_event_id)
    """,
    ),
    (
        "invalid_reply_provenance",
        """
        SELECT 1 FROM agent_reply_sources s JOIN agent_reply_operations o USING(operation_id)
        JOIN event_envelopes e USING(event_id)
        WHERE s.source_principal_id <> o.audience_principal_id
           OR s.source_principal_id <> e.audience_principal_id
           OR (o.retention_status='active' AND (s.retention_status <> 'active' OR e.payload_id IS NULL))
    """,
    ),
    (
        "invalid_reply_payload",
        """
        SELECT 1 FROM agent_reply_operations o LEFT JOIN agent_reply_payloads p USING(operation_id)
        JOIN event_envelopes e ON e.event_id=o.initiating_event_id
        WHERE o.correlation_id <> e.correlation_id
          OR (o.retention_status <> 'active' AND p.operation_id IS NOT NULL)
          OR (o.retention_status='active' AND (
              (o.result_state='persisted') <> (p.operation_id IS NOT NULL)
              OR p.retention_status <> 'active'))
    """,
    ),
)


def _validate_graph(
    db: sqlite3.Connection, schema_version: int = SCHEMA_VERSION
) -> None:
    if schema_version == 4:
        EventStore._verify_schema_version(db, 4)
        checks = _GRAPH_CHECKS[2:]  # v4 has no resolution or commit-sequence tables.
    else:
        EventStore._verify_schema_version(db, schema_version)
        checks = _GRAPH_CHECKS
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ApplicationPrivacyError("application_integrity_failure")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ApplicationPrivacyError("orphaned_application_data")
    if schema_version == 14:
        from app.core.intellectual_state import validate_private_graph

        try:
            validate_private_graph(db)
        except (ValueError, TypeError, KeyError, IndexError):
            raise ApplicationPrivacyError("invalid_intellectual_graph") from None
    if schema_version in {6, 7, 8, 9, 10, 11, 12, 13, 14}:
        try:
            validate_shared_graph(db)
        except ValueError as error:
            raise ApplicationPrivacyError(str(error)) from None
    if schema_version in {7, 8, 9, 10, 11, 12, 13, 14}:
        try:
            validate_generic_graph(db)
        except (ValueError, KeyError, TypeError, IndexError) as error:
            raise ApplicationPrivacyError("invalid_generic_graph") from error
    if (
        schema_version in {9, 10, 11, 12, 13, 14}
        and db.execute(
            """SELECT 1 FROM private_extraction_receipts r
        JOIN event_envelopes e ON e.event_id=r.source_event_id
        LEFT JOIN private_payloads p ON p.payload_id=e.payload_id
        WHERE r.source_principal_id<>e.audience_principal_id
           OR e.event_type<>'participant_input'
           OR e.audience_type<>'private_participant'
           OR e.provenance_type<>'participant_assertion'
           OR e.verification_status<>'unverified'
           OR p.audience_principal_id<>e.audience_principal_id
           OR p.provenance_type<>'participant_assertion'
           OR p.verification_status<>'unverified'
           OR p.retention_status<>'active'
           OR p.payload_id IS NULL LIMIT 1"""
        ).fetchone()
        is not None
    ):
        raise ApplicationPrivacyError("invalid_extraction_receipt")
    if schema_version in {10, 11, 12, 13, 14}:
        _validate_typed_receipts(db)
    if schema_version in {12, 13, 14}:
        try:
            validate_narrative_graph(db)
            _validate_narrative_receipts(db)
        except ValueError:
            raise ApplicationPrivacyError("invalid_narrative_graph") from None
    for reason, sql in checks:
        if db.execute(sql).fetchone() is not None:
            raise ApplicationPrivacyError(reason)


def _validate_typed_receipts(db: sqlite3.Connection) -> None:
    """A typed receipt must contain exactly the exposed heads' source union."""
    from app.cognition.worker_typed import _HANDLE, parse_typed_proposal

    rows = db.execute(
        "SELECT operation_id,source_event_id,source_principal_id,proposal FROM private_extraction_receipts WHERE operation_id LIKE 'typed:%'"
    ).fetchall()
    try:
        for _operation_id, source_event_id, principal_id, raw in rows:
            if len(raw.encode("utf-8")) > 131_072:
                raise ValueError
            value = json.loads(raw)
            if (
                type(value) is not dict
                or set(value) != {"version", "snapshot", "proposal"}
                or value["version"] != "private-typed-receipt-v1"
                or type(value["snapshot"]) is not dict
                or set(value["snapshot"]) != {"selected", "visible", "sources"}
            ):
                raise ValueError
            if value["proposal"] is not None:
                parse_typed_proposal(json.dumps(value["proposal"]))
            selected = value["snapshot"]["selected"]
            visible = value["snapshot"]["visible"]
            sources = value["snapshot"]["sources"]
            if (
                type(selected) is not list
                or len(selected) > 8
                or type(visible) is not list
                or any(
                    type(head) is not list
                    or len(head) != 2
                    or type(head[0]) is not str
                    or type(head[1]) is not int
                    or head[1] < 1
                    for head in visible
                )
                or len({tuple(head) for head in visible}) != len(visible)
                or type(sources) is not list
                or len(sources) > 50
                or any(type(source) is not str for source in sources)
                or len(set(sources)) != len(sources)
            ):
                raise ValueError
            expected = set()
            handles = set()
            selected_heads = set()
            role_row_count = 2
            for selected_row in selected:
                if (
                    type(selected_row) is not list
                    or len(selected_row) != 4
                    or type(selected_row[0]) is not str
                    or _HANDLE.fullmatch(selected_row[0]) is None
                    or type(selected_row[1]) is not str
                    or type(selected_row[2]) is not int
                    or selected_row[2] < 1
                    or type(selected_row[3]) is not str
                    or selected_row[0] in handles
                    or tuple(selected_row[1:3]) not in {tuple(head) for head in visible}
                    or tuple(selected_row[1:3]) in selected_heads
                ):
                    raise ValueError
                handles.add(selected_row[0])
                selected_heads.add(tuple(selected_row[1:3]))
                _handle, item_id, revision, kind = selected_row
                item = db.execute(
                    """SELECT i.state_type,p.retention_status FROM state_items i
                       JOIN state_revisions r USING(item_key)
                       JOIN state_revision_payloads p USING(revision_key)
                       WHERE i.scope='private_candidate' AND i.audience_principal_id=?
                         AND i.item_id=? AND r.revision_number=?""",
                    (principal_id, item_id, revision),
                ).fetchone()
                if item != (
                    {
                        "durable_interest": "interest",
                        "belief_or_hypothesis": "belief",
                        "goal_or_intention": "goal",
                    }[kind],
                    "active",
                ):
                    raise ValueError
                provenance = db.execute(
                    """SELECT p.event_id,p.evidence_role,p.retention_status FROM state_items i
                       JOIN state_revisions r USING(item_key)
                       JOIN protected_state_provenance p USING(revision_key)
                       WHERE i.scope='private_candidate' AND i.audience_principal_id=?
                         AND i.item_id=? AND r.revision_number<=?""",
                    (principal_id, item_id, revision),
                ).fetchall()
                if not provenance or any(
                    status != "active" for _event, _role, status in provenance
                ):
                    raise ValueError
                expected.update(event_id for event_id, _role, _status in provenance)
                role_row_count += len(provenance)
            if expected != set(sources) or role_row_count > 50:
                raise ValueError
    except (KeyError, TypeError, UnicodeError, ValueError):
        raise ApplicationPrivacyError("invalid_typed_receipt") from None


def _validate_narrative_receipts(db: sqlite3.Connection) -> None:
    """Receipts bind exactly the reply's private source and narrative selection."""
    from app.cognition.worker_narrative import _HANDLE, parse_narrative_proposal

    rows = db.execute(
        "SELECT operation_id,source_event_id,principal_id,receipt FROM private_narrative_receipts"
    ).fetchall()
    if len(rows) > MAX_NARRATIVE_RECEIPTS:
        raise ValueError("invalid narrative receipts")
    for operation, source, principal, raw in rows:
        try:
            if type(raw) is not str or len(raw.encode("utf-8")) > 65536:
                raise ValueError
            value = json.loads(raw)
            if type(value) is not dict or set(value) != {
                "heads",
                "selected",
                "sources",
                "proposal",
            }:
                raise ValueError
            heads = tuple(tuple(row) for row in value["heads"])
            selected = tuple(tuple(row) for row in value["selected"])
            sources = tuple(value["sources"])
            if (
                len(heads) > 16
                or len(selected) > 16
                or not 1 <= len(sources) <= 100
                or tuple(sorted(set(heads))) != heads
                or tuple(sorted(set(sources))) != sources
                or len({handle for handle, _ in selected}) != len(selected)
                or len({operation_id for _, operation_id in selected}) != len(selected)
                or any(
                    len(row) != 2
                    or type(row[0]) is not str
                    or type(row[1]) is not int
                    or row[1] < 1
                    for row in heads
                )
                or any(
                    len(row) != 2
                    or type(row[0]) is not str
                    or _HANDLE.fullmatch(row[0]) is None
                    or type(row[1]) is not str
                    for row in selected
                )
            ):
                raise ValueError
            if value["proposal"] is not None:
                parse_narrative_proposal(json.dumps(value["proposal"]))
            reply = "reply:" + operation.removeprefix("narrative:")
            if not operation.startswith("narrative:"):
                raise ValueError
            row = db.execute(
                "SELECT initiating_event_id,audience_principal_id FROM agent_reply_operations WHERE operation_id=?",
                (reply,),
            ).fetchone()
            reply_sources = tuple(
                item[0]
                for item in db.execute(
                    "SELECT event_id FROM agent_reply_sources WHERE operation_id=? ORDER BY event_id",
                    (reply,),
                )
            )
            exposed = {
                item[0]
                for item in db.execute(
                    "SELECT narrative_operation_id FROM protected_reply_narrative WHERE reply_operation_id=? AND principal_id=?",
                    (reply, principal),
                )
            }
            if (
                row != (source, principal)
                or sources != reply_sources
                or not {operation_id for _, operation_id in selected} <= exposed
            ):
                raise ValueError
        except (TypeError, ValueError, KeyError, IndexError, UnicodeError):
            raise ValueError("invalid narrative receipts") from None


def _erase(
    db: sqlite3.Connection,
    request: DeletionRequest,
    schema_version: int = SCHEMA_VERSION,
) -> ErasureReport:
    if request.content_category is not ContentCategory.PRIVATE_PAYLOAD:
        raise ApplicationPrivacyError("unsupported_source_category")
    db.execute("PRAGMA secure_delete=ON")
    db.execute("PRAGMA synchronous=FULL")
    with db:
        db.execute("BEGIN IMMEDIATE")
        _validate_graph(db, schema_version)
        source = db.execute(
            "SELECT audience_principal_id, payload_id FROM event_envelopes WHERE event_id=?",
            (request.content_reference,),
        ).fetchone()
        if source is None:
            raise ApplicationPrivacyError("source_identity_missing")
        if source[0] != request.principal_id:
            raise ApplicationPrivacyError("source_ownership_mismatch")
        # All revisions of an affected item are tainted, including later revisions
        # whose direct edges no longer mention this event. Preserve identity rows.
        from app.core.intellectual_state import erase_source

        erase_source(db, "event:" + request.content_reference)
        items = db.execute(
            "SELECT DISTINCT r.item_key FROM state_revisions r "
            "JOIN protected_state_provenance s USING(revision_key) WHERE s.event_id=?",
            (request.content_reference,),
        ).fetchall()
        for (item_key,) in items:
            db.execute(
                "DELETE FROM state_revision_payloads WHERE revision_key IN "
                "(SELECT revision_key FROM state_revisions WHERE item_key=?)",
                (item_key,),
            )
            db.execute(
                "UPDATE protected_state_provenance SET retention_status='invalidated' "
                "WHERE revision_key IN (SELECT revision_key FROM state_revisions WHERE item_key=?)",
                (item_key,),
            )
        replies = db.execute(
            "SELECT operation_id FROM agent_reply_sources WHERE event_id=?",
            (request.content_reference,),
        ).fetchall()
        for (operation_id,) in replies:
            db.execute(
                "DELETE FROM agent_reply_payloads WHERE operation_id=?", (operation_id,)
            )
            db.execute(
                "UPDATE agent_reply_operations SET retention_status='invalidated' WHERE operation_id=?",
                (operation_id,),
            )
            db.execute(
                "UPDATE agent_reply_sources SET retention_status='invalidated' WHERE operation_id=?",
                (operation_id,),
            )
        if schema_version in {7, 8, 9, 10, 11, 12, 13, 14}:
            erase_generic_source(db, request.content_reference)
        if schema_version in {12, 13, 14}:
            erase_narrative_source(db, request.content_reference)
            db.execute(
                """DELETE FROM private_narrative_receipts WHERE source_event_id=?
                OR EXISTS (SELECT 1 FROM json_each(
                    CASE WHEN json_valid(receipt) THEN
                        json_extract(receipt,'$.sources') ELSE NULL END
                ) WHERE value=?)""",
                (request.content_reference, request.content_reference),
            )
        if schema_version in {6, 7, 8, 9, 10, 11, 12, 13, 14}:
            erase_shared_source(db, request.content_reference)
        if schema_version in {9, 10, 11, 12, 13, 14}:
            db.execute(
                """DELETE FROM private_extraction_receipts
                WHERE source_event_id=? OR EXISTS (
                    SELECT 1 FROM json_each(
                        CASE WHEN json_valid(proposal) THEN
                            json_extract(proposal,'$.snapshot.sources') ELSE NULL END
                    ) WHERE value=?
                )""",
                (request.content_reference, request.content_reference),
            )
        db.execute("DELETE FROM private_payloads WHERE payload_id=?", (source[1],))
        _validate_graph(db, schema_version)
    # SQLite updates indexes in this transaction. Legacy shared rows remain
    # unsupported; exactly the v6 graph is cascaded above, including removable
    # candidate fingerprints. Unknown schemas/categories continue to fail closed.
    return ErasureReport(
        True, True, frozenset(ContentCategory.required_erasure_categories()), ()
    )


class EventStoreErasureAdapter:
    """Use only behind DeletionCoordinator's trusted pending-first boundary."""

    def __init__(self, store: EventStore):
        self.store = store

    def erase(self, deletion: DeletionRequest) -> ErasureReport:
        try:
            with closing(self.store._open()) as db:
                return _erase(db, deletion)
        except ApplicationPrivacyError as error:
            return ErasureReport(False, False, frozenset(), (str(error),))

    def apply_revocation(
        self, consent_id: str, revoked_at: datetime, scope=None
    ) -> bool:
        require_token(consent_id, "consent id")
        require_utc(revoked_at, "revocation timestamp")
        with closing(self.store._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.store._verify_current_schema(db)
            validate_shared_graph(db)
            erase_shared_review(db, consent_id)
            erase_model_shared_candidates(db, consent_id, scope)
            validate_shared_graph(db)
        return True


class EventStoreRestoreAdapter:
    """Callback-lifetime descriptor path supplied by QuarantinedRestore only.

    Do not construct EventStore here: resolving the descriptor or initializing
    WAL would bypass the recovery owner's identity and sidecar guarantees.
    Every callback closes its writer. Eligibility remains separate from activation.
    """

    def __init__(self, quarantine: Path):
        self._path = quarantine
        self._released = False

    def _open(self) -> sqlite3.Connection:
        if self._released:
            raise ApplicationPrivacyError("quarantine_adapter_released")
        db = sqlite3.connect(
            f"{self._path.as_uri()}?mode=rw", uri=True, isolation_level=None
        )
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def apply_deletion(self, record: DeletionRecord) -> ErasureReport:
        with closing(self._open()) as db:
            return _erase(
                db, record.request, db.execute("PRAGMA user_version").fetchone()[0]
            )

    def apply_revocation(
        self, consent_id: str, revoked_at: datetime, scope=None
    ) -> bool:
        require_token(consent_id, "consent id")
        require_utc(revoked_at, "revocation timestamp")
        with closing(self._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                _validate_graph(db, version)
                if version >= 11:
                    erase_shared_review(db, consent_id)
                    erase_model_shared_candidates(db, consent_id, scope)
                    _validate_graph(db, version)
        return True

    def rebuild_dependencies(self) -> bool:
        with closing(self._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                _validate_graph(db, version)
                if version >= 11:
                    # Legacy receipts did not bind the exact configured audience.
                    # They cannot reauthorize a proposal after recovery.
                    db.execute(
                        "DELETE FROM private_extraction_receipts WHERE operation_id LIKE 'sharedgen:%' "
                        "AND json_extract(proposal,'$.version') IS NOT 'shared-proposal-receipt-v2'"
                    )
                    # The external privacy ledger has no review-decision journal.
                    # A restored snapshot cannot establish that a later local
                    # revocation did not occur, so all reviewed grants fail closed.
                    for (operation_id,) in db.execute(
                        "SELECT o.operation_id FROM shared_operations o "
                        "JOIN protected_shared_manifests m USING(operation_id) "
                        "WHERE o.status<>'invalidated' "
                        "AND json_extract(m.binding,'$.reviewed')=1"
                    ).fetchall():
                        revoke_shared_authorization(db, operation_id)
                    _validate_graph(db, version)
        # All supported indexes are SQLite indexes, maintained atomically above.
        return True

    def release_writers(self) -> bool:
        self._released = True
        return True


class PrivacyReadBlocked(RuntimeError):
    """Participant retrieval is unavailable until trusted reconciliation."""


_T = TypeVar("_T")


class PrivacyReadGate:
    """Trusted startup/service boundary; raw storage is an internal capability.

    Starts closed, replays even APPLIED records against this database, and fences
    participant reads against newly committed privacy controls. This Stage 0
    boundary is installed by the explicit synthetic private conversation runtime;
    ordinary disabled web and owner routes retain their existing configuration.
    Callers must never expose raw stores or an eligible quarantine to participants.
    """

    def __init__(
        self,
        ledger: PrivacyLedger,
        store: EventStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self._ledger = ledger
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._generation: int | None = None
        self._identity: tuple[int, int] | None = None
        self._next_revocation_at: datetime | None = None

    def _database_identity(self) -> tuple[int, int]:
        status = self._store.database_path.stat()
        return status.st_dev, status.st_ino

    def reconcile(self) -> bool:
        with self._lock:
            self._generation = None
            self._identity = None
            self._next_revocation_at = None
            adapter = EventStoreErasureAdapter(self._store)
            try:
                identity = self._database_identity()
                DeletionCoordinator(self._ledger, adapter).reconcile_pending()
                snapshot = self._ledger.reconciliation_snapshot()
                now = self._clock()
                require_utc(now, "reconciliation timestamp")
                self._next_revocation_at = min(
                    (at for _, at in snapshot.revocations if at > now), default=None
                )
                for consent_id, revoked_at in snapshot.revocations:
                    if revoked_at > now:
                        continue
                    if not adapter.apply_revocation(
                        consent_id, revoked_at, self._ledger.consent_scope(consent_id)
                    ):
                        return False
                for record in snapshot.deletions:
                    if (
                        record.disposition is not DeletionDisposition.APPLIED
                        or adapter.erase(record.request).blockers
                    ):
                        return False
                with closing(self._store._open()) as db:
                    with db:
                        db.execute("BEGIN")
                        _validate_graph(db)
                if (
                    self._ledger.control_generation() != snapshot.generation
                    or self._database_identity() != identity
                ):
                    return False
                self._generation = snapshot.generation
                self._identity = identity
                return True
            except Exception:
                return False

    def _read(self, callback: Callable[[], _T]) -> _T:
        with self._lock, closing(self._ledger._open()) as db:
            with db:
                # This short ledger fence orders a read before/after durable intent;
                # reconciliation never holds it across coordinator ledger writes.
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT generation FROM privacy_control_generation WHERE singleton=1"
                ).fetchone()
                pending = db.execute(
                    "SELECT 1 FROM deletion_records WHERE disposition IN ('pending','incomplete_blocked')"
                ).fetchone()
                if (
                    self._generation is None
                    or row != (self._generation,)
                    or pending is not None
                    or self._database_identity() != self._identity
                    or (
                        self._next_revocation_at is not None
                        and self._clock() >= self._next_revocation_at
                    )
                ):
                    raise PrivacyReadBlocked("privacy_reconciliation_required")
                result = callback()
                if self._database_identity() != self._identity:
                    raise PrivacyReadBlocked("application_database_changed")
                if (
                    self._next_revocation_at is not None
                    and self._clock() >= self._next_revocation_at
                ):
                    raise PrivacyReadBlocked("privacy_reconciliation_required")
                return result

    def authorized(self, callback: Callable[[], _T]) -> _T:
        """Run a short application read/write boundary under the privacy fence.

        Never hold this fence over model execution. Authorization is rechecked
        at subsequent persistence and response boundaries.
        """
        return self._read(callback)

    def retrieve_private_events(self, context: TrustedPrincipalContext):
        return self._read(lambda: self._store.retrieve_private_events(context))

    def retrieve_private_candidates(self, context: TrustedPrincipalContext):
        return self._read(
            lambda: SelfStateStore(self._store).retrieve_private_candidates(context)
        )

    def retrieve_private_revision_history(
        self, context: TrustedPrincipalContext, state_type: StateType, item_id: str
    ):
        return self._read(
            lambda: SelfStateStore(self._store).retrieve_private_revision_history(
                context, state_type, item_id
            )
        )

    def inspect_private_revision_evidence(
        self,
        context: TrustedPrincipalContext,
        state_type: StateType,
        item_id: str,
        revision_number: int,
    ):
        return self._read(
            lambda: SelfStateStore(self._store).inspect_private_revision_evidence(
                context, state_type, item_id, revision_number
            )
        )

    def read_reply(self, context: TrustedPrincipalContext, operation_id: str):
        return self._read(
            lambda: AgentOutputStore(self._store).read_reply(context, operation_id)
        )
