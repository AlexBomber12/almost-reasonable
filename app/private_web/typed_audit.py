"""Erasable, source-bound selection receipt for private typed evolution."""

from contextlib import closing
from dataclasses import dataclass
import json
import secrets

from app.cognition.read_store import CognitionReadStore
from app.cognition.retrieval import MemoryContextSelector
from app.cognition.worker_typed import (
    TypedMemory,
    TypedModelRequest,
    TypedProposal,
    _HANDLE,
    parse_typed_proposal,
)
from app.core.self_state import BeliefCandidate, GoalCandidate, InterestCandidate
from app.core.self_state_store import MAX_EVIDENCE_PER_REVISION, SelfStateStore
from app.trusted.openai_worker_protocol import vars_typed

from .extraction_audit import ExtractionAuditStore


@dataclass(frozen=True, slots=True)
class TypedSnapshot:
    selected: tuple[tuple[str, str, int, str], ...]
    visible: tuple[tuple[str, int], ...]
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TypedAudit:
    status: str
    snapshot: TypedSnapshot | None
    proposal: TypedProposal | None
    request: TypedModelRequest | None = None


def _kind_fields(candidate):
    if isinstance(candidate, InterestCandidate):
        return "durable_interest", {
            "topic": candidate.topic,
            "strength": candidate.strength,
            "status": candidate.status.value,
            "reason_for_interest": candidate.reason_for_interest,
        }
    if isinstance(candidate, BeliefCandidate):
        return "belief_or_hypothesis", {
            "proposition": candidate.proposition,
            "stance": candidate.stance.value,
            "confidence": candidate.confidence,
            "status": candidate.status.value,
            "alternatives": list(candidate.alternatives),
        }
    if isinstance(candidate, GoalCandidate):
        return "goal_or_intention", {
            "description": candidate.description,
            "rationale": candidate.rationale,
            "priority": candidate.priority,
            "status": candidate.status.value,
            "constraints": list(candidate.constraints),
            "review_condition": candidate.review_condition,
        }
    raise ValueError("invalid typed head")


