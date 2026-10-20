"""Durable private narrative operation receipt and exact model-visible binding."""

from contextlib import closing
from dataclasses import dataclass
import json
import secrets

from app.cognition.narrative_store import MAX_EPISODES, MAX_REVISIONS, NarrativeStore
from app.cognition.worker_narrative import (
    NarrativeModelRequest,
    NarrativeProjection,
    NarrativeProposal,
    parse_narrative_proposal,
)
from app.trusted.openai_worker_protocol import narrative_data
from .extraction_audit import ExtractionAuditStore


@dataclass(frozen=True, slots=True)
class NarrativeAudit:
    status: str
    heads: tuple[tuple[str, int], ...] = ()
    selected: tuple[tuple[str, str], ...] = ()
    sources: tuple[str, ...] = ()
    proposal: NarrativeProposal | None = None
    request: NarrativeModelRequest | None = None


class NarrativeAuditStore(ExtractionAuditStore):
    def status(self, operation_id, principal_id, source_event_id):
        with closing(self._provider._event_store._open()) as db:
            self._provider._event_store._verify_current_schema(db)
            row = db.execute(
                "SELECT binding,status FROM private_extraction_audit WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        if row[0] != self._binding(principal_id, source_event_id):
            raise ValueError("narrative operation conflict")
        if row[1] == "unavailable":
            receipt = self._receipt(operation_id, principal_id, source_event_id)
            if receipt is not None and receipt.proposal is not None:
                return "withheld"
        return row[1]

    def finish(self, operation_id, status):
        if status != "unavailable":
            return super().finish(operation_id, status)
        with closing(self._provider._event_store._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute(
                    "UPDATE private_extraction_audit SET status='unavailable' WHERE operation_id=? AND status IN ('reserved','received')",
                    (operation_id,),
                ).rowcount
                if changed != 1:
                    raise ValueError("extraction outcome conflict")
        with closing(self._events._open()) as db:
            row = db.execute(
                "SELECT principal_id,source_event_id FROM private_narrative_receipts WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None or self._receipt(operation_id, *row).proposal is None:
            self._delete_receipt(operation_id)

    def _delete_receipt(self, operation_id):
        with closing(self._events._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "DELETE FROM private_narrative_receipts WHERE operation_id=?",
                    (operation_id,),
                )

    def _receipt(self, operation_id, principal_id, source_event_id):
        with closing(self._events._open()) as db:
            self._events._verify_current_schema(db)
            row = db.execute(
                "SELECT source_event_id,principal_id,receipt FROM private_narrative_receipts WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        if row[:2] != (source_event_id, principal_id):
            raise ValueError("narrative receipt conflict")
        try:
            value = json.loads(row[2])
            if (
                type(value) is not dict
                or set(value) != {"heads", "selected", "sources", "proposal"}
                or len(row[2].encode("utf-8")) > 65536
            ):
                raise ValueError
            heads = tuple((item, revision) for item, revision in value["heads"])
            selected = tuple(
                (handle, operation) for handle, operation in value["selected"]
            )
            sources = tuple(value["sources"])
            if (
                len(heads) > 16
                or len(selected) > 16
                or not 1 <= len(sources) <= 100
                or tuple(sorted(set(heads))) != heads
                or tuple(sorted(set(sources))) != sources
                or len({handle for handle, _ in selected}) != len(selected)
                or any(
                    type(item) is not str or type(revision) is not int
                    for item, revision in heads
                )
                or any(
                    type(handle) is not str or type(operation) is not str
                    for handle, operation in selected
                )
            ):
                raise ValueError
            proposal = (
                None
                if value["proposal"] is None
                else parse_narrative_proposal(json.dumps(value["proposal"]))
            )
        except (TypeError, ValueError, KeyError, UnicodeError):
            raise ValueError("invalid narrative receipt") from None
        return NarrativeAudit(
            "received" if proposal else "reserved", heads, selected, sources, proposal
        )

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
            raise ValueError("narrative operation conflict")
        if row[1] not in {"reserved", "received"}:
            if (
                row[1] != "unavailable"
                or self.status(operation_id, principal_id, source_event_id)
                != "withheld"
            ):
                self._delete_receipt(operation_id)
            return NarrativeAudit(row[1])
        receipt = self._receipt(operation_id, principal_id, source_event_id)
        if receipt is None or (row[1] == "received" and receipt.proposal is None):
            self.finish(operation_id, "unavailable")
            return NarrativeAudit("unavailable")
        return receipt

    def reserve(
        self,
        operation_id,
        principal,
        source_event_id,
        content,
        reply_operation,
        context,
    ):
        principal_id = principal.principal_id.value
        self._check_input(principal_id, source_event_id, content, required=True)
        existing = self.read(operation_id, principal_id, source_event_id, content)
        if existing is not None:
            return existing
        with closing(self._events._open()) as db:
            db.execute("BEGIN")
            self._events._verify_current_schema(db)
            current = NarrativeStore(self._events).current(principal, _connection=db)
            heads = tuple(
                sorted((episode.item_id, episode.revision) for episode in current)
            )
            indexed = {episode.operation_id: episode for episode in current}
            projected = context.projected_narrative_operation_ids
            if (
                len(projected) != len(context.episodes)
                or any(operation not in indexed for operation in projected)
                or reply_operation.initiating_event_id != source_event_id
                or reply_operation.source_event_ids
                != tuple(sorted(set(reply_operation.source_event_ids)))
            ):
                raise ValueError("narrative context unavailable")
            selected = tuple(
                ("h" + secrets.token_hex(8), operation)
                for operation in projected
                if indexed[operation].revision < MAX_REVISIONS
            )
            episodes = tuple(
                NarrativeProjection(handle, indexed[operation].text)
                for handle, operation in selected
            )
            request = NarrativeModelRequest(
                context.conversation.conversation,
                episodes,
                create_allowed=len(current) < MAX_EPISODES,
            )
            request.serialize()
            sources = reply_operation.source_event_ids
            receipt = json.dumps(
                {
                    "heads": heads,
                    "selected": selected,
                    "sources": sources,
                    "proposal": None,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
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
                    "INSERT INTO private_narrative_receipts VALUES (?,?,?,?)",
                    (operation_id, source_event_id, principal_id, receipt),
                )
        return NarrativeAudit("reserved", heads, selected, sources, None, request)

    def receive(self, operation_id, principal_id, source_event_id, content, proposal):
        self._check_input(principal_id, source_event_id, content, required=True)
        parsed = parse_narrative_proposal(json.dumps(narrative_data(proposal)))
        with closing(self._events._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT receipt FROM private_narrative_receipts WHERE operation_id=? AND source_event_id=? AND principal_id=?",
                    (operation_id, source_event_id, principal_id),
                ).fetchone()
                if row is None:
                    raise ValueError("narrative receipt unavailable")
                value = json.loads(row[0])
                if value["proposal"] is not None:
                    raise ValueError("narrative receipt conflict")
                value["proposal"] = narrative_data(parsed)
                db.execute(
                    "UPDATE private_narrative_receipts SET receipt=? WHERE operation_id=?",
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
                    raise ValueError("narrative receipt conflict")
