from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.behavior.deliberation import (
    BEHAVIOR_CONTRACT_VERSION,
    BehaviorCapabilities,
    BehaviorIntent,
    DeliberationBudget,
    DeliberationEngine,
    DeliberationInput,
    DeliberationOutcome,
    PriorResultCategory,
    RecordedBehaviorModel,
    TriggerType,
    parse_behavior_proposal,
)
from app.core.agent_proposals import (
    AgentDecision,
    NoActionProposal,
    NoActionReason,
    ReplyProposal,
)
from app.core.core_self import initial_core_self


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def behavior_input(
    *,
    capabilities: BehaviorCapabilities | None = None,
    budget: DeliberationBudget | None = None,
    supports_defer: bool = True,
    deadline: datetime | None = None,
) -> DeliberationInput:
    return DeliberationInput(
        correlation_id="corr-1",
        authorized_context=("Аргумент собеседника",),
        core_self=initial_core_self(),
        permitted_mutable_state=("убеждение: проверять источники",),
        current_goals=("исследовать полезные вопросы",),
        capabilities=capabilities or BehaviorCapabilities.all_simulated("caps-v1"),
        trigger=TriggerType.PRIVATE_MESSAGE,
        budget=budget or DeliberationBudget(2, 1, 5.0),
        now=NOW,
        deadline=deadline or NOW + timedelta(seconds=10),
        prior_result=PriorResultCategory.NONE,
        channel_supports_defer=supports_defer,
    )


def test_deliberation_returns_existing_typed_proposals_without_effects() -> None:
    model = RecordedBehaviorModel(
        (
            {"decision": "reply", "content": "Я не согласен: довод не подтверждён."},
            {"decision": "no_action", "reason_category": "low_value"},
        )
    )
    engine = DeliberationEngine(model)

    reply = engine.deliberate(behavior_input())
    inactive = engine.deliberate(behavior_input())

    assert reply.outcome is DeliberationOutcome.PROPOSED
    assert reply.proposal == ReplyProposal("Я не согласен: довод не подтверждён.")
    assert inactive.outcome is DeliberationOutcome.NO_ACTION
    assert inactive.proposal == NoActionProposal(NoActionReason.LOW_VALUE)
    assert reply.model_calls == reply.steps == 1
    assert model.requests[0]["contract_version"] == BEHAVIOR_CONTRACT_VERSION
    assert "owner" not in repr(model.requests[0]).lower()


@pytest.mark.parametrize(
    ("payload", "decision", "parameter"),
    [
        (
            {"decision": "remember", "statement": "Сохранить вывод"},
            AgentDecision.REMEMBER,
            ("statement", "Сохранить вывод"),
        ),
        (
            {
                "decision": "revise_belief",
                "proposition": "Гипотеза",
                "rationale": "Новые данные",
                "evidence_ids": ["e-1"],
                "counterevidence_ids": ["e-2"],
                "confidence": 0.6,
            },
            AgentDecision.REVISE_BELIEF,
            ("confidence", 0.6),
        ),
        (
            {
                "decision": "revise_goal",
                "goal_id": "g-1",
                "status": "abandoned",
                "rationale": "Цель больше не полезна",
            },
            AgentDecision.REVISE_GOAL,
            ("status", "abandoned"),
        ),
        (
            {
                "decision": "revise_mission",
                "proposed_mission": "Изучать проверяемые идеи",
                "supporting_reasons": ["Накоплен опыт"],
                "counterevidence": ["Старое направление ещё полезно"],
            },
            AgentDecision.REVISE_MISSION,
            ("proposed_mission", "Изучать проверяемые идеи"),
        ),
        (
            {"decision": "defer", "resume_condition": "После новых данных"},
            AgentDecision.DEFER,
            ("resume_condition", "После новых данных"),
        ),
        (
            {
                "decision": "terminate_conversation",
                "scope": "conversation",
                "cooldown_seconds": 300,
            },
            AgentDecision.TERMINATE_CONVERSATION,
            ("scope", "conversation"),
        ),
    ],
)
def test_pending_integration_decisions_are_typed_intents(
    payload: dict[str, object],
    decision: AgentDecision,
    parameter: tuple[str, object],
) -> None:
    parsed = parse_behavior_proposal(payload)

    assert parsed.valid
    assert isinstance(parsed.proposal, BehaviorIntent)
    assert parsed.proposal.decision is decision
    assert dict(parsed.proposal.parameters)[parameter[0]] == parameter[1]


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "invent_power", "content": "x"},
        {"decision": "remember", "statement": "x", "admin": True},
        {"decision": "remember", "statement": ""},
        {
            "decision": "revise_belief",
            "proposition": "p",
            "rationale": "r",
            "evidence_ids": [],
            "counterevidence_ids": [],
            "confidence": 2,
        },
        {
            "decision": "revise_goal",
            "goal_id": "g",
            "status": "change_security_settings",
            "rationale": "r",
        },
        {
            "decision": "revise_mission",
            "proposed_mission": "m",
            "supporting_reasons": [],
            "counterevidence": ["c"],
        },
        {"decision": "defer", "resume_condition": 4},
        {
            "decision": "terminate_conversation",
            "scope": "global",
            "cooldown_seconds": -1,
        },
    ],
)
def test_behavior_parser_fails_closed(payload: dict[str, object]) -> None:
    parsed = parse_behavior_proposal(payload)
    assert not parsed.valid
    assert parsed.proposal is None
    assert parsed.invalid_reason is not None