class TypedAuditStore(ExtractionAuditStore):
    """Persist model-visible heads and every source before provider contact."""

    def _decode(self, operation_id, principal_id, source_event_id):
        with closing(self._events._open()) as db:
            self._events._verify_current_schema(db)
            row = db.execute(
                "SELECT source_event_id,source_principal_id,proposal FROM private_extraction_receipts WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        if row[:2] != (source_event_id, principal_id):
            raise ValueError("typed receipt conflict")
        try:
            if len(row[2].encode("utf-8")) > 131_072:
                raise ValueError
            value = json.loads(row[2])
            if (
                type(value) is not dict
                or set(value) != {"version", "snapshot", "proposal"}
                or value["version"] != "private-typed-receipt-v1"
            ):
                raise ValueError
            raw = value["snapshot"]
            if type(raw) is not dict or set(raw) != {"selected", "visible", "sources"}:
                raise ValueError
            selected = tuple(tuple(row) for row in raw["selected"])
            visible = tuple(tuple(row) for row in raw["visible"])
            sources = tuple(raw["sources"])
            if (
                len(selected) > 8
                or len(sources) > MAX_EVIDENCE_PER_REVISION
                or len(set(row[0] for row in selected)) != len(selected)
                or len(set(row[1:3] for row in selected)) != len(selected)
                or len(set(visible)) != len(visible)
                or len(set(sources)) != len(sources)
                or not set(row[1:3] for row in selected) <= set(visible)
                or any(
                    len(row) != 4
                    or type(row[0]) is not str
                    or _HANDLE.fullmatch(row[0]) is None
                    or type(row[1]) is not str
                    or type(row[2]) is not int
                    or row[2] < 1
                    or row[3]
                    not in {
                        "durable_interest",
                        "belief_or_hypothesis",
                        "goal_or_intention",
                    }
                    for row in selected
                )
                or any(
                    len(row) != 2
                    or type(row[0]) is not str
                    or type(row[1]) is not int
                    or row[1] < 1
                    for row in visible
                )
                or any(type(source) is not str for source in sources)
            ):
                raise ValueError
            proposal = (
                None
                if value["proposal"] is None
                else parse_typed_proposal(json.dumps(value["proposal"]))
            )
        except (IndexError, KeyError, TypeError, UnicodeError, ValueError):
            raise ValueError("invalid typed receipt") from None
        expected_sources = set()
        role_row_count = 2
        with closing(self._events._open()) as db:
            self._events._verify_current_schema(db)
            for _handle, item_id, revision, kind in selected:
                row = db.execute(
                    """SELECT i.state_type,p.retention_status FROM state_items i
                       JOIN state_revisions r USING(item_key)
                       JOIN state_revision_payloads p USING(revision_key)
                       WHERE i.scope='private_candidate' AND i.audience_principal_id=?
                         AND i.item_id=? AND r.revision_number=?""",
                    (principal_id, item_id, revision),
                ).fetchone()
                if row != (
                    {
                        "durable_interest": "interest",
                        "belief_or_hypothesis": "belief",
                        "goal_or_intention": "goal",
                    }[kind],
                    "active",
                ):
                    raise ValueError("invalid typed receipt")
                roles = db.execute(
                    """SELECT p.event_id,p.evidence_role,p.retention_status
                       FROM state_items i JOIN state_revisions r USING(item_key)
                       JOIN protected_state_provenance p USING(revision_key)
                       WHERE i.scope='private_candidate' AND i.audience_principal_id=?
                         AND i.item_id=? AND r.revision_number<=?""",
                    (principal_id, item_id, revision),
                ).fetchall()
                if not roles or any(role[2] != "active" for role in roles):
                    raise ValueError("invalid typed receipt")
                expected_sources.update(role[0] for role in roles)
                role_row_count += len(roles)
        if (
            expected_sources != set(sources)
            or role_row_count > MAX_EVIDENCE_PER_REVISION
        ):
            raise ValueError("invalid typed receipt")
        return TypedSnapshot(selected, visible, sources), proposal

    def read(self, operation_id, principal_id, source_event_id, content):
        self._check_input(principal_id, source_event_id, content, required=False)
        with closing(self._provider._event_store._open()) as db:
            self._provider._event_store._verify_current_schema(db)
            row = db.execute(
                "SELECT binding,status FROM private_extraction_audit WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        if row[0] != self._binding(principal_id, source_event_id):
            raise ValueError("typed identity conflict")
        if row[1] not in {"reserved", "received"}:
            self._delete_receipt(operation_id)
            return TypedAudit(row[1], None, None)
        receipt = self._decode(operation_id, principal_id, source_event_id)
        if receipt is None or (row[1] == "received" and receipt[1] is None):
            self.finish(operation_id, "unavailable")
            return TypedAudit("unavailable", None, None)
        snapshot, proposal = receipt
        return TypedAudit(
            "received" if proposal is not None else row[1], snapshot, proposal
        )

    def reserve(self, operation_id, context, source_event_id, content):
        principal_id = context.principal_id.value
        self._check_input(principal_id, source_event_id, content, required=True)
        existing = self.read(operation_id, principal_id, source_event_id, content)
        if existing is not None:
            return existing
        words = set(MemoryContextSelector._words(content[:1024]))
        with closing(self._events._open()) as db:
            with db:
                db.execute("BEGIN")
                reads = CognitionReadStore(
                    self._events, SelfStateStore(self._events)
                ).retrieve_all_private_candidate_reads(context, _connection=db)
                ranked = sorted(
                    reads,
                    key=lambda read: (
                        len(
                            words
                            & set(
                                MemoryContextSelector._words(
                                    " ".join(
                                        str(value)
                                        for value in _kind_fields(read.candidate)[
                                            1
                                        ].values()
                                    )
                                )
                            )
                        ),
                        read.decided_at,
                        read.candidate.item_id,
                    ),
                    reverse=True,
                )
                selected = []
                memories = []
                sources = set()
                role_row_count = 2
                for read in ranked:
                    if len(memories) == 8:
                        break
                    candidate = read.candidate
                    rows = tuple(
                        db.execute(
                            """SELECT p.event_id,p.evidence_role FROM state_items i
                           JOIN state_revisions r USING(item_key)
                           JOIN protected_state_provenance p USING(revision_key)
                           WHERE i.scope='private_candidate' AND i.audience_principal_id=?
                             AND i.item_id=? AND r.revision_number<=?
                             AND p.retention_status='active'""",
                            (principal_id, candidate.item_id, candidate.revision),
                        )
                    )
                    if (
                        not rows
                        or role_row_count + len(rows) > MAX_EVIDENCE_PER_REVISION
                    ):
                        continue
                    kind, fields = _kind_fields(candidate)
                    handle = "h" + secrets.token_hex(8)
                    record = TypedMemory(handle, kind, fields)
                    trial = TypedModelRequest(content, tuple((*memories, record)))
                    try:
                        trial.serialize()
                    except ValueError:
                        continue
                    selected.append(
                        (handle, candidate.item_id, candidate.revision, kind)
                    )
                    memories.append(record)
                    sources.update(event_id for event_id, _role in rows)
                    role_row_count += len(rows)
                snapshot = TypedSnapshot(
                    tuple(selected),
                    tuple(
                        (read.candidate.item_id, read.candidate.revision)
                        for read in reads
                    ),
                    tuple(sorted(sources)),
                )
        request = TypedModelRequest(content, tuple(memories))
        request.serialize()
        receipt = json.dumps(
            {
                "version": "private-typed-receipt-v1",
                "snapshot": {
                    "selected": snapshot.selected,
                    "visible": snapshot.visible,
                    "sources": snapshot.sources,
                },
                "proposal": None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(receipt.encode("utf-8")) > 131_072:
            return None
        with closing(self._provider._event_store._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO private_extraction_audit VALUES (?,?,?)",
                    (
                        operation_id,
                        self._binding(principal_id, source_event_id),
                        "reserved",
                    ),
                )
        with closing(self._events._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO private_extraction_receipts VALUES (?,?,?,?)",
                    (operation_id, source_event_id, principal_id, receipt),
                )
        return TypedAudit("reserved", snapshot, None, request)

    def receive(self, operation_id, principal_id, source_event_id, content, proposal):
        self._check_input(principal_id, source_event_id, content, required=True)
        parsed = parse_typed_proposal(json.dumps(vars_typed(proposal)))
        with closing(self._events._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT proposal FROM private_extraction_receipts WHERE operation_id=? AND source_event_id=? AND source_principal_id=?",
                    (operation_id, source_event_id, principal_id),
                ).fetchone()
                if row is None:
                    raise ValueError("typed receipt unavailable")
                value = json.loads(row[0])
                if value["proposal"] is not None:
                    raise ValueError("typed receipt conflict")
                value["proposal"] = vars_typed(parsed)
                db.execute(
                    "UPDATE private_extraction_receipts SET proposal=? WHERE operation_id=?",
                    (
                        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                        operation_id,
                    ),
                )
        with closing(self._provider._event_store._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if (
                    db.execute(
                        "UPDATE private_extraction_audit SET status='received' WHERE operation_id=? AND status='reserved'",
                        (operation_id,),
                    ).rowcount
                    != 1
                ):
                    raise ValueError("typed receipt conflict")
