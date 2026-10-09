"""Closed, typed parsing for untrusted model-generated action proposals."""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar


MAX_PROPOSAL_TEXT_UTF8_BYTES = 4096


class AgentDecision(StrEnum):
    """The complete decision vocabulary from the behavioral contract."""

    REPLY = "reply"
    RESEARCH = "research"
    PROPOSE_PUBLICATION = "propose_publication"
    REMEMBER = "remember"
    REVISE_BELIEF = "revise_belief"
    REVISE_GOAL = "revise_goal"
    REVISE_MISSION = "revise_mission"
    DEFER = "defer"
    TERMINATE_CONVERSATION = "terminate_conversation"
    NO_ACTION = "no_action"


AGENT_DECISION_VOCABULARY = tuple(AgentDecision)


class NoActionReason(StrEnum):
    """Bounded reason categories for deliberate inactivity."""

    NOT_WARRANTED = "not_warranted"
    DUPLICATE = "duplicate"
    LOW_VALUE = "low_value"
    OTHER = "other"


class ProposalInvalidReason(StrEnum):
    """Non-sensitive parse failures that never echo model data."""

    INVALID_CONTAINER = "invalid_container"
    INVALID_DECISION = "invalid_decision"
    UNKNOWN_DECISION = "unknown_decision"
    UNKNOWN_FIELDS = "unknown_fields"
    INVALID_FIELD = "invalid_field"
    OVERSIZED_FIELD = "oversized_field"


@dataclass(frozen=True, slots=True)
class NoActionProposal:
    reason_category: NoActionReason
    decision: ClassVar[AgentDecision] = AgentDecision.NO_ACTION


@dataclass(frozen=True, slots=True)
class ReplyProposal:
    content: str
    decision: ClassVar[AgentDecision] = AgentDecision.REPLY


@dataclass(frozen=True, slots=True)
class ResearchProposal:
    brief: str
    decision: ClassVar[AgentDecision] = AgentDecision.RESEARCH


@dataclass(frozen=True, slots=True)
class PublicationProposal:
    draft: str
    decision: ClassVar[AgentDecision] = AgentDecision.PROPOSE_PUBLICATION


@dataclass(frozen=True, slots=True)
class UnsupportedProposal:
    """A contract decision whose payload and behavior are not implemented yet."""

    decision: AgentDecision


AgentProposal = (
    NoActionProposal
    | ReplyProposal
    | ResearchProposal
    | PublicationProposal
    | UnsupportedProposal
)


@dataclass(frozen=True, slots=True)
class ProposalValidation:
    """A fail-closed parse result without the rejected payload."""

    proposal: AgentProposal | None
    invalid_reason: ProposalInvalidReason | None

    @property
    def valid(self) -> bool:
        return (
            self.proposal is not None
            and self.invalid_reason is None
            and is_valid_agent_proposal(self.proposal)
        )


_SUPPORTED_FIELDS = {
    AgentDecision.NO_ACTION: frozenset(("decision", "reason_category")),
    AgentDecision.REPLY: frozenset(("decision", "content")),
    AgentDecision.RESEARCH: frozenset(("decision", "brief")),
    AgentDecision.PROPOSE_PUBLICATION: frozenset(("decision", "draft")),
}
_UNSUPPORTED_DECISIONS = frozenset(AgentDecision) - frozenset(_SUPPORTED_FIELDS)


def parse_agent_proposal(payload: object) -> ProposalValidation:
    """Parse one JSON-like object through exact purpose-specific allowlists."""

    if type(payload) is not dict:
        return _invalid(ProposalInvalidReason.INVALID_CONTAINER)
    decision_value = payload.get("decision")
    if not isinstance(decision_value, str):
        return _invalid(ProposalInvalidReason.INVALID_DECISION)
    try:
        decision = AgentDecision(decision_value)
    except ValueError:
        return _invalid(ProposalInvalidReason.UNKNOWN_DECISION)

    expected_fields = _SUPPORTED_FIELDS.get(decision, frozenset(("decision",)))
    if frozenset(payload) != expected_fields:
        return _invalid(ProposalInvalidReason.UNKNOWN_FIELDS)
    if decision in _UNSUPPORTED_DECISIONS:
        return ProposalValidation(UnsupportedProposal(decision), None)
    if decision is AgentDecision.NO_ACTION:
        try:
            reason = NoActionReason(payload["reason_category"])
        except (TypeError, ValueError):
            return _invalid(ProposalInvalidReason.INVALID_FIELD)
        return ProposalValidation(NoActionProposal(reason), None)

    field = {
        AgentDecision.REPLY: "content",
        AgentDecision.RESEARCH: "brief",
        AgentDecision.PROPOSE_PUBLICATION: "draft",
    }[decision]
    value = payload[field]
    invalid_reason = _validate_text(value)
    if invalid_reason is not None:
        return _invalid(invalid_reason)
    if decision is AgentDecision.REPLY:
        proposal: AgentProposal = ReplyProposal(value)
    elif decision is AgentDecision.RESEARCH:
        proposal = ResearchProposal(value)
    else:
        proposal = PublicationProposal(value)
    return ProposalValidation(proposal, None)


def is_valid_agent_proposal(proposal: object) -> bool:
    """Revalidate a typed proposal so direct construction cannot bypass parsing."""

    if type(proposal) is NoActionProposal:
        return type(proposal.reason_category) is NoActionReason
    if type(proposal) is ReplyProposal:
        return _validate_text(proposal.content) is None
    if type(proposal) is ResearchProposal:
        return _validate_text(proposal.brief) is None
    if type(proposal) is PublicationProposal:
        return _validate_text(proposal.draft) is None
    if type(proposal) is UnsupportedProposal:
        return (
            type(proposal.decision) is AgentDecision
            and proposal.decision in _UNSUPPORTED_DECISIONS
        )
    return False


def _validate_text(value: object) -> ProposalInvalidReason | None:
    if not isinstance(value, str) or not value:
        return ProposalInvalidReason.INVALID_FIELD
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return ProposalInvalidReason.INVALID_FIELD
    if size > MAX_PROPOSAL_TEXT_UTF8_BYTES:
        return ProposalInvalidReason.OVERSIZED_FIELD
    return None


def _invalid(reason: ProposalInvalidReason) -> ProposalValidation:
    return ProposalValidation(None, reason)
