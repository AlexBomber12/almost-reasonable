"""Source-bound, erasable snapshot and proposal receipt for worker evolution."""

from dataclasses import dataclass
import json
import secrets

from app.cognition.generic_store import GenericMemoryStore, MAX_GENERIC_SOURCES
from app.cognition.retrieval import MemoryContextSelector
from app.cognition.worker_evolution import (
    EvolutionMemory,
    EvolutionModelRequest,
    EvolutionProposal,
    MAX_MEMORIES,
    parse_evolution_proposal,
)
from app.trusted.openai_worker_protocol import vars_evolution

from .extraction_audit import ExtractionAuditStore


@dataclass(frozen=True, slots=True)
class EvolutionSnapshot:
    selected: tuple[tuple[str, str, int], ...]
    visible: tuple[tuple[str, int], ...]
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvolutionAudit:
    status: str
    snapshot: EvolutionSnapshot | None
    proposal: EvolutionProposal | None
    request: EvolutionModelRequest | None = None


class EvolutionAuditStore(ExtractionAuditStore):
    """The existing IPC intent and source receipt retain one immutable snapshot."""

    def _decode(self, operation_id, principal_id, source_event_id):
        with self._events._open() as db:
            self._events._verify_current_schema(db)
            row = db.execute(
                "SELECT source_event_id,source_principal_id,proposal FROM private_extraction_receipts WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        if row[:2] != (source_event_id, principal_id):
            raise ValueError("evolution receipt conflict")
        try:
            if len(row[2].encode("utf-8")) > 131_072:
                raise ValueError
            value = json.loads(row[2])
            if (
                type(value) is not dict
                or set(value) != {"version", "snapshot", "proposal"}
                or value["version"] != "private-evolution-receipt-v1"
            ):
                raise ValueError
            raw = value["snapshot"]
            if type(raw) is not dict or set(raw) != {"selected", "visible", "sources"}:
                raise ValueError
            selected = tuple(
                (handle, item, revision) for handle, item, revision in raw["selected"]
            )
            visible = tuple((item, revision) for item, revision in raw["visible"])
            sources = tuple(raw["sources"])
            if (
                len(selected) > MAX_MEMORIES
                or len(visible) > 128
                or len(sources) > MAX_GENERIC_SOURCES
                or len(set(handle for handle, _, _ in selected)) != len(selected)
                or len(set((item, revision) for _, item, revision in selected))
                != len(selected)
                or len(set(visible)) != len(visible)
                or not set((item, revision) for _, item, revision in selected)
                <= set(visible)
                or len(set(sources)) != len(sources)
                or any(
                    type(handle) is not str
                    or type(item) is not str
                    or type(revision) is not int
                    or revision < 1
                    for handle, item, revision in selected
                )
                or any(
                    type(item) is not str or type(revision) is not int or revision < 1
                    for item, revision in visible
                )
                or any(type(source) is not str for source in sources)
            ):
                raise ValueError
            proposal = (
                None
                if value["proposal"] is None
                else parse_evolution_proposal(json.dumps(value["proposal"]))
            )
        except (TypeError, UnicodeError, ValueError):
            raise ValueError("invalid evolution receipt") from None
        return EvolutionSnapshot(selected, visible, sources), proposal

    def read(self, operation_id, principal_id, source_event_id, content):
        self._check_input(principal_id, source_event_id, content, required=False)
        with self._provider._event_store._open() as db:
            self._provider._event_store._verify_current_schema(db)
            row = db.execute(
                "SELECT binding,status FROM private_extraction_audit WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        if row[0] != self._binding(principal_id, source_event_id):
            raise ValueError("evolution identity conflict")
        if row[1] not in {"reserved", "received"}:
            self._delete_receipt(operation_id)
            return EvolutionAudit(row[1], None, None)
        receipt = self._decode(operation_id, principal_id, source_event_id)
        if receipt is None:
            self.finish(operation_id, "unavailable")
            return EvolutionAudit("unavailable", None, None)
        snapshot, proposal = receipt
        if row[1] == "received" and proposal is None:
            self.finish(operation_id, "unavailable")
            return EvolutionAudit("unavailable", None, None)
        return EvolutionAudit(
            "received" if proposal is not None else row[1], snapshot, proposal
        )

    def reserve(self, operation_id, context, source_event_id, content):
        principal_id = context.principal_id.value
        self._check_input(principal_id, source_event_id, content, required=True)
        existing = self.read(operation_id, principal_id, source_event_id, content)
        if existing is not None:
            return existing
        generic = GenericMemoryStore(self._events).current(context)
        words = set(MemoryContextSelector._words(content[:1024]))
        ranked = sorted(
            generic,
            key=lambda read: (
                len(words & set(MemoryContextSelector._words(read.view.statement))),
                read.decided_at,
                read.view.item_id,
            ),
            reverse=True,
        )
        selected = []
        memories = []
        sources = set()
        for read in ranked:
            if len(memories) == MAX_MEMORIES:
                break
            if (
                len(sources | set(read.source_event_ids) | {source_event_id})
                > MAX_GENERIC_SOURCES
            ):
                continue
            handle = "h" + secrets.token_hex(8)
            record = EvolutionMemory(
                handle,
                read.view.memory_kind.value,
                read.view.statement,
                read.view.confidence,
            )
            trial = EvolutionModelRequest(content, tuple((*memories, record)))
            try:
                trial.serialize()
            except ValueError:
                continue
            selected.append((handle, read.view.item_id, read.view.revision))
            memories.append(record)
            sources.update(read.source_event_ids)
        request = EvolutionModelRequest(content, tuple(memories))
        request.serialize()
        snapshot = EvolutionSnapshot(
            tuple(selected),
            tuple((read.view.item_id, read.view.revision) for read in generic),
            tuple(sorted(sources)),
        )
        binding = self._binding(principal_id, source_event_id)
        with self._provider._event_store._open() as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                self._provider._event_store._verify_current_schema(db)
                db.execute(
                    "INSERT INTO private_extraction_audit VALUES (?,?,?)",
                    (operation_id, binding, "reserved"),
                )
        receipt = json.dumps(
            {
                "version": "private-evolution-receipt-v1",
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
        with self._events._open() as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                self._events._verify_current_schema(db)
                db.execute(
                    "INSERT INTO private_extraction_receipts VALUES (?,?,?,?)",
                    (operation_id, source_event_id, principal_id, receipt),
                )
        return EvolutionAudit("reserved", snapshot, None, request)

    def receive(self, operation_id, principal_id, source_event_id, content, proposal):
        self._check_input(principal_id, source_event_id, content, required=True)
        parsed = parse_evolution_proposal(json.dumps(vars_evolution(proposal)))
        with self._events._open() as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                self._events._verify_current_schema(db)
                row = db.execute(
                    "SELECT proposal FROM private_extraction_receipts WHERE operation_id=? AND source_event_id=? AND source_principal_id=?",
                    (operation_id, source_event_id, principal_id),
                ).fetchone()
                if row is None:
                    raise ValueError("evolution receipt unavailable")
                value = json.loads(row[0])
                if value["proposal"] is not None:
                    raise ValueError("evolution receipt conflict")
                value["proposal"] = vars_evolution(parsed)
                db.execute(
                    "UPDATE private_extraction_receipts SET proposal=? WHERE operation_id=?",
                    (
                        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                        operation_id,
                    ),
                )
        with self._provider._event_store._open() as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if (
                    db.execute(
                        "UPDATE private_extraction_audit SET status='received' WHERE operation_id=? AND status='reserved'",
                        (operation_id,),
                    ).rowcount
                    != 1
                ):
                    raise ValueError("evolution receipt conflict")
