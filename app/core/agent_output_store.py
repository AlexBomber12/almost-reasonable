"""Durable application-authorized private drafts and response-release metadata.

All operation fields are supplied by trusted application code, never inferred
from a proposal. A reservation survives interruption but grants no provider
retry or external effect. Conversation IDs are opaque application routing IDs.
"""

from contextlib import nullcontext
from dataclasses import dataclass, field as dataclass_field
from enum import StrEnum
import sqlite3

from .action_policy import (
    ActionPolicy,
    CapabilityState,
    PolicyDecision,
    TrustedActionContext,
)
from .agent_proposals import ReplyProposal
from .events import TrustedPrincipalContext
from .shared_graph import validate_shared_graph
from .generic_graph import MAX_GENERIC_GRAPH_REPLIES, validate_generic_graph

from .store import (
    EventIdentityConflictError,
    EventStore,
    EventValidationError,
    FaultPoint,
    MAX_PRIVATE_PAYLOAD_BYTES,
)


class ResultState(StrEnum):
    PENDING = "pending"
    PERSISTED = "persisted"


@dataclass(frozen=True, slots=True)
class ReplyOperation:
    """Trusted immutable identity and complete exposed source-event evidence."""

    operation_id: str
    initiating_event_id: str
    conversation_id: str
    correlation_id: str
    model_identifier: str
    prompt_version: str
    core_self_version: str
    source_event_ids: tuple[str, ...]
    shared_operation_ids: tuple[str, ...] = ()
    generic_operation_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PrivateReply:
    """Scoped storage result; persisted text is still unverified model inference."""

    operation: ReplyOperation
    internal_decision: str
    result_state: ResultState
    delivery_state: str
    content: str | None
    reservation_created: bool = dataclass_field(default=False, compare=False)


