"""Application-local binding between an authenticated input and public approval."""

from datetime import datetime
import hashlib
import os
import sqlite3

from app.core.provider_contract import CostEstimateStatus
from app.core.provider_spend import ProviderAttemptRecord
from app.core.public_research_authority import (
    ApplicationApproval,
    CONFIG,
    load_operator_approval,
)
from app.core.public_research_client import PublicResearchClient
from app.core.public_research_profile import ResearchProfile, load_research_profile
from app.trusted.openai_worker_protocol import WorkerProtocolError
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
        application_scope=None,
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
        self._profile = None
        self._application_scope = application_scope

    def install_profile(self, profile: ResearchProfile, principal):
        """Bind a reviewed catalog to one authenticated application scope."""
        if (
            type(profile) is not ResearchProfile
            or profile.role != "application"
            or self._grants
            or not enabled_now(profile, self._clock())
            or profile.application_scope != self._application_scope
            or profile.principal_id != principal.principal_id.value
            or profile.authenticated_source_ref != principal.authenticated_source.value
            or profile.peer_uid != os.geteuid()
            or load_research_profile(self._approval_path, role="application") != profile
        ):
            raise ValueError("invalid application research profile")
        self._profile = profile

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
        if self._profile is not None:
            self._profile = None
        self._grants.pop(operation_id, None)

    def permits(self, principal, operation, question):
        if self._profile is not None:
            from .runtime import WorkerConversationRuntime

            profile = self._profile
            try:
                if (
                    load_research_profile(self._approval_path, role="application")
                    != profile
                ):
                    return False
                db = self._store._open()
                try:
                    self._store._verify_current_schema(db)
                    row = db.execute(
                        "SELECT audience_principal_id, authenticated_source_ref FROM event_envelopes WHERE event_id=?",
                        (operation.initiating_event_id,),
                    ).fetchone()
                finally:
                    db.close()
            except (OSError, ValueError, TypeError, UnicodeError, sqlite3.Error):
                return False
            return (
                self.enabled
                and enabled_now(profile, self._clock())
                and profile.topic(question) is not None
                and profile.principal_id == principal.principal_id.value
                and profile.authenticated_source_ref
                == principal.authenticated_source.value
                and row == (profile.principal_id, profile.authenticated_source_ref)
                and os.geteuid() == profile.peer_uid
                and operation.operation_id
                == WorkerConversationRuntime._operation_id(
                    operation.initiating_event_id
                )
            )
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

    def execute(self, operation_id, choice=None):
        if self._profile is not None:
            try:
                current = load_research_profile(self._approval_path, role="application")
            except (OSError, ValueError, TypeError, UnicodeError):
                current = None
            if current != self._profile or not enabled_now(
                self._profile, self._clock()
            ):
                raise WorkerProtocolError("application profile changed before IPC")
            return self._client.execute(self._profile.request(operation_id, choice))
        return self._client.execute(self._grants[operation_id].request)

    def evidence_choice(self, model_identifier):
        """Accept old reply evidence only under its original reviewed profile."""
        if self._profile is None:
            return None
        prefix = "research-profile:" + self._profile.digest + ":"
        if type(model_identifier) is not str or not model_identifier.startswith(prefix):
            return None
        fingerprint = model_identifier.removeprefix(prefix)
        return next(
            (
                topic.topic_id
                for topic in self._profile.topics
                if hashlib.sha256(topic.topic_id.encode()).hexdigest()[:24]
                == fingerprint
            ),
            None,
        )

    def record(
        self, provider, operation, question, outcome, now, *, require_reservation=True
    ):
        model_identifier = CONFIG
        if self._profile is not None:
            topic = self._profile.topic(question)
            if topic is not None:
                fingerprint = hashlib.sha256(topic.topic_id.encode()).hexdigest()[:24]
                model_identifier = (
                    "research-profile:" + self._profile.digest + ":" + fingerprint
                )
        return provider.record_attempt(
            ProviderAttemptRecord(
                intent_id(operation.operation_id),
                operation.operation_id,
                "openai",
                model_identifier,
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
