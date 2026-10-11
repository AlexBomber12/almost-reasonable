"""Reviewed Stage 0 EventStore v5 erasure and quarantine boundaries.

Only event references with PRIVATE_PAYLOAD category are accepted. Metadata and
invalidated protected edges remain for identity/replay checks, never for model or
participant rendering. No database activation or route wiring is provided.
"""

from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
from threading import RLock
from typing import TypeVar

from app.core.agent_output_store import AgentOutputStore
from app.core.events import TrustedPrincipalContext
from app.core.self_state import StateType
from app.core.self_state_store import SelfStateStore
from app.core.store import EventStore

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


# Every v5 table is either validated here, mutated below, or metadata-only:
# schema_metadata and provider_{spend_suspensions,attempt_reservations,usage_attempts}.
# Provider records contain usage/control metadata, never prompts or model results.
_GRAPH_CHECKS = (
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


def _validate_graph(db: sqlite3.Connection) -> None:
    EventStore._verify_current_schema(db)
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ApplicationPrivacyError("application_integrity_failure")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ApplicationPrivacyError("orphaned_application_data")
    for reason, sql in _GRAPH_CHECKS:
        if db.execute(sql).fetchone() is not None:
            raise ApplicationPrivacyError(reason)


def _erase(db: sqlite3.Connection, request: DeletionRequest) -> ErasureReport:
    if request.content_category is not ContentCategory.PRIVATE_PAYLOAD:
        raise ApplicationPrivacyError("unsupported_source_category")
    db.execute("PRAGMA secure_delete=ON")
    db.execute("PRAGMA synchronous=FULL")
    with db:
        db.execute("BEGIN IMMEDIATE")
        _validate_graph(db)
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
        db.execute("DELETE FROM private_payloads WHERE payload_id=?", (source[1],))
        _validate_graph(db)
    # SQLite updates its ordinary indexes in this transaction. No keyed content
    # fingerprints, summaries or generic memory exist in the exact v4 schema.
    # Shared state is provably empty above; any future category/object is blocked.
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
            return _erase(db, record.request)

    def apply_revocation(self, consent_id: str, revoked_at: datetime) -> bool:
        require_token(consent_id, "consent id")
        require_utc(revoked_at, "revocation timestamp")
        with closing(self._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                _validate_graph(db)
        # v4 has no persisted consent grants, disclosure authorization or consent
        # cache. External PrivacyLedger remains the sole current-use authority.
        return True

    def rebuild_dependencies(self) -> bool:
        with closing(self._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                _validate_graph(db)
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

    def __init__(self, ledger: PrivacyLedger, store: EventStore):
        self._ledger = ledger
        self._store = store
        self._lock = RLock()
        self._generation: int | None = None
        self._identity: tuple[int, int] | None = None

    def _database_identity(self) -> tuple[int, int]:
        status = self._store.database_path.stat()
        return status.st_dev, status.st_ino

    def reconcile(self) -> bool:
        with self._lock:
            self._generation = None
            self._identity = None
            adapter = EventStoreErasureAdapter(self._store)
            try:
                identity = self._database_identity()
                DeletionCoordinator(self._ledger, adapter).reconcile_pending()
                snapshot = self._ledger.reconciliation_snapshot()
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
                ):
                    raise PrivacyReadBlocked("privacy_reconciliation_required")
                result = callback()
                if self._database_identity() != self._identity:
                    raise PrivacyReadBlocked("application_database_changed")
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
