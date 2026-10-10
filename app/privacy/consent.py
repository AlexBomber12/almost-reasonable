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
    ) -> ConsentUseResult:
        require_token(principal_id, "principal id")
        require_token(content_reference, "content reference")
        require_token(audience, "audience")
        require_token(correlation_id, "correlation id")
        used_at = self._clock()
        require_utc(used_at, "use timestamp")
        if not isinstance(purpose, ConsentPurpose):
            raise ValueError("invalid consent purpose")
        if context.origin is not OperationOrigin.CONTROL:
            result = ConsentUseResult(False, "invalid_authentication")
        elif context.principal_id != principal_id:
            result = ConsentUseResult(False, "invalid_authorization")
        else:
            match = self.ledger.matching_consent(
                principal_id, content_reference, audience, purpose
            )
            if match is None:
                result = ConsentUseResult(False, "no_matching_consent")
            else:
                consent_id, granted_at, expires_at, revoked_at = match
                if used_at < granted_at:
                    result = ConsentUseResult(False, "not_yet_valid", consent_id)
                elif revoked_at is not None and used_at >= revoked_at:
                    result = ConsentUseResult(False, "revoked", consent_id)
                elif used_at >= expires_at:
                    result = ConsentUseResult(False, "expired", consent_id)
                else:
                    result = ConsentUseResult(True, "authorized", consent_id)
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
        for value, name in (
            (consent_id, "consent id"),
            (operation_id, "operation id"),
            (correlation_id, "correlation id"),
        ):
            require_token(value, name)
        require_utc(revoked_at, "revocation timestamp")
        principal = self.ledger.consent_principal(consent_id)
        if context.origin is not OperationOrigin.CONTROL:
            reason = "invalid_authentication"
        elif principal != context.principal_id:
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
                revoked_at,
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
                grant.granted_at,
                result,
                "payload_free",
            )
        )
