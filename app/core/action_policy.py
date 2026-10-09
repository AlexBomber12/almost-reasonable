"""Deterministic mediation between untrusted proposals and trusted capabilities."""

from dataclasses import dataclass, field
from enum import StrEnum

from .agent_proposals import (
    AgentDecision,
    AgentProposal,
    NoActionProposal,
    ProposalValidation,
    PublicationProposal,
    ReplyProposal,
    ResearchProposal,
    UnsupportedProposal,
)
from .events import (
    AuthenticatedSourceReference,
    ParticipantId,
    TrustedPrincipalContext,
)


POLICY_VERSION = "action-policy-v1"
MAX_CONTROL_IDENTIFIER_UTF8_BYTES = 128


class PolicyOutcome(StrEnum):
    ACCEPTED_INTERNAL = "accepted_internal"
    REJECTED_BY_POLICY = "rejected_by_policy"
    SKIPPED_STOP = "skipped_stop"
    SKIPPED_SPEND = "skipped_spend"
    UNAVAILABLE_CAPABILITY = "unavailable_capability"
    INVALID_PROPOSAL = "invalid_proposal"


class PolicyReason(StrEnum):
    NO_ACTION_ACCEPTED = "no_action_accepted"
    PRIVATE_REPLY_DRAFT_ACCEPTED = "private_reply_draft_accepted"
    PUBLICATION_DRAFT_ACCEPTED = "publication_draft_accepted"
    INVALID_PROPOSAL_SCHEMA = "invalid_proposal_schema"
    DECISION_NOT_IMPLEMENTED = "decision_not_implemented"
    EMERGENCY_STOP_ACTIVE = "emergency_stop_active"
    PROVIDER_SPEND_SUSPENDED = "provider_spend_suspended"
    CAPABILITY_DISABLED = "capability_disabled"
    RESEARCH_CONTROLS_UNAVAILABLE = "research_controls_unavailable"
    REAL_EFFECTS_DISABLED = "real_effects_disabled"
    GRANT_MISMATCH = "grant_mismatch"
    TRUSTED_ROUTE_MISMATCH = "trusted_route_mismatch"


class CapabilityCategory(StrEnum):
    NONE = "none"
    PRIVATE_REPLY_DRAFT = "private_reply_draft"
    RESEARCH = "research"
    PUBLICATION_DRAFT = "publication_draft"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class TrustedActionContext:
    """Application-owned routing and correlation data, never model input."""

    correlation_id: str
    principal: TrustedPrincipalContext
    _route_binding: object = field(
        default_factory=object, init=False, repr=False, compare=False
    )


@dataclass(frozen=True, slots=True)
class CapabilityState:
    """Application-controlled operational state at one point in time."""

    version: str
    emergency_stop_active: bool
    provider_spend_suspended: bool
    private_reply_drafts_enabled: bool
    publication_drafts_enabled: bool
    simulation_only: bool


