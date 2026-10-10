"""Structured consent and revocation service."""

from datetime import datetime

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
    def __init__(self, ledger: PrivacyLedger):
        self.ledger = ledger

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
            self._audit(grant, "consent_grant", reason)
            return OperationResult(False, reason)
        created = self.ledger.insert_consent(grant)
        reason = "granted" if created else "already_granted"
        self._audit(grant, "consent_grant", reason)
        return OperationResult(created, reason)

    def authorize(
        self,
        *,
        principal_id: str,
        content_reference: str,
        audience: str,
        purpose: ConsentPurpose,
        used_at: datetime,
        correlation_id: str,
    ) -> ConsentUseResult:
        require_token(principal_id, "principal id")
        require_token(content_reference, "content reference")
        require_token(audience, "audience")
        require_token(correlation_id, "correlation id")
        require_utc(used_at, "use timestamp")
        if not isinstance(purpose, ConsentPurpose):
            raise ValueError("invalid consent purpose")
        match = self.ledger.matching_consent(
            principal_id, content_reference, audience, purpose
        )
        if match is None:
            return ConsentUseResult(False, "no_matching_consent")
        consent_id, granted_at, expires_at, revoked = match
        if revoked:
            return ConsentUseResult(False, "revoked", consent_id)
        if used_at < granted_at:
            return ConsentUseResult(False, "not_yet_valid", consent_id)
        if used_at >= expires_at:
            return ConsentUseResult(False, "expired", consent_id)
        return ConsentUseResult(True, "authorized", consent_id)

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

    def _audit(self, grant: ConsentGrant, action: str, result: str) -> None:
        self.ledger.record_audit(
            AuditRecord(
                grant.correlation_id,
                grant.actor_reference,
                action,
                "consent",
                grant.granted_at,
                result,
                "payload_free",
            )
        )
