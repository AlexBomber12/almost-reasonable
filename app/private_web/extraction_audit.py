"""Application intent ledger and source-bound, erasable extraction receipt."""

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import json

from app.cognition.worker_extraction import (
    ExtractionProposal,
    parse_extraction_proposal,
)
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.trusted.openai_worker_protocol import vars_extraction


@dataclass(frozen=True, slots=True)
class ExtractionAudit:
    status: str
    proposal: ExtractionProposal | None
    created: bool = False


class ExtractionAuditStore:
    """IPC intent survives backups; proposal text follows source erasure."""

    def __init__(self, provider: ProviderStateStore, events: EventStore):
        if type(provider) is not ProviderStateStore or type(events) is not EventStore:
            raise TypeError("application stores required")
        self._provider = provider
        self._events = events

    @staticmethod
    def _binding(principal_id: str, source_event_id: str) -> str:
        return sha256(
            json.dumps([principal_id, source_event_id], ensure_ascii=False).encode(
                "utf-8"
            )
        ).hexdigest()

    def _check_input(
        self, principal_id: str, source_event_id: str, content: str, *, required: bool
    ) -> None:
        with closing(self._events._open()) as db:
            self._events._verify_current_schema(db)
            row = db.execute(
                """SELECT p.content FROM event_envelopes e
                JOIN private_payloads p ON p.payload_id=e.payload_id
                WHERE e.event_id=? AND e.audience_principal_id=?
                  AND e.event_type='participant_input'
                  AND p.retention_status='active'""",
                (source_event_id, principal_id),
            ).fetchone()
        if row is None:
            if required:
                raise ValueError("extraction source unavailable")
        elif row != (content,):
            raise ValueError("extraction identity conflict")

    def _delete_receipt(self, operation_id: str) -> None:
        with closing(self._events._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "DELETE FROM private_extraction_receipts WHERE operation_id=?",
                    (operation_id,),
                )

    def _receipt(
        self, operation_id: str, principal_id: str, source_event_id: str
    ) -> ExtractionProposal | None:
        with closing(self._events._open()) as db:
            self._events._verify_current_schema(db)
            row = db.execute(
                "SELECT source_event_id,source_principal_id,proposal FROM private_extraction_receipts WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        if row[:2] != (source_event_id, principal_id):
            raise ValueError("extraction receipt conflict")
        return parse_extraction_proposal(row[2])

    def reserve(
        self, operation_id: str, principal_id: str, source_event_id: str, content: str
    ) -> ExtractionAudit:
        self._check_input(principal_id, source_event_id, content, required=True)
        binding = self._binding(principal_id, source_event_id)
        with closing(self._provider._event_store._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                self._provider._event_store._verify_current_schema(db)
                row = db.execute(
                    "SELECT binding,status FROM private_extraction_audit WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    db.execute(
                        "INSERT INTO private_extraction_audit VALUES (?,?,?)",
                        (operation_id, binding, "reserved"),
                    )
                    return ExtractionAudit("reserved", None, True)
                if row[0] != binding:
                    raise ValueError("extraction identity conflict")
        if row[1] not in {"reserved", "received"}:
            self._delete_receipt(operation_id)
            return ExtractionAudit(row[1], None)
        proposal = self._receipt(operation_id, principal_id, source_event_id)
        return ExtractionAudit("received" if proposal is not None else row[1], proposal)

    def read(
        self, operation_id: str, principal_id: str, source_event_id: str, content: str
    ) -> ExtractionAudit | None:
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
            raise ValueError("extraction identity conflict")
        if row[1] not in {"reserved", "received"}:
            self._delete_receipt(operation_id)
            return ExtractionAudit(row[1], None)
        proposal = self._receipt(operation_id, principal_id, source_event_id)
        return ExtractionAudit(
            "received"
            if proposal is not None and row[1] in {"reserved", "received"}
            else row[1],
            proposal,
        )

    def receive(
        self,
        operation_id: str,
        principal_id: str,
        source_event_id: str,
        content: str,
        proposal: ExtractionProposal,
    ) -> None:
        receipt = json.dumps(
            vars_extraction(proposal), ensure_ascii=False, sort_keys=True
        )
        with closing(self._events._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                self._events._verify_current_schema(db)
                source = db.execute(
                    """SELECT p.content FROM event_envelopes e
                    JOIN private_payloads p ON p.payload_id=e.payload_id
                    WHERE e.event_id=? AND e.audience_principal_id=?
                      AND e.event_type='participant_input'
                      AND e.audience_type='private_participant'
                      AND e.provenance_type='participant_assertion'
                      AND e.verification_status='unverified'
                      AND p.audience_principal_id=e.audience_principal_id
                      AND p.provenance_type='participant_assertion'
                      AND p.verification_status='unverified'
                      AND p.retention_status='active'""",
                    (source_event_id, principal_id),
                ).fetchone()
                if source != (content,):
                    raise ValueError("extraction source unavailable")
                db.execute(
                    "INSERT INTO private_extraction_receipts VALUES (?,?,?,?)",
                    (operation_id, source_event_id, principal_id, receipt),
                )
        with closing(self._provider._event_store._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute(
                    "UPDATE private_extraction_audit SET status='received' WHERE operation_id=? AND status='reserved'",
                    (operation_id,),
                ).rowcount
                if changed != 1:
                    raise ValueError("extraction receipt conflict")

    def finish(self, operation_id: str, status: str) -> None:
        if status not in {
            "accepted",
            "no_change",
            "rejected",
            "unavailable",
            "uncertain",
        }:
            raise ValueError("invalid extraction outcome")
        with closing(self._provider._event_store._open()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute(
                    "UPDATE private_extraction_audit SET status=? WHERE operation_id=? AND status IN ('reserved','received')",
                    (status, operation_id),
                ).rowcount
                if changed != 1:
                    raise ValueError("extraction outcome conflict")
        self._delete_receipt(operation_id)