@dataclass(frozen=True, slots=True)
class PolicyEvidence:
    """Bounded evidence suitable for a future durable audit record."""

    correlation_id: str
    decision_type: AgentDecision | None
    policy_version: str
    outcome: PolicyOutcome
    reason: PolicyReason
    capability: CapabilityCategory
    operational_state_version: str

    def to_audit_record(self) -> dict[str, str | None]:
        """Return only the explicit non-sensitive audit allowlist."""

        return {
            "correlation_id": self.correlation_id,
            "decision_type": (
                self.decision_type.value if self.decision_type is not None else None
            ),
            "policy_version": self.policy_version,
            "outcome": self.outcome.value,
            "reason_code": self.reason.value,
            "capability_category": self.capability.value,
            "operational_state_version": self.operational_state_version,
        }


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Sanitized evidence plus an in-memory simulation-only grant."""

    evidence: PolicyEvidence
    _proposal: AgentProposal | None = field(default=None, repr=False, compare=False)
    _trusted_route_binding: object | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def proposal(self) -> AgentProposal | None:
        return self._proposal

    @property
    def dispatchable(self) -> bool:
        return (
            self.evidence.outcome is PolicyOutcome.ACCEPTED_INTERNAL
            and isinstance(self._proposal, (ReplyProposal, PublicationProposal))
            and self._trusted_route_binding is not None
        )


class ActionPolicy:
    """Stateless, default-deny policy for the Stage 0 simulation boundary."""

    def evaluate(
        self,
        validation: ProposalValidation,
        trusted: TrustedActionContext,
        capabilities: CapabilityState,
    ) -> PolicyDecision:
        _validate_inputs(validation, trusted, capabilities)
        proposal = validation.proposal
        if not validation.valid:
            return self._result(
                trusted,
                capabilities,
                None,
                PolicyOutcome.INVALID_PROPOSAL,
                PolicyReason.INVALID_PROPOSAL_SCHEMA,
                CapabilityCategory.NONE,
            )
        if isinstance(proposal, UnsupportedProposal):
            return self._result(
                trusted,
                capabilities,
                proposal,
                PolicyOutcome.UNAVAILABLE_CAPABILITY,
                PolicyReason.DECISION_NOT_IMPLEMENTED,
                CapabilityCategory.UNSUPPORTED,
            )
        if isinstance(proposal, NoActionProposal):
            return self._result(
                trusted,
                capabilities,
                proposal,
                PolicyOutcome.ACCEPTED_INTERNAL,
                PolicyReason.NO_ACTION_ACCEPTED,
                CapabilityCategory.NONE,
            )

        capability = _capability_for(proposal)
        if capabilities.emergency_stop_active:
            return self._result(
                trusted,
                capabilities,
                proposal,
                PolicyOutcome.SKIPPED_STOP,
                PolicyReason.EMERGENCY_STOP_ACTIVE,
                capability,
            )
        if isinstance(proposal, ResearchProposal):
            if capabilities.provider_spend_suspended:
                return self._result(
                    trusted,
                    capabilities,
                    proposal,
                    PolicyOutcome.SKIPPED_SPEND,
                    PolicyReason.PROVIDER_SPEND_SUSPENDED,
                    capability,
                )
            return self._result(
                trusted,
                capabilities,
                proposal,
                PolicyOutcome.UNAVAILABLE_CAPABILITY,
                PolicyReason.RESEARCH_CONTROLS_UNAVAILABLE,
                capability,
            )

        enabled = (
            capabilities.private_reply_drafts_enabled
            if isinstance(proposal, ReplyProposal)
            else capabilities.publication_drafts_enabled
        )
        if not enabled:
            return self._result(
                trusted,
                capabilities,
                proposal,
                PolicyOutcome.UNAVAILABLE_CAPABILITY,
                PolicyReason.CAPABILITY_DISABLED,
                capability,
            )
        if not capabilities.simulation_only:
            return self._result(
                trusted,
                capabilities,
                proposal,
                PolicyOutcome.REJECTED_BY_POLICY,
                PolicyReason.REAL_EFFECTS_DISABLED,
                capability,
            )
        reason = (
            PolicyReason.PRIVATE_REPLY_DRAFT_ACCEPTED
            if isinstance(proposal, ReplyProposal)
            else PolicyReason.PUBLICATION_DRAFT_ACCEPTED
        )
        return self._result(
            trusted,
            capabilities,
            proposal,
            PolicyOutcome.ACCEPTED_INTERNAL,
            reason,
            capability,
            grant=True,
        )

    def revalidate(
        self,
        previous: PolicyDecision,
        proposal: AgentProposal | None,
        trusted: TrustedActionContext,
        capabilities: CapabilityState,
    ) -> PolicyDecision:
        """Recheck trusted, time-sensitive state immediately before simulation."""

        if not isinstance(previous, PolicyDecision):
            raise TypeError("previous must be a PolicyDecision")
        _validate_trusted_context(trusted)
        _validate_capabilities(capabilities)
        if not previous.dispatchable or proposal is not previous._proposal:
            return self._result(
                trusted,
                capabilities,
                proposal,
                PolicyOutcome.REJECTED_BY_POLICY,
                PolicyReason.GRANT_MISMATCH,
                _capability_for_optional(proposal),
            )
        if previous._trusted_route_binding is not trusted._route_binding:
            return self._result(
                trusted,
                capabilities,
                proposal,
                PolicyOutcome.REJECTED_BY_POLICY,
                PolicyReason.TRUSTED_ROUTE_MISMATCH,
                _capability_for_optional(proposal),
            )
        return self.evaluate(ProposalValidation(proposal, None), trusted, capabilities)

    @staticmethod
    def _result(
        trusted: TrustedActionContext,
        capabilities: CapabilityState,
        proposal: AgentProposal | None,
        outcome: PolicyOutcome,
        reason: PolicyReason,
        capability: CapabilityCategory,
        *,
        grant: bool = False,
    ) -> PolicyDecision:
        evidence = PolicyEvidence(
            correlation_id=trusted.correlation_id,
            decision_type=proposal.decision if proposal is not None else None,
            policy_version=POLICY_VERSION,
            outcome=outcome,
            reason=reason,
            capability=capability,
            operational_state_version=capabilities.version,
        )
        trusted_route_binding = trusted._route_binding if grant else None
        return PolicyDecision(evidence, proposal, trusted_route_binding)


def _capability_for(proposal: AgentProposal) -> CapabilityCategory:
    if isinstance(proposal, ReplyProposal):
        return CapabilityCategory.PRIVATE_REPLY_DRAFT
    if isinstance(proposal, ResearchProposal):
        return CapabilityCategory.RESEARCH
    if isinstance(proposal, PublicationProposal):
        return CapabilityCategory.PUBLICATION_DRAFT
    if isinstance(proposal, UnsupportedProposal):
        return CapabilityCategory.UNSUPPORTED
    return CapabilityCategory.NONE


def _capability_for_optional(proposal: AgentProposal | None) -> CapabilityCategory:
    return CapabilityCategory.NONE if proposal is None else _capability_for(proposal)


def _validate_inputs(
    validation: ProposalValidation,
    trusted: TrustedActionContext,
    capabilities: CapabilityState,
) -> None:
    if not isinstance(validation, ProposalValidation):
        raise TypeError("validation must be a ProposalValidation")
    _validate_trusted_context(trusted)
    _validate_capabilities(capabilities)


def _validate_trusted_context(trusted: TrustedActionContext) -> None:
    if not isinstance(trusted, TrustedActionContext):
        raise TypeError("trusted must be a TrustedActionContext")
    _validate_identifier(trusted.correlation_id, "correlation_id")
    principal = trusted.principal
    if not isinstance(principal, TrustedPrincipalContext):
        raise TypeError("principal must be a TrustedPrincipalContext")
    if not isinstance(principal.principal_id, ParticipantId):
        raise TypeError("principal_id must be a ParticipantId")
    _validate_identifier(principal.principal_id.value, "principal_id")
    if not isinstance(principal.authenticated_source, AuthenticatedSourceReference):
        raise TypeError("authenticated_source must be trusted")
    _validate_identifier(principal.authenticated_source.value, "authenticated_source")
    if type(principal.is_owner) is not bool:
        raise TypeError("is_owner must be a boolean")


def _validate_capabilities(capabilities: CapabilityState) -> None:
    if not isinstance(capabilities, CapabilityState):
        raise TypeError("capabilities must be CapabilityState")
    _validate_identifier(capabilities.version, "capability state version")
    values = (
        capabilities.emergency_stop_active,
        capabilities.provider_spend_suspended,
        capabilities.private_reply_drafts_enabled,
        capabilities.publication_drafts_enabled,
        capabilities.simulation_only,
    )
    if any(type(value) is not bool for value in values):
        raise ValueError("capability state flags must be booleans")


def _validate_identifier(value: object, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} must be valid UTF-8") from error
    if size > MAX_CONTROL_IDENTIFIER_UTF8_BYTES:
        raise ValueError(f"{name} is too large")
