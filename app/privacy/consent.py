"""Structured consent and revocation service."""

from collections.abc import Callable
from datetime import UTC, datetime

from .ledger import PrivacyLedger
from .models import (
    AuditRecord,
    ConsentGrant,
    ConsentPurpose,
    ConsentUseResult,
    OperationOrigin,
    OperationResult,
    TrustedPrivacyContext,
    audit_correlation,
    require_token,
    require_utc,
)


class ConsentService:
    def __init__(
        self,
        ledger: PrivacyLedger,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.ledger = ledger
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _authenticated(
        context: TrustedPrivacyContext, grant: ConsentGrant
    ) -> str | None:
        if context.origin is not OperationOrigin.CONTROL:
            return "invalid_authentication"
        if context.principal_id != grant.principal_id:
            return "invalid_authorization"
        if (
            context.actor_reference != grant.actor_reference
            or context.authentication_event_id != grant.authentication_event_id
        ):
            return "invalid_authentication"
        return None

    def grant(
        self, context: TrustedPrivacyContext, grant: ConsentGrant
    ) -> OperationResult:
        reason = self._authenticated(context, grant)
        if reason is not None:
            self._audit(
                grant,
                "consent_grant",
                reason,
                actor_reference=context.actor_reference,
            )
            return OperationResult(False, reason)
        created = self.ledger.insert_consent(grant)
        reason = "granted" if created else "already_granted"
        return OperationResult(created, reason)

    def authorize(
        self,
        context: TrustedPrivacyContext,
        *,
        principal_id: str,
        content_reference: str,
        audience: str,
        purpose: ConsentPurpose,
        correlation_id: str,
        dispatch: Callable[[], None],
    ) -> ConsentUseResult:
        if context.origin is not OperationOrigin.CONTROL:
            correlation_id = audit_correlation(
                correlation_id, context.authentication_event_id
            )
            result = ConsentUseResult(False, "invalid_authentication")
        else:
            require_token(correlation_id, "correlation id")
            require_token(principal_id, "principal id")
            require_token(content_reference, "content reference")
            require_token(audience, "audience")
            if not isinstance(purpose, ConsentPurpose):
                raise ValueError("invalid consent purpose")
            if context.principal_id != principal_id:
                result = ConsentUseResult(False, "invalid_authorization")
            else:
                return self.ledger.authorize_consent_use(
                    principal_id=principal_id,
                    content_reference=content_reference,
                    audience=audience,
                    purpose=purpose,
                    correlation_id=correlation_id,
                    actor_reference=context.actor_reference,
                    authentication_event_id=context.authentication_event_id,
                    clock=self._clock,
                    dispatch=dispatch,
                )
        used_at = self._trusted_time()
        self.ledger.record_audit(
            AuditRecord(
                correlation_id,
                context.actor_reference,
                "consent_use",
                "consent",
                used_at,
                result.reason,
                "payload_free",
            )
        )
        return result

    def revoke(
        self,
        context: TrustedPrivacyContext,
        *,
        consent_id: str,
        operation_id: str,
        revoked_at: datetime,
        correlation_id: str,
    ) -> OperationResult:
        if context.origin is not OperationOrigin.CONTROL:
            correlation_id = audit_correlation(
                correlation_id, context.authentication_event_id
            )
            reason = "invalid_authentication"
        else:
            require_token(correlation_id, "correlation id")
            require_token(consent_id, "consent id")
            require_token(operation_id, "operation id")
            require_utc(revoked_at, "revocation timestamp")
            principal = self.ledger.consent_principal(consent_id)
            if principal != context.principal_id:
                reason = "invalid_authorization"
            else:
                created = self.ledger.insert_revocation(
                    consent_id=consent_id,
                    operation_id=operation_id,
                    principal_id=context.principal_id,
                    actor_reference=context.actor_reference,
                    authentication_event_id=context.authentication_event_id,
                    revoked_at=revoked_at,
                    correlation_id=correlation_id,
                )
                reason = "revoked" if created else "already_revoked"
                return OperationResult(created, reason)
        self.ledger.record_audit(
            AuditRecord(
                correlation_id,
                context.actor_reference,
                "consent_revocation",
                "consent",
                self._trusted_time(),
                reason,
                "payload_free",
            )
        )
        return OperationResult(False, reason)

    def _audit(
        self,
        grant: ConsentGrant,
        action: str,
        result: str,
        *,
        actor_reference: str,
    ) -> None:
        self.ledger.record_audit(
            AuditRecord(
                grant.correlation_id,
                actor_reference,
                action,
                "consent",
                self._trusted_time(),
                result,
                "payload_free",
            )
        )

    def _trusted_time(self) -> datetime:
        occurred_at = self._clock()
        require_utc(occurred_at, "audit timestamp")
        return occurred_at
