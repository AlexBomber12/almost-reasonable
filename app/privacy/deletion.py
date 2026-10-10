"""Durable deletion coordination across the non-atomic ledger/application boundary."""

from collections.abc import Callable
from datetime import UTC, datetime
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
    require_utc,
)


class ErasureAdapter(Protocol):
    """Integration boundary implemented only after an application schema is reviewed."""

    def erase(self, deletion: DeletionRequest) -> ErasureReport: ...


class DeletionCoordinator:
    def __init__(
        self,
        ledger: PrivacyLedger,
        adapter: ErasureAdapter,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.ledger = ledger
        self.adapter = adapter
        self.clock = clock or (lambda: datetime.now(UTC))

    def delete(
        self,
        context: TrustedPrivacyContext,
        request: DeletionRequest,
        *,
        after_pending: Callable[[DeletionRequest], None] | None = None,
    ) -> DeletionOutcome:
        if context.origin is not OperationOrigin.CONTROL or (
            context.actor_reference != request.actor_reference
            or context.authentication_event_id != request.authentication_event_id
        ):
            return self._reject(context, request, "invalid_authentication")
        if context.principal_id != request.principal_id:
            return self._reject(context, request, "wrong_principal")

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
                DeletionDisposition.NOT_ELIGIBLE,
            }:
                return DeletionOutcome(
                    request.operation_id, existing.disposition, existing.reason
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
        if not isinstance(report, ErasureReport):
            return self._finish(
                request,
                DeletionDisposition.INCOMPLETE_BLOCKED,
                "invalid_erasure_report",
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
        return DeletionOutcome(request.operation_id, disposition, reason)

    def _reject(
        self,
        context: TrustedPrivacyContext,
        request: DeletionRequest,
        reason: str,
    ) -> DeletionOutcome:
        occurred_at = self.clock()
        require_utc(occurred_at, "deletion rejection timestamp")
        self.ledger.record_audit(
            AuditRecord(
                request.correlation_id,
                context.actor_reference,
                "deletion_rejected",
                request.content_category.value,
                occurred_at,
                f"{DeletionDisposition.INVALID_AUTHORIZATION.value}:{reason}",
                "payload_free",
            )
        )
        return DeletionOutcome(
            request.operation_id, DeletionDisposition.INVALID_AUTHORIZATION, reason
        )

    def _finish(
        self, request: DeletionRequest, disposition: DeletionDisposition, reason: str
    ) -> DeletionOutcome:
        completed_at = self.clock()
        require_utc(completed_at, "deletion transition timestamp")
        updated = self.ledger.update_deletion(
            request, disposition, reason, completed_at
        )
        if not updated:
            record = self.ledger.deletion(request.operation_id)
            return DeletionOutcome(
                request.operation_id, record.disposition, record.reason
            )
        return DeletionOutcome(request.operation_id, disposition, reason)