def test_engine_distinguishes_operational_skips_and_capability_denials() -> None:
    stopped = BehaviorCapabilities("v", True, False, True, True, True)
    suspended = BehaviorCapabilities("v", False, True, True, True, True)
    disabled = BehaviorCapabilities("v", False, False, True, False, False)

    assert (
        DeliberationEngine(RecordedBehaviorModel(()))
        .deliberate(behavior_input(capabilities=stopped))
        .outcome
        is DeliberationOutcome.SKIPPED_STOP
    )
    assert (
        DeliberationEngine(RecordedBehaviorModel(()))
        .deliberate(behavior_input(capabilities=suspended))
        .outcome
        is DeliberationOutcome.SKIPPED_SPEND
    )

    research = DeliberationEngine(
        RecordedBehaviorModel(({"decision": "research", "brief": "Проверить данные"},))
    ).deliberate(behavior_input(capabilities=disabled))
    publication = DeliberationEngine(
        RecordedBehaviorModel(
            ({"decision": "propose_publication", "draft": "Проверенный вывод"},)
        )
    ).deliberate(behavior_input(capabilities=disabled))
    assert research.outcome is DeliberationOutcome.UNAVAILABLE_CAPABILITY
    assert publication.outcome is DeliberationOutcome.UNAVAILABLE_CAPABILITY


def test_engine_enforces_budgets_deadline_defer_and_malformed_output() -> None:
    no_budget = DeliberationEngine(RecordedBehaviorModel(())).deliberate(
        behavior_input(budget=DeliberationBudget(0, 0, 0.0))
    )
    expired = DeliberationEngine(RecordedBehaviorModel(())).deliberate(
        behavior_input(deadline=NOW)
    )
    defer = DeliberationEngine(
        RecordedBehaviorModel(
            ({"decision": "defer", "resume_condition": "Когда появится источник"},)
        )
    ).deliberate(behavior_input(supports_defer=False))
    malformed = DeliberationEngine(
        RecordedBehaviorModel(("not an object",))
    ).deliberate(behavior_input())

    assert no_budget.outcome is DeliberationOutcome.BUDGET_EXHAUSTED
    assert expired.outcome is DeliberationOutcome.BUDGET_EXHAUSTED
    assert defer.outcome is DeliberationOutcome.UNAVAILABLE_CAPABILITY
    assert malformed.outcome is DeliberationOutcome.INVALID_PROPOSAL


def test_input_and_fake_model_validation() -> None:
    with pytest.raises(ValueError, match="responses exhausted"):
        RecordedBehaviorModel(()).complete({})
    with pytest.raises(TypeError, match="DeliberationInput"):
        DeliberationEngine(RecordedBehaviorModel(())).deliberate(object())  # type: ignore[arg-type]
    invalid = behavior_input()
    object.__setattr__(invalid, "correlation_id", "")
    with pytest.raises(ValueError, match="correlation_id"):
        DeliberationEngine(RecordedBehaviorModel(())).deliberate(invalid)


def test_additional_closed_schema_and_capability_paths() -> None:
    invalid_payloads = (
        {"decision": "remember"},
        {
            "decision": "revise_goal",
            "goal_id": "g",
            "status": 3,
            "rationale": "r",
        },
        {
            "decision": "revise_belief",
            "proposition": "",
            "rationale": "r",
            "evidence_ids": [],
            "counterevidence_ids": [],
            "confidence": 0.5,
        },
        {
            "decision": "terminate_conversation",
            "scope": "conversation",
            "cooldown_seconds": 2_592_001,
        },
        {"decision": "remember", "statement": "\ud800"},
    )
    assert all(
        not parse_behavior_proposal(payload).valid for payload in invalid_payloads
    )

    capabilities = BehaviorCapabilities("v", False, False, False, True, True)
    result = DeliberationEngine(
        RecordedBehaviorModel(({"decision": "reply", "content": "Ответ"},))
    ).deliberate(behavior_input(capabilities=capabilities))
    assert result.outcome is DeliberationOutcome.UNAVAILABLE_CAPABILITY


def test_deliberation_input_rejects_every_untrusted_shape() -> None:
    base = behavior_input()
    invalid_values = (
        replace(base, core_self=replace(initial_core_self(), identity="Impostor")),
        replace(base, authorized_context=("",)),
        replace(base, capabilities=replace(base.capabilities, version="")),
        replace(base, capabilities=replace(base.capabilities, replies_available=1)),
        replace(base, trigger="private_message"),
        replace(base, budget=object()),
        replace(base, budget=DeliberationBudget(-1, 1, 1.0)),
        replace(base, now=NOW.replace(tzinfo=None)),
    )
    for invalid in invalid_values:
        with pytest.raises(ValueError):
            DeliberationEngine(RecordedBehaviorModel(())).deliberate(invalid)
