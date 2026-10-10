"""Typed, bounded deliberation over recorded or otherwise injected model output."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from app.core.agent_proposals import (
    AgentDecision,
    AgentProposal,
    ProposalInvalidReason,
    parse_agent_proposal,
)
from app.core.core_self import CoreSelf, initial_core_self
from app.core.self_state import GoalStatus


BEHAVIOR_CONTRACT_VERSION = "behavior-deliberation-v1"
MAX_BEHAVIOR_TEXT_UTF8_BYTES = 4096
MAX_CONTEXT_ITEMS = 64


class TriggerType(StrEnum):
    PRIVATE_MESSAGE = "private_message"
    PUBLIC_MESSAGE = "public_message"
    AUTONOMOUS_WAKEUP = "autonomous_wakeup"
    MAINTENANCE = "maintenance"
    MISSION_CONFIRMATION = "mission_confirmation"


class PriorResultCategory(StrEnum):
    NONE = "none"
    SUCCESS = "success"
    NO_ACTION = "no_action"
    TRANSIENT_FAILURE = "transient_failure"
    POLICY_REJECTION = "policy_rejection"


class DeliberationOutcome(StrEnum):
    PROPOSED = "proposed"
    NO_ACTION = "no_action"
    INVALID_PROPOSAL = "invalid_proposal"
    UNAVAILABLE_CAPABILITY = "unavailable_capability"
    SKIPPED_STOP = "skipped_stop"
    SKIPPED_SPEND = "skipped_spend"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True, slots=True)
class BehaviorCapabilities:
    version: str
    emergency_stop_active: bool
    provider_spend_suspended: bool
    replies_available: bool
    research_available: bool
    publication_proposals_available: bool

    @classmethod
    def all_simulated(cls, version: str) -> "BehaviorCapabilities":
        return cls(version, False, False, True, True, True)


@dataclass(frozen=True, slots=True)
class DeliberationBudget:
    max_steps: int
    max_model_calls: int
    max_execution_seconds: float


@dataclass(frozen=True, slots=True)
class DeliberationInput:
    correlation_id: str
    authorized_context: tuple[str, ...]
    core_self: CoreSelf
    permitted_mutable_state: tuple[str, ...]
    current_goals: tuple[str, ...]
    capabilities: BehaviorCapabilities
    trigger: TriggerType
    budget: DeliberationBudget
    now: datetime
    deadline: datetime
    prior_result: PriorResultCategory
    channel_supports_defer: bool


@dataclass(frozen=True, slots=True)
class BehaviorIntent:
    """Typed integration intent for decisions not yet implemented by core policy."""

    decision: AgentDecision
    parameters: tuple[tuple[str, object], ...]


BehaviorProposal = AgentProposal | BehaviorIntent


@dataclass(frozen=True, slots=True)
class BehaviorProposalValidation:
    proposal: BehaviorProposal | None
    invalid_reason: ProposalInvalidReason | None

    @property
    def valid(self) -> bool:
        return self.proposal is not None and self.invalid_reason is None


@dataclass(frozen=True, slots=True)
class DeliberationResult:
    outcome: DeliberationOutcome
    proposal: BehaviorProposal | None
    invalid_reason: ProposalInvalidReason | None
    model_calls: int
    steps: int


class BehaviorModel(Protocol):
    def complete(self, request: Mapping[str, object]) -> object: ...


@dataclass(slots=True)
class RecordedBehaviorModel:
    """Deterministic fake: it cannot contact a provider or execute a tool."""

    responses: tuple[object, ...]
    requests: list[dict[str, object]] = field(default_factory=list)
    _position: int = 0

    def complete(self, request: Mapping[str, object]) -> object:
        if self._position >= len(self.responses):
            raise ValueError("recorded model responses exhausted")
        self.requests.append(dict(request))
        response = self.responses[self._position]
        self._position += 1
        return response


_INTENT_FIELDS: dict[AgentDecision, frozenset[str]] = {
    AgentDecision.REMEMBER: frozenset(("decision", "statement")),
    AgentDecision.REVISE_BELIEF: frozenset(
        (
            "decision",
            "proposition",
            "rationale",
            "evidence_ids",
            "counterevidence_ids",
            "confidence",
        )
    ),
    AgentDecision.REVISE_GOAL: frozenset(
        ("decision", "goal_id", "status", "rationale")
    ),
    AgentDecision.REVISE_MISSION: frozenset(
        ("decision", "proposed_mission", "supporting_reasons", "counterevidence")
    ),
    AgentDecision.DEFER: frozenset(("decision", "resume_condition")),
    AgentDecision.TERMINATE_CONVERSATION: frozenset(
        ("decision", "scope", "cooldown_seconds")
    ),
}


def parse_behavior_proposal(payload: object) -> BehaviorProposalValidation:
    """Parse the complete shared decision vocabulary through closed schemas."""

    core_result = parse_agent_proposal(payload)
    if core_result.valid and core_result.proposal is not None:
        if core_result.proposal.decision not in _INTENT_FIELDS:
            return BehaviorProposalValidation(core_result.proposal, None)
    if type(payload) is not dict:
        return _invalid(ProposalInvalidReason.INVALID_CONTAINER)
    raw_decision = payload.get("decision")
    try:
        decision = (
            AgentDecision(raw_decision) if isinstance(raw_decision, str) else None
        )
    except ValueError:
        decision = None
    if decision is None:
        return _invalid(ProposalInvalidReason.UNKNOWN_DECISION)
    expected = _INTENT_FIELDS.get(decision)
    if expected is None or frozenset(payload) != expected:
        return _invalid(ProposalInvalidReason.UNKNOWN_FIELDS)

    parameters = _parse_intent_parameters(decision, payload)
    if parameters is None:
        return _invalid(ProposalInvalidReason.INVALID_FIELD)
    return BehaviorProposalValidation(BehaviorIntent(decision, parameters), None)


def _parse_intent_parameters(
    decision: AgentDecision, payload: dict[object, object]
) -> tuple[tuple[str, object], ...] | None:
    if decision is AgentDecision.REMEMBER:
        return _one_text(payload, "statement")
    if decision is AgentDecision.REVISE_BELIEF:
        confidence = payload["confidence"]
        if type(confidence) not in (int, float) or not 0.0 <= float(confidence) <= 1.0:
            return None
        values = _texts_and_lists(
            payload,
            ("proposition", "rationale"),
            ("evidence_ids", "counterevidence_ids"),
            allow_empty_lists=True,
        )
        return None if values is None else (*values, ("confidence", float(confidence)))
    if decision is AgentDecision.REVISE_GOAL:
        values = _texts_and_lists(payload, ("goal_id", "rationale"), ())
        status = payload["status"]
        if values is None or not isinstance(status, str):
            return None
        try:
            GoalStatus(status)
        except ValueError:
            return None
        return (values[0], ("status", status), values[1])
    if decision is AgentDecision.REVISE_MISSION:
        return _texts_and_lists(
            payload,
            ("proposed_mission",),
            ("supporting_reasons", "counterevidence"),
        )
    if decision is AgentDecision.DEFER:
        return _one_text(payload, "resume_condition")
    scope = payload["scope"]
    cooldown = payload["cooldown_seconds"]
    if scope not in ("conversation", "principal", "channel"):
        return None
    if type(cooldown) is not int or cooldown < 0 or cooldown > 2_592_000:
        return None
    return (("scope", scope), ("cooldown_seconds", cooldown))


def _one_text(
    payload: dict[object, object], key: str
) -> tuple[tuple[str, object], ...] | None:
    value = payload[key]
    return ((key, value),) if _valid_text(value) else None


def _texts_and_lists(
    payload: dict[object, object],
    text_keys: tuple[str, ...],
    list_keys: tuple[str, ...],
    *,
    allow_empty_lists: bool = False,
) -> tuple[tuple[str, object], ...] | None:
    result: list[tuple[str, object]] = []
    for key in text_keys:
        value = payload[key]
        if not _valid_text(value):
            return None
        result.append((key, value))
    for key in list_keys:
        value = payload[key]
        if (
            type(value) is not list
            or (not value and not allow_empty_lists)
            or len(value) > MAX_CONTEXT_ITEMS
            or any(not _valid_text(item) for item in value)
        ):
            return None
        result.append((key, tuple(value)))
    return tuple(result)


def _valid_text(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_BEHAVIOR_TEXT_UTF8_BYTES
    except UnicodeEncodeError:
        return False


def _invalid(reason: ProposalInvalidReason) -> BehaviorProposalValidation:
    return BehaviorProposalValidation(None, reason)


class DeliberationEngine:
    """One-call decision engine that returns proposals and never effects."""

    def __init__(self, model: BehaviorModel):
        self._model = model

    def deliberate(self, value: DeliberationInput) -> DeliberationResult:
        if not isinstance(value, DeliberationInput):
            raise TypeError("value must be a DeliberationInput")
        _validate_input(value)
        if value.capabilities.emergency_stop_active:
            return _result(DeliberationOutcome.SKIPPED_STOP)
        if value.capabilities.provider_spend_suspended:
            return _result(DeliberationOutcome.SKIPPED_SPEND)
        if (
            value.budget.max_steps < 1
            or value.budget.max_model_calls < 1
            or value.budget.max_execution_seconds <= 0
            or value.deadline <= value.now
        ):
            return _result(DeliberationOutcome.BUDGET_EXHAUSTED)

        parsed = parse_behavior_proposal(self._model.complete(_build_request(value)))
        if not parsed.valid or parsed.proposal is None:
            return DeliberationResult(
                DeliberationOutcome.INVALID_PROPOSAL,
                None,
                parsed.invalid_reason,
                1,
                1,
            )
        proposal = parsed.proposal
        decision = proposal.decision
        unavailable = (
            (
                decision is AgentDecision.REPLY
                and not value.capabilities.replies_available
            )
            or (
                decision is AgentDecision.RESEARCH
                and not value.capabilities.research_available
            )
            or (
                decision is AgentDecision.PROPOSE_PUBLICATION
                and not value.capabilities.publication_proposals_available
            )
            or (decision is AgentDecision.DEFER and not value.channel_supports_defer)
        )
        if unavailable:
            return DeliberationResult(
                DeliberationOutcome.UNAVAILABLE_CAPABILITY, proposal, None, 1, 1
            )
        outcome = (
            DeliberationOutcome.NO_ACTION
            if decision is AgentDecision.NO_ACTION
            else DeliberationOutcome.PROPOSED
        )
        return DeliberationResult(outcome, proposal, None, 1, 1)


def _build_request(value: DeliberationInput) -> dict[str, object]:
    return {
        "contract_version": BEHAVIOR_CONTRACT_VERSION,
        "correlation_id": value.correlation_id,
        "trigger": value.trigger.value,
        "core_self": {
            "version": value.core_self.version,
            "identity": value.core_self.identity,
            "principles": value.core_self.principles,
        },
        "authorized_context": value.authorized_context,
        "permitted_mutable_state": value.permitted_mutable_state,
        "current_goals": value.current_goals,
        "capability_availability": {
            "reply": value.capabilities.replies_available,
            "research": value.capabilities.research_available,
            "propose_publication": value.capabilities.publication_proposals_available,
            "defer": value.channel_supports_defer,
        },
        "limits": {
            "max_steps": value.budget.max_steps,
            "max_model_calls": value.budget.max_model_calls,
            "max_execution_seconds": value.budget.max_execution_seconds,
            "deadline": value.deadline.isoformat(),
        },
        "prior_result": value.prior_result.value,
        "allowed_decisions": tuple(item.value for item in AgentDecision),
    }


def _validate_input(value: DeliberationInput) -> None:
    if not _valid_text(value.correlation_id):
        raise ValueError("correlation_id is invalid")
    if value.core_self != initial_core_self():
        raise ValueError("core_self must be the approved immutable Core Self")
    for name, items in (
        ("authorized_context", value.authorized_context),
        ("permitted_mutable_state", value.permitted_mutable_state),
        ("current_goals", value.current_goals),
    ):
        if (
            type(items) is not tuple
            or len(items) > MAX_CONTEXT_ITEMS
            or any(not _valid_text(item) for item in items)
        ):
            raise ValueError(f"{name} is invalid")
    if not isinstance(value.capabilities, BehaviorCapabilities) or not _valid_text(
        value.capabilities.version
    ):
        raise ValueError("capabilities are invalid")
    flags = (
        value.capabilities.emergency_stop_active,
        value.capabilities.provider_spend_suspended,
        value.capabilities.replies_available,
        value.capabilities.research_available,
        value.capabilities.publication_proposals_available,
        value.channel_supports_defer,
    )
    if any(type(flag) is not bool for flag in flags):
        raise ValueError("capability flags are invalid")
    if not isinstance(value.trigger, TriggerType) or not isinstance(
        value.prior_result, PriorResultCategory
    ):
        raise ValueError("decision context enums are invalid")
    if not isinstance(value.budget, DeliberationBudget):
        raise ValueError("budget is invalid")
    if (
        type(value.budget.max_steps) is not int
        or type(value.budget.max_model_calls) is not int
        or type(value.budget.max_execution_seconds) not in (int, float)
        or value.budget.max_steps < 0
        or value.budget.max_model_calls < 0
        or value.budget.max_execution_seconds < 0
    ):
        raise ValueError("budget is invalid")
    if (
        not isinstance(value.now, datetime)
        or not isinstance(value.deadline, datetime)
        or value.now.tzinfo is None
        or value.deadline.tzinfo is None
    ):
        raise ValueError("times must be timezone-aware")


def _result(outcome: DeliberationOutcome) -> DeliberationResult:
    return DeliberationResult(outcome, None, None, 0, 0)
