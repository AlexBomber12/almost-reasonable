"""Durable deletion coordination across the non-atomic ledger/application boundary."""

from collections.abc import Callable
from typing import Protocol

from .ledger import PrivacyLedger
from .models import (
    AuditRecord,
    ContentCategory,
    DeletionDisposition,
    DeletionOutcome,
    DeletionRequest,
    ErasureReport,
    OperationOrigin,
    TrustedPrivacyContext,
)


class ErasureAdapter(Protocol):
    """Integration boundary implemented only after an application schema is reviewed."""

    def erase(self, deletion: DeletionRequest) -> ErasureReport: ...


class DeletionCoordinator:
    def __init__(self, ledger: PrivacyLedger, adapter: ErasureAdapter):
        self.ledger = ledger
        self.adapter = adapter

    def delete(
        self,
        context: TrustedPrivacyContext,
        request: DeletionRequest,
        *,
        after_pending: Callable[[DeletionRequest], None] | None = None,
    ) -> DeletionOutcome:
        existing = None
        try:
            existing = self.ledger.deletion(request.operation_id)
        except KeyError:
            pass
        if existing is not None:
            if existing.request != request:
                raise ValueError("deletion operation identity conflict")
            if existing.disposition in {
                DeletionDisposition.APPLIED,
                DeletionDisposition.INVALID_AUTHORIZATION,
                DeletionDisposition.NOT_ELIGIBLE,
            }:
                return DeletionOutcome(
                    request.operation_id, existing.disposition, existing.reason
                )

        if context.origin is not OperationOrigin.CONTROL or (
            context.actor_reference != request.actor_reference
            or context.authentication_event_id != request.authentication_event_id
        ):
            return self._terminal(
                request,
                DeletionDisposition.INVALID_AUTHORIZATION,
                "invalid_authentication",
            )
        if context.principal_id != request.principal_id:
            return self._terminal(
                request, DeletionDisposition.INVALID_AUTHORIZATION, "wrong_principal"
            )
        if request.content_category is ContentCategory.PUBLIC_ACTION:
            return self._terminal(
                request, DeletionDisposition.NOT_ELIGIBLE, "public_action_not_erasable"
            )

        if existing is None:
            self.ledger.insert_deletion(
                request, DeletionDisposition.PENDING, "awaiting_erasure"
            )
        if after_pending is not None:
            after_pending(request)
        return self._apply(request)

    def reconcile_pending(self) -> tuple[DeletionOutcome, ...]:
        records = self.ledger.deletions_for_reconciliation()
        return tuple(
            self._apply(record.request)
            for record in records
            if record.disposition
            in {
                DeletionDisposition.PENDING,
                DeletionDisposition.INCOMPLETE_BLOCKED,
            }
        )

    def _apply(self, request: DeletionRequest) -> DeletionOutcome:
        try:
            report = self.adapter.erase(request)
        except Exception:
            return self._finish(
                request, DeletionDisposition.INCOMPLETE_BLOCKED, "adapter_failure"
            )
        required = frozenset(ContentCategory.required_erasure_categories())
        complete = (
            report.payload_removed
            and report.tombstone_written
            and not report.blockers
            and required <= report.invalidated_categories
        )
        if not complete:
            return self._finish(
                request, DeletionDisposition.INCOMPLETE_BLOCKED, "erasure_incomplete"
            )
        return self._finish(request, DeletionDisposition.APPLIED, "erasure_applied")

    def _terminal(
        self, request: DeletionRequest, disposition: DeletionDisposition, reason: str
    ) -> DeletionOutcome:
        created = self.ledger.insert_deletion(request, disposition, reason)
        if not created:
            record = self.ledger.deletion(request.operation_id)
            return DeletionOutcome(
                request.operation_id, record.disposition, record.reason
            )
        self._audit(request, disposition, reason)
        return DeletionOutcome(request.operation_id, disposition, reason)

    def _finish(
        self, request: DeletionRequest, disposition: DeletionDisposition, reason: str
    ) -> DeletionOutcome:
        self.ledger.update_deletion(
            request.operation_id, disposition, reason, request.requested_at
        )
        self._audit(request, disposition, reason)
        return DeletionOutcome(request.operation_id, disposition, reason)

    def _audit(
        self, request: DeletionRequest, disposition: DeletionDisposition, reason: str
    ) -> None:
        self.ledger.record_audit(
            AuditRecord(
                request.correlation_id,
                request.actor_reference,
                "deletion",
                request.content_category.value,
                request.requested_at,
                f"{disposition.value}:{reason}",
                "content_free_tombstone"
                if disposition is DeletionDisposition.APPLIED
                else "payload_free",
            )
        )
