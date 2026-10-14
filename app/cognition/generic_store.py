"""Durable, participant-private generic memory revisions and material graph."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
import sqlite3

from app.core.events import TrustedPrincipalContext, VerificationStatus
from app.core.generic_graph import validate_generic_graph
from app.core.store import EventStore

from .contracts import (
    GenericMemoryProposal,
    GenericMemoryView,
    GenericReviewState,
    MemoryKind,
)


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_KINDS = frozenset(
    {
        MemoryKind.UNRESOLVED_QUESTION,
        MemoryKind.CANDIDATE_USEFUL_MEMORY,
        MemoryKind.CONTRADICTION_COUNTEREVIDENCE,
    }
)
MAX_GENERIC_SOURCES = 100
MAX_GENERIC_PARENTS = 16
MAX_GENERIC_REVISIONS = 32


class GenericMemoryError(ValueError):
    """A proposal or stored private graph cannot be used safely."""


class GenericMemoryConflict(GenericMemoryError):
    """An immutable operation identity has a different binding."""


class GenericMemoryStale(GenericMemoryError):
    """A referenced head changed before the operation committed."""


@dataclass(frozen=True, slots=True)
class GenericMemoryRead:
    view: GenericMemoryView
    operation_id: str
    decided_at: datetime
    reason: str
    review_at: datetime
    model_identifier: str
    prompt_version: str
    core_self_version: str
    source_event_ids: tuple[str, ...]


def _token(value: object) -> bool:
    return type(value) is str and _TOKEN.fullmatch(value) is not None


def _text(value: object) -> bool:
    if type(value) is not str or not value.strip():
        return False
    try:
        return len(value.encode("utf-8")) <= 4096
    except UnicodeEncodeError:
        return False


class GenericMemoryStore:
    def __init__(self, events: EventStore):
        self._events = events

    def operation_exists(
        self, context: TrustedPrincipalContext, operation_id: str
    ) -> bool:
        EventStore._validate_context(context)
        if not _token(operation_id):
            raise GenericMemoryError("invalid_generic_operation")
        with closing(self._events._open()) as db:
            self._events._verify_current_schema(db)
            validate_generic_graph(db)
            return (
                db.execute(
                    """SELECT 1 FROM generic_memory_operations o
                JOIN generic_memory_items i USING(item_id)
                WHERE o.operation_id=? AND i.audience_principal_id=?""",
                    (operation_id, context.principal_id.value),
                ).fetchone()
                is not None
            )

    def current(
        self,
        context: TrustedPrincipalContext,
        *,
        _connection: sqlite3.Connection | None = None,
    ) -> tuple[GenericMemoryRead, ...]:
        EventStore._validate_context(context)
        db = _connection if _connection is not None else self._events._open()
        try:
            if _connection is None:
                db.execute("BEGIN")
            self._events._verify_current_schema(db)
            validate_generic_graph(db)
            rows = db.execute(
                """SELECT o.operation_id,o.item_id,o.revision,p.kind,p.statement,
                    p.confidence,p.verification,o.decided_at,p.reason,p.review_at,
                    o.model_identifier,o.prompt_version,o.core_self_version
                FROM generic_memory_items i JOIN generic_memory_operations o
                    ON o.item_id=i.item_id AND o.revision=i.current_revision
                JOIN generic_memory_payloads p USING(operation_id)
                WHERE i.audience_principal_id=? AND o.status='active'
                ORDER BY o.item_id""",
                (context.principal_id.value,),
            ).fetchall()
            reads = []
            for row in rows:
                sources = db.execute(
                    "SELECT event_id,role FROM generic_memory_sources WHERE operation_id=? ORDER BY event_id,role",
                    (row[0],),
                ).fetchall()
                reads.append(
                    GenericMemoryRead(
                        GenericMemoryView(
                            row[1],
                            row[2],
                            MemoryKind(row[3]),
                            row[4],
                            row[5],
                            VerificationStatus(row[6]),
                            len(
                                {
                                    event
                                    for event, role in sources
                                    if role in {"supporting", "initiating"}
                                }
                            ),
                            len(
                                {
                                    event
                                    for event, role in sources
                                    if role == "counterevidence"
                                }
                            ),
                        ),
                        row[0],
                        datetime.fromisoformat(row[7]),
                        row[8],
                        datetime.fromisoformat(row[9]),
                        row[10],
                        row[11],
                        row[12],
                        tuple(sorted({event for event, _role in sources})),
                    )
                )
            return tuple(reads)
        except (sqlite3.Error, ValueError, TypeError) as error:
            raise GenericMemoryError("generic_memory_unavailable") from error
        finally:
            if _connection is None:
                db.close()

    def commit(
        self,
        context: TrustedPrincipalContext,
        proposal: GenericMemoryProposal,
        *,
        model_identifier: str,
        prompt_version: str,
        core_self_version: str,
        decided_at: datetime,
    ) -> tuple[str, int]:
        """Atomically compare the exact private graph and persist one revision."""
        EventStore._validate_context(context)
        if type(proposal) is not GenericMemoryProposal:
            raise GenericMemoryError("invalid_generic_proposal")
        c = proposal.context
        if (
            proposal.memory_kind not in _KINDS
            or not _token(proposal.item_id)
            or not _token(c.operation_id)
            or not _token(c.source_event_id)
            or not _text(proposal.statement)
            or not _text(c.reason)
            or type(proposal.confidence) is not float
            or not 0 <= proposal.confidence <= 1
            or proposal.verification is not VerificationStatus.UNVERIFIED
            or proposal.review_state is not GenericReviewState.PENDING_REVIEW
            or type(c.supporting_event_ids) is not tuple
            or type(c.counterevidence_event_ids) is not tuple
            or (
                c.expected_revision is not None and type(c.expected_revision) is not int
            )
            or type(proposal.parent_revisions) is not tuple
            or len(proposal.parent_revisions) > MAX_GENERIC_PARENTS
            or any(
                not _token(value)
                for value in (model_identifier, prompt_version, core_self_version)
            )
        ):
            raise GenericMemoryError("invalid_generic_proposal")
        parents = proposal.parent_revisions
        if (
            any(
                type(p) is not tuple
                or len(p) != 2
                or not _token(p[0])
                or type(p[1]) is not int
                or p[1] < 1
                for p in parents
            )
            or len(set(parents)) != len(parents)
            or proposal.item_id in {p[0] for p in parents}
        ):
            raise GenericMemoryError("invalid_generic_parents")
        direct = {(c.source_event_id, "initiating")}
        direct.update((event, "supporting") for event in c.supporting_event_ids)
        direct.update(
            (event, "counterevidence") for event in c.counterevidence_event_ids
        )
        if len(direct) > MAX_GENERIC_SOURCES:
            raise GenericMemoryError("generic_source_bound")
        binding = json.dumps(
            {
                "item": proposal.item_id,
                "expected": c.expected_revision,
                "source": c.source_event_id,
                "support": c.supporting_event_ids,
                "counter": c.counterevidence_event_ids,
                "parents": parents,
                "kind": proposal.memory_kind.value,
                "statement": proposal.statement,
                "confidence": proposal.confidence,
                "verification": proposal.verification.value,
                "reason": c.reason,
                "proposed": c.proposed_at.isoformat(),
                "review": c.review_at.isoformat(),
                "review_state": proposal.review_state.value,
                "model": model_identifier,
                "prompt": prompt_version,
                "core": core_self_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with closing(self._events._open()) as db:
            try:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    self._events._verify_current_schema(db)
                    validate_generic_graph(db)
                    prior = db.execute(
                        "SELECT item_id,revision,status FROM generic_memory_operations WHERE operation_id=?",
                        (c.operation_id,),
                    ).fetchone()
                    if prior is not None:
                        payload = db.execute(
                            "SELECT binding FROM generic_memory_payloads WHERE operation_id=?",
                            (c.operation_id,),
                        ).fetchone()
                        if prior[2] != "active" or payload is None:
                            raise GenericMemoryConflict("invalidated_generic_identity")
                        if prior[0] != proposal.item_id or payload[0] != binding:
                            raise GenericMemoryConflict("generic_operation_conflict")
                        return prior[0], prior[1]
                    item = db.execute(
                        "SELECT audience_principal_id,current_revision FROM generic_memory_items WHERE item_id=?",
                        (proposal.item_id,),
                    ).fetchone()
                    if item is None:
                        if c.expected_revision is not None:
                            raise GenericMemoryStale("generic_revision_stale")
                        revision = 1
                    elif item[0] != context.principal_id.value:
                        raise GenericMemoryConflict("generic_item_conflict")
                    elif c.expected_revision != item[1]:
                        raise GenericMemoryStale("generic_revision_stale")
                    else:
                        revision = item[1] + 1
                    if revision > MAX_GENERIC_REVISIONS:
                        raise GenericMemoryError("generic_revision_bound")
                    source_roles = set(direct)
                    parent_operations = []
                    for parent_id, parent_revision in parents:
                        parent = db.execute(
                            """SELECT o.operation_id FROM generic_memory_items i
                            JOIN generic_memory_operations o ON o.item_id=i.item_id
                                AND o.revision=i.current_revision
                            JOIN generic_memory_payloads p USING(operation_id)
                            WHERE i.item_id=? AND i.audience_principal_id=?
                              AND o.revision=? AND o.status='active'""",
                            (parent_id, context.principal_id.value, parent_revision),
                        ).fetchone()
                        if parent is None:
                            raise GenericMemoryStale("generic_parent_stale")
                        parent_operations.append(parent[0])
                    ancestors = parent_operations[:]
                    if item is not None:
                        ancestors.extend(
                            row[0]
                            for row in db.execute(
                                "SELECT operation_id FROM generic_memory_operations WHERE item_id=?",
                                (proposal.item_id,),
                            )
                        )
                    for operation_id in ancestors:
                        source_roles.update(
                            db.execute(
                                "SELECT event_id,role FROM generic_memory_sources WHERE operation_id=?",
                                (operation_id,),
                            )
                        )
                    if len(source_roles) > MAX_GENERIC_SOURCES:
                        raise GenericMemoryError("generic_source_bound")
                    graph_digest = hashlib.sha256(
                        json.dumps(
                            [sorted(source_roles), sorted(parent_operations)],
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                    for event_id, _role in source_roles:
                        valid = db.execute(
                            """SELECT 1 FROM event_envelopes e JOIN private_payloads p
                                ON p.payload_id=e.payload_id
                            JOIN protected_event_taints t ON t.event_id=e.event_id
                            WHERE e.event_id=? AND e.audience_principal_id=?
                              AND e.audience_type='private_participant'
                              AND e.event_type='participant_input'
                              AND e.channel='private_chat'
                              AND e.provenance_type='participant_assertion'
                              AND e.verification_status='unverified'
                              AND p.audience_principal_id=e.audience_principal_id
                              AND p.audience_type=e.audience_type
                              AND p.provenance_type=e.provenance_type
                              AND p.verification_status=e.verification_status
                              AND t.source_event_id=e.event_id
                              AND t.source_principal_id=e.audience_principal_id
                              AND t.source_provenance_type=e.provenance_type
                              AND p.retention_status='active'""",
                            (event_id, context.principal_id.value),
                        ).fetchone()
                        if valid is None:
                            raise GenericMemoryError("generic_source_unavailable")
                    if item is None:
                        db.execute(
                            "INSERT INTO generic_memory_items VALUES (?,?,?)",
                            (proposal.item_id, context.principal_id.value, revision),
                        )
                    else:
                        db.execute(
                            "UPDATE generic_memory_items SET current_revision=? WHERE item_id=?",
                            (revision, proposal.item_id),
                        )
                    db.execute(
                        "INSERT INTO generic_memory_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            c.operation_id,
                            proposal.item_id,
                            revision,
                            c.expected_revision,
                            len(source_roles),
                            len(parent_operations),
                            "active",
                            decided_at.isoformat(),
                            model_identifier,
                            prompt_version,
                            core_self_version,
                            graph_digest,
                        ),
                    )
                    db.execute(
                        "INSERT INTO generic_memory_payloads VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            c.operation_id,
                            proposal.memory_kind.value,
                            proposal.statement,
                            proposal.confidence,
                            proposal.verification.value,
                            c.reason,
                            c.review_at.isoformat(),
                            proposal.review_state.value,
                            binding,
                        ),
                    )
                    db.executemany(
                        "INSERT INTO generic_memory_sources VALUES (?,?,?,?,?)",
                        (
                            (
                                c.operation_id,
                                event,
                                role,
                                context.principal_id.value,
                                "active",
                            )
                            for event, role in sorted(source_roles)
                        ),
                    )
                    db.executemany(
                        "INSERT INTO generic_memory_parents VALUES (?,?)",
                        ((c.operation_id, parent) for parent in parent_operations),
                    )
                    validate_generic_graph(db)
                    return proposal.item_id, revision
            except sqlite3.Error as error:
                raise GenericMemoryError("generic_memory_store_failure") from error