class AgentOutputStore:
    """Storage boundary sharing EventStore's transactions and security settings."""

    def __init__(self, event_store: EventStore):
        self._store = event_store

    @staticmethod
    def _validate(context: TrustedPrincipalContext, operation: ReplyOperation) -> None:
        EventStore._validate_context(context)
        if not isinstance(operation, ReplyOperation):
            raise EventValidationError("trusted reply operation required")
        for field in (
            "operation_id",
            "initiating_event_id",
            "conversation_id",
            "correlation_id",
            "model_identifier",
            "prompt_version",
            "core_self_version",
        ):
            EventStore._require_token(field, getattr(operation, field))
        sources = operation.source_event_ids
        if (
            type(sources) is not tuple
            or not 1 <= len(sources) <= 100
            or any(type(source) is not str for source in sources)
        ):
            raise EventValidationError("invalid reply sources")
        for source in sources:
            EventStore._require_token("source event", source)
        if (
            tuple(sorted(set(sources))) != sources
            or operation.initiating_event_id not in sources
        ):
            raise EventValidationError("invalid reply sources")

        shared = operation.shared_operation_ids
        if (
            type(shared) is not tuple
            or len(shared) > 16
            or any(type(item) is not str for item in shared)
            or tuple(sorted(set(shared))) != shared
        ):
            raise EventValidationError("invalid shared reply sources")
        for item in shared:
            EventStore._require_token("shared operation", item)
        generic = operation.generic_operation_ids
        if (
            type(generic) is not tuple
            or len(generic) > 16
            or any(type(item) is not str for item in generic)
            or tuple(sorted(set(generic))) != generic
        ):
            raise EventValidationError("invalid generic reply sources")
        for item in generic:
            EventStore._require_token("generic operation", item)

    @staticmethod
    def _binding(context: TrustedPrincipalContext, op: ReplyOperation) -> tuple:
        return (
            op.operation_id,
            context.principal_id.value,
            op.initiating_event_id,
            op.conversation_id,
            op.correlation_id,
            op.model_identifier,
            op.prompt_version,
            op.core_self_version,
            len(op.source_event_ids),
        )

    @staticmethod
    def _sources_valid(
        db: sqlite3.Connection, principal: str, op: ReplyOperation
    ) -> bool:
        validate_shared_graph(db)
        validate_generic_graph(db)
        all_sources = set(op.source_event_ids)
        for generic in op.generic_operation_ids:
            row = db.execute(
                """SELECT i.audience_principal_id,o.status FROM generic_memory_operations o
                JOIN generic_memory_items i ON i.item_id=o.item_id
                    AND i.audience_principal_id=o.audience_principal_id
                WHERE o.operation_id=? AND o.audience_principal_id=?""",
                (generic, principal),
            ).fetchone()
            if row != (principal, "active"):
                return False
            dependencies = {
                source[0]
                for source in db.execute(
                    "SELECT event_id FROM generic_memory_sources WHERE operation_id=? AND source_principal_id=?",
                    (generic, principal),
                )
            }
            if not dependencies <= set(op.source_event_ids):
                return False
        for shared in op.shared_operation_ids:
            authorized = db.execute(
                """SELECT 1 FROM shared_operations o
                JOIN protected_shared_audiences a USING(operation_id)
                WHERE o.operation_id=? AND o.status='active'
                    AND a.principal_id=? AND a.purpose='conversation'""",
                (shared, principal),
            ).fetchone()
            if authorized is None:
                return False
            all_sources.update(
                e[0]
                for e in db.execute(
                    "SELECT event_id FROM protected_shared_sources WHERE operation_id=?",
                    (shared,),
                )
            )
        if len(all_sources) > 100:
            return False
        for source in op.source_event_ids:
            row = db.execute(
                """
                SELECT e.audience_principal_id, e.event_type, e.channel,
                       e.audience_type, e.provenance_type, e.verification_status,
                       p.audience_principal_id, p.audience_type,
                       p.provenance_type, p.verification_status, p.retention_status,
                       t.source_event_id, t.source_principal_id, t.source_provenance_type,
                       (typeof(p.content)='text' AND length(CAST(p.content AS BLOB)) BETWEEN 1 AND ?)
                FROM event_envelopes e
                JOIN private_payloads p ON p.payload_id=e.payload_id
                JOIN protected_event_taints t ON t.event_id=e.event_id
                WHERE e.event_id=?
            """,
                (MAX_PRIVATE_PAYLOAD_BYTES, source),
            ).fetchone()
            if row != (
                principal,
                "participant_input",
                "private_chat",
                "private_participant",
                "participant_assertion",
                "unverified",
                principal,
                "private_participant",
                "participant_assertion",
                "unverified",
                "active",
                source,
                principal,
                "participant_assertion",
                1,
            ):
                return False
        return True

    def reserve_reply(
        self,
        context: TrustedPrincipalContext,
        operation: ReplyOperation,
        *,
        _connection: sqlite3.Connection | None = None,
    ) -> PrivateReply:
        """Reserve once before work; conflicting global operation identity fails closed."""
        self._validate(context, operation)
        db = _connection if _connection is not None else self._store._open()
        try:
            with nullcontext() if _connection is not None else db:
                if _connection is None:
                    db.execute("BEGIN IMMEDIATE")
                self._store._verify_current_schema(db)
                if not self._sources_valid(db, context.principal_id.value, operation):
                    raise EventValidationError("reply sources unavailable")
                initiating = db.execute(
                    "SELECT correlation_id FROM event_envelopes WHERE event_id=?",
                    (operation.initiating_event_id,),
                ).fetchone()
                if initiating != (operation.correlation_id,):
                    raise EventValidationError("reply correlation mismatch")
                existing = db.execute(
                    "SELECT * FROM agent_reply_operations WHERE operation_id=?",
                    (operation.operation_id,),
                ).fetchone()
                if existing is not None:
                    if existing[:9] != self._binding(context, operation):
                        raise EventIdentityConflictError(
                            "reply operation identity conflict"
                        )
                    result = self._read(db, context, operation.operation_id)
                    if result is None or result.operation != operation:
                        raise EventValidationError("reply operation unavailable")
                    return result
                if operation.generic_operation_ids and (
                    db.execute(
                        "SELECT COUNT(*) FROM protected_reply_generic"
                    ).fetchone()[0]
                    + len(operation.generic_operation_ids)
                    > MAX_GENERIC_GRAPH_REPLIES
                ):
                    raise EventValidationError("generic reply graph bound")
                db.execute(
                    """
                    INSERT INTO agent_reply_operations(operation_id, audience_principal_id,
                        initiating_event_id, conversation_id, correlation_id, model_identifier,
                        prompt_version, core_self_version, source_count, shared_count,
                        generic_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        *self._binding(context, operation),
                        len(operation.shared_operation_ids),
                        len(operation.generic_operation_ids),
                    ),
                )
                db.executemany(
                    """
                    INSERT INTO agent_reply_sources(operation_id, event_id, source_principal_id)
                    VALUES (?, ?, ?)
                """,
                    (
                        (operation.operation_id, source, context.principal_id.value)
                        for source in operation.source_event_ids
                    ),
                )
                db.executemany(
                    "INSERT INTO protected_reply_shared VALUES (?, ?, 1)",
                    (
                        (operation.operation_id, item)
                        for item in operation.shared_operation_ids
                    ),
                )
                db.executemany(
                    "INSERT INTO protected_reply_generic VALUES (?, ?, ?)",
                    (
                        (operation.operation_id, item, context.principal_id.value)
                        for item in operation.generic_operation_ids
                    ),
                )
                return PrivateReply(
                    operation, "pending", ResultState.PENDING, "held", None, True
                )
        except sqlite3.Error as error:
            raise self._store._translated_error(error) from None
        finally:
            if _connection is None:
                db.close()

    def persist_reply(
        self,
        route: TrustedActionContext,
        operation: ReplyOperation,
        decision: PolicyDecision,
        capabilities: CapabilityState,
    ) -> PrivateReply:
        """Revalidate a current bound policy grant and commit only its exact private draft."""
        if not isinstance(route, TrustedActionContext) or not isinstance(
            decision, PolicyDecision
        ):
            raise EventValidationError("trusted reply policy required")
        self._validate(route.principal, operation)
        validated = ActionPolicy().revalidate(
            decision, decision.proposal, route, capabilities
        )
        if (
            not validated.dispatchable
            or not isinstance(validated.proposal, ReplyProposal)
            or route.correlation_id != operation.correlation_id
        ):
            raise EventValidationError("private reply not authorized")
        content = validated.proposal.content
        EventStore._validate_payload_content(content)
        db = self._store._open()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                self._store._verify_current_schema(db)
                previous = self._read(db, route.principal, operation.operation_id)
                if previous is None:
                    raise EventValidationError("reply reservation unavailable")
                if previous.operation != operation:
                    raise EventIdentityConflictError(
                        "reply operation identity conflict"
                    )
                if previous.result_state is ResultState.PERSISTED:
                    if previous.content != content:
                        raise EventIdentityConflictError(
                            "reply result identity conflict"
                        )
                    return previous
                if (
                    db.execute(
                        "SELECT 1 FROM agent_reply_resolutions WHERE operation_id=?",
                        (operation.operation_id,),
                    ).fetchone()
                    is not None
                ):
                    raise EventValidationError("reply already resolved")
                db.execute(
                    "INSERT INTO agent_reply_payloads(operation_id, content) VALUES (?, ?)",
                    (operation.operation_id, content),
                )
                self._fault(FaultPoint.AFTER_REPLY_PAYLOAD_INSERT)
                db.execute(
                    """
                    UPDATE agent_reply_operations SET internal_decision='accepted_internal',
                        result_state='persisted' WHERE operation_id=?
                """,
                    (operation.operation_id,),
                )
            self._fault(FaultPoint.AFTER_REPLY_COMMIT)
            return PrivateReply(
                operation, "accepted_internal", ResultState.PERSISTED, "held", content
            )
        except sqlite3.Error as error:
            raise self._store._translated_error(error) from None
        finally:
            db.close()

    def read_reply(
        self, context: TrustedPrincipalContext, operation_id: str
    ) -> PrivateReply | None:
        """Read one scoped reservation/result; unavailable source/payload hides the reply."""
        EventStore._validate_context(context)
        EventStore._require_token("operation id", operation_id)
        db = self._store._open()
        try:
            with db:
                db.execute("BEGIN")
                self._store._verify_current_schema(db)
                return self._read(db, context, operation_id)
        except sqlite3.Error as error:
            raise self._store._translated_error(error) from None
        finally:
            db.close()

    def _read(
        self,
        db: sqlite3.Connection,
        context: TrustedPrincipalContext,
        operation_id: str,
    ) -> PrivateReply | None:
        row = db.execute(
            """
            SELECT * FROM agent_reply_operations
            WHERE operation_id=? AND audience_principal_id=?
        """,
            (operation_id, context.principal_id.value),
        ).fetchone()
        if row is None or row[12] != "active":
            return None
        sources = db.execute(
            """
            SELECT event_id, source_principal_id, retention_status FROM agent_reply_sources
            WHERE operation_id=? ORDER BY event_id
        """,
            (operation_id,),
        ).fetchall()
        if len(sources) != row[8] or any(
            source[1:] != (context.principal_id.value, "active") for source in sources
        ):
            return None
        shared = tuple(
            item[0]
            for item in db.execute(
                "SELECT shared_operation FROM protected_reply_shared WHERE operation_id=? ORDER BY shared_operation",
                (operation_id,),
            )
        )
        if len(shared) != row[13]:
            return None
        generic = tuple(
            item[0]
            for item in db.execute(
                "SELECT generic_operation_id FROM protected_reply_generic WHERE operation_id=? ORDER BY generic_operation_id",
                (operation_id,),
            )
        )
        if len(generic) != row[14]:
            return None
        op = ReplyOperation(
            row[0],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            tuple(source[0] for source in sources),
            shared,
            generic,
        )
        self._validate(context, op)
        try:
            if not self._sources_valid(db, context.principal_id.value, op):
                return None
        except ValueError:
            return None
        correlation = db.execute(
            "SELECT correlation_id FROM event_envelopes WHERE event_id=?",
            (op.initiating_event_id,),
        ).fetchone()
        if correlation != (op.correlation_id,):
            raise EventValidationError("corrupt reply correlation")
        payload = db.execute(
            "SELECT content, retention_status FROM agent_reply_payloads WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row[9:12] == ("pending", "pending", "held") and payload is None:
            return PrivateReply(op, "pending", ResultState.PENDING, "held", None)
        if row[9:12] != ("accepted_internal", "persisted", "held"):
            raise EventValidationError("corrupt reply lifecycle")
        if payload is None or payload[1] != "active":
            return None
        EventStore._validate_payload_content(payload[0])
        return PrivateReply(
            op, "accepted_internal", ResultState.PERSISTED, "held", payload[0]
        )

    def resolution(self, context: TrustedPrincipalContext, operation_id: str):
        """Return content-free terminal/release metadata only for live scoped sources."""
        return self.read_reply_and_resolution(context, operation_id)[1]

    def read_reply_and_resolution(
        self, context: TrustedPrincipalContext, operation_id: str
    ):
        """Read the live scoped reply and resolution from one SQLite snapshot."""
        EventStore._validate_context(context)
        EventStore._require_token("operation id", operation_id)
        db = self._store._open()
        try:
            with db:
                db.execute("BEGIN")
                self._store._verify_current_schema(db)
                reply = self._read(db, context, operation_id)
                if reply is None:
                    return None, None
                resolution = db.execute(
                    "SELECT outcome, release_id FROM agent_reply_resolutions WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                return reply, resolution
        finally:
            db.close()

    def resolve_without_reply(
        self, context: TrustedPrincipalContext, operation_id: str, outcome: str
    ) -> None:
        """Commit a bounded terminal decision without fabricating reply text."""
        if outcome not in {"no_action", "failed", "invalid", "unsupported", "skipped"}:
            raise EventValidationError("invalid conversation outcome")
        self._resolve(context, operation_id, outcome, None)

    def release_reply(
        self,
        route: TrustedActionContext,
        operation: ReplyOperation,
        decision: PolicyDecision,
        capabilities: CapabilityState,
        release_id: str,
    ) -> None:
        """Durably release an exact Russian draft for response; never claim receipt.

        Application callers must hold the current PrivacyReadGate fence. This
        storage boundary independently checks the route, current controls and
        every source in the same transaction as the release record.
        """
        from app.behavior.russian_gate import RussianOutputPolicy

        self._validate(route.principal, operation)
        EventStore._require_token("release id", release_id)
        if release_id != f"release:{operation.operation_id}":
            raise EventValidationError("private release identity mismatch")
        validated = ActionPolicy().revalidate(
            decision, decision.proposal, route, capabilities
        )
        if (
            not validated.dispatchable
            or not isinstance(validated.proposal, ReplyProposal)
            or capabilities.provider_spend_suspended
            or route.correlation_id != operation.correlation_id
            or RussianOutputPolicy(max_regenerations=0)
            .deliver(validated.proposal.content)
            .text
            != validated.proposal.content
        ):
            raise EventValidationError("private release not authorized")
        self._resolve(
            route.principal,
            operation.operation_id,
            "completed",
            release_id,
            operation=operation,
            content=validated.proposal.content,
        )

    def _resolve(
        self,
        context,
        operation_id,
        outcome,
        release_id,
        *,
        operation=None,
        content=None,
    ) -> None:
        EventStore._validate_context(context)
        EventStore._require_token("operation id", operation_id)
        db = self._store._open()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                self._store._verify_current_schema(db)
                reply = self._read(db, context, operation_id)
                if reply is None:
                    raise EventValidationError("reply reservation unavailable")
                if outcome == "completed":
                    if reply.operation != operation or reply.content != content:
                        raise EventIdentityConflictError("private release mismatch")
                elif reply.result_state is not ResultState.PENDING:
                    raise EventValidationError("reply already persisted")
                previous = db.execute(
                    "SELECT outcome, release_id FROM agent_reply_resolutions WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if previous is not None:
                    if previous != (outcome, release_id):
                        raise EventIdentityConflictError("reply resolution conflict")
                    return
                db.execute(
                    "INSERT INTO agent_reply_resolutions VALUES (?, ?, ?)",
                    (operation_id, outcome, release_id),
                )
        finally:
            db.close()

    def _fault(self, point: FaultPoint) -> None:
        if self._store._fault_injector is not None:
            self._store._fault_injector(point)
