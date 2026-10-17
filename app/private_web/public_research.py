"""Application-local binding between an authenticated input and public approval."""

from datetime import datetime
import hashlib
import os

from app.core.provider_contract import CostEstimateStatus
from app.core.provider_spend import ProviderAttemptRecord
from app.core.public_research_authority import (
    ApplicationApproval,
    CONFIG,
    load_operator_approval,
)
from app.core.public_research_client import PublicResearchClient
from app.research.brief import ResearchScreeningContext

from .research_bridge import intent_id


class PublicResearchBridge:
    """The private binding stays in this application and never enters worker IPC."""

    def __init__(
        self,
        store,
        gate,
        client,
        *,
        enabled=False,
        clock,
        screening=ResearchScreeningContext(),
        approval_path,
    ):
        if type(client) is not PublicResearchClient or gate._store is not store:
            raise TypeError("invalid public research dependencies")
        if (
            type(enabled) is not bool
            or type(screening) is not ResearchScreeningContext
            or approval_path is None
        ):
            raise ValueError("invalid public research configuration")
        self._store, self._gate, self._client = store, gate, client
        self.enabled, self._clock, self._screening = enabled, clock, screening
        self._grants = {}
        self._approval_path = approval_path

    def install_approval(self, approval: ApplicationApproval, principal):
        """Trusted local provisioning after the exact authenticated input exists."""
        from .runtime import WorkerConversationRuntime

        if type(approval) is not ApplicationApproval or not enabled_now(
            approval, self._clock()
        ):
            raise ValueError("invalid application approval")
        if (
            approval.principal_id != principal.principal_id.value
            or approval.request.operation_id
            != WorkerConversationRuntime._operation_id(approval.event_id)
        ):
            raise ValueError("approval does not bind this input")
        if approval.authenticated_source_ref != principal.authenticated_source.value:
            raise ValueError("approval does not bind authenticated source")
        if os.geteuid() != approval.peer_uid:
            raise ValueError("approval does not bind this application peer")
        events = self._gate.retrieve_private_events(principal)
        event = next(
            (item for item in events if item.event_id == approval.event_id), None
        )
        if (
            event is None
            or hashlib.sha256(event.content.encode()).hexdigest()
            != approval.input_sha256
        ):
            raise ValueError("approved authenticated input unavailable")

        def source_matches():
            db = self._store._open()
            try:
                self._store._verify_current_schema(db)
                row = db.execute(
                    "SELECT authenticated_source_ref FROM event_envelopes WHERE event_id=?",
                    (approval.event_id,),
                ).fetchone()
                return row is not None and row[0] == approval.authenticated_source_ref
            finally:
                db.close()

        if not self._gate.authorized(source_matches):
            raise ValueError("approved source identity unavailable")
        if load_operator_approval(self._approval_path, role="application") != approval:
            raise ValueError("operator approval file changed")
        self._grants[approval.request.operation_id] = approval

    def revoke(self, operation_id: str):
        self._grants.pop(operation_id, None)

    def permits(self, principal, operation, question):
        grant = self._grants.get(operation.operation_id)
        try:
            if load_operator_approval(self._approval_path, role="application") != grant:
                return False
        except (OSError, ValueError, TypeError, UnicodeError):
            return False
        return (
            self.enabled
            and type(grant) is ApplicationApproval
            and enabled_now(grant, self._clock())
            and grant.principal_id == principal.principal_id.value
            and grant.event_id == operation.initiating_event_id
            and grant.request.operation_id == operation.operation_id
            and grant.request.question == question
            and os.geteuid() == grant.peer_uid
        )

    def execute(self, operation_id):
        return self._client.execute(self._grants[operation_id].request)

    @staticmethod
    def record(
        provider, operation, _question, outcome, now, *, require_reservation=True
    ):
        return provider.record_attempt(
            ProviderAttemptRecord(
                intent_id(operation.operation_id),
                operation.operation_id,
                "openai",
                CONFIG,
                CONFIG,
                CONFIG,
                operation.core_self_version,
                None,
                now,
                0,
                None,
                None,
                None,
                None,
                outcome,
                0,
                None,
                None,
                None,
                CostEstimateStatus.USAGE_UNAVAILABLE,
                None,
            ),
            require_reservation=require_reservation,
        )


def enabled_now(approval, now: datetime):
    return now.tzinfo is not None and approval.issued_at <= now < approval.expires_at
