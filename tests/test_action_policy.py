from dataclasses import replace

import pytest

from app.core.action_policy import (
    POLICY_VERSION,
    ActionPolicy,
    CapabilityCategory,
    CapabilityState,
    PolicyOutcome,
    PolicyReason,
    TrustedActionContext,
)
from app.core.agent_proposals import (
    MAX_PROPOSAL_TEXT_UTF8_BYTES,
    AgentDecision,
    NoActionProposal,
    ProposalValidation,
    ProposalInvalidReason,
    PublicationProposal,
    ReplyProposal,
    ResearchProposal,
    UnsupportedProposal,
    parse_agent_proposal,
)
from app.core.events import (
    AuthenticatedSourceReference,
    ParticipantId,
    TrustedPrincipalContext,
)


def trusted(name: str) -> TrustedActionContext:
    return TrustedActionContext(
        correlation_id=f"correlation-{name}",
        principal=TrustedPrincipalContext(
            principal_id=ParticipantId(f"participant-{name}"),
            authenticated_source=AuthenticatedSourceReference(f"session-{name}"),
            is_owner=name == "owner",
        ),
    )


def capabilities(**changes) -> CapabilityState:
    return CapabilityState(
        version=changes.get("version", "operations-v1"),
        emergency_stop_active=changes.get("emergency_stop_active", False),
        provider_spend_suspended=changes.get("provider_spend_suspended", False),
        private_reply_drafts_enabled=changes.get("private_reply_drafts_enabled", True),
        publication_drafts_enabled=changes.get("publication_drafts_enabled", True),
        simulation_only=changes.get("simulation_only", True),
    )


def proposal(payload):
    parsed = parse_agent_proposal(payload)
    assert parsed.proposal is not None
    return parsed.proposal


def test_no_action_is_success_not_failure_or_operational_skip():
    policy = ActionPolicy()
    parsed = parse_agent_proposal(
        {"decision": "no_action", "reason_category": "not_warranted"}
    )

    decision = policy.evaluate(
        parsed,
        trusted("a"),
        capabilities(emergency_stop_active=True, provider_spend_suspended=True),
    )

    assert decision.evidence.outcome is PolicyOutcome.ACCEPTED_INTERNAL
    assert decision.evidence.reason is PolicyReason.NO_ACTION_ACCEPTED
    assert decision.evidence.capability is CapabilityCategory.NONE
    assert decision.dispatchable is False


@pytest.mark.parametrize(
    ("payload", "capability", "reason"),
    [
        (
            {"decision": "reply", "content": "Внутренний ответ."},
            CapabilityCategory.PRIVATE_REPLY_DRAFT,
            PolicyReason.PRIVATE_REPLY_DRAFT_ACCEPTED,
        ),
        (
            {"decision": "propose_publication", "draft": "Черновик."},
            CapabilityCategory.PUBLICATION_DRAFT,
            PolicyReason.PUBLICATION_DRAFT_ACCEPTED,
        ),
    ],
)
def test_supported_drafts_are_accepted_only_for_internal_simulation(
    payload, capability, reason
):
    policy = ActionPolicy()
    parsed = parse_agent_proposal(payload)

    decision = policy.evaluate(parsed, trusted("a"), capabilities())

    assert decision.evidence.outcome is PolicyOutcome.ACCEPTED_INTERNAL
    assert decision.evidence.reason is reason
    assert decision.evidence.capability is capability
    assert decision.dispatchable is True
    assert "participant-a" not in repr(decision.evidence)
    assert "session-a" not in repr(decision)


def test_research_is_recognized_but_unavailable_without_safe_adapter():
    decision = ActionPolicy().evaluate(
        parse_agent_proposal({"decision": "research", "brief": "Проверить факт."}),
        trusted("a"),
        capabilities(),
    )

    assert decision.evidence.outcome is PolicyOutcome.UNAVAILABLE_CAPABILITY
    assert decision.evidence.reason is PolicyReason.RESEARCH_CONTROLS_UNAVAILABLE
    assert decision.evidence.capability is CapabilityCategory.RESEARCH
    assert decision.dispatchable is False


@pytest.mark.parametrize(
    "decision_name",
    [
        "remember",
        "revise_belief",
        "revise_goal",
        "revise_mission",
        "defer",
        "terminate_conversation",
    ],
)
def test_other_contract_decisions_are_explicitly_unavailable(decision_name):
    decision = ActionPolicy().evaluate(
        parse_agent_proposal({"decision": decision_name}),
        trusted("a"),
        capabilities(),
    )

    assert decision.evidence.decision_type is AgentDecision(decision_name)
    assert decision.evidence.outcome is PolicyOutcome.UNAVAILABLE_CAPABILITY
    assert decision.evidence.reason is PolicyReason.DECISION_NOT_IMPLEMENTED
    assert decision.evidence.capability is CapabilityCategory.UNSUPPORTED


def test_invalid_proposal_has_closed_sanitized_policy_result():
    decision = ActionPolicy().evaluate(
        parse_agent_proposal({"decision": "shell", "command": "printenv; docker ps"}),
        trusted("a"),
        capabilities(),
    )

    assert decision.evidence.decision_type is None
    assert decision.evidence.outcome is PolicyOutcome.INVALID_PROPOSAL
    assert decision.evidence.reason is PolicyReason.INVALID_PROPOSAL_SCHEMA
    assert decision.evidence.capability is CapabilityCategory.NONE
    assert "shell" not in repr(decision)
    assert "printenv" not in repr(decision)
    assert decision.evidence.to_audit_record()["decision_type"] is None


@pytest.mark.parametrize(
    "validation",
    [
        ProposalValidation(
            ReplyProposal("looks valid"), ProposalInvalidReason.INVALID_FIELD
        ),
        ProposalValidation(ReplyProposal(""), None),
        ProposalValidation(ReplyProposal("bad\ud800"), None),
        ProposalValidation(
            ReplyProposal("x" * (MAX_PROPOSAL_TEXT_UTF8_BYTES + 1)), None
        ),
        ProposalValidation(ResearchProposal(""), None),
        ProposalValidation(PublicationProposal(7), None),
        ProposalValidation(NoActionProposal("invented"), None),
        ProposalValidation(UnsupportedProposal(AgentDecision.REPLY), None),
        ProposalValidation(UnsupportedProposal("shell"), None),
        ProposalValidation(object(), None),
        ProposalValidation(None, None),
    ],
)
def test_policy_revalidates_publicly_constructed_parse_results(validation):
    decision = ActionPolicy().evaluate(validation, trusted("a"), capabilities())

    assert validation.valid is False
    assert decision.evidence.decision_type is None
    assert decision.evidence.outcome is PolicyOutcome.INVALID_PROPOSAL
    assert decision.evidence.reason is PolicyReason.INVALID_PROPOSAL_SCHEMA
    assert decision.dispatchable is False


def test_emergency_stop_is_an_operational_skip():
    decision = ActionPolicy().evaluate(
        parse_agent_proposal({"decision": "reply", "content": "Черновик."}),
        trusted("a"),
        capabilities(emergency_stop_active=True),
    )

    assert decision.evidence.outcome is PolicyOutcome.SKIPPED_STOP
    assert decision.evidence.reason is PolicyReason.EMERGENCY_STOP_ACTIVE
    assert decision.dispatchable is False


def test_confirmed_spend_suspension_blocks_applicable_paid_research():
    decision = ActionPolicy().evaluate(
        parse_agent_proposal({"decision": "research", "brief": "Проверить факт."}),
        trusted("a"),
        capabilities(provider_spend_suspended=True),
    )

    assert decision.evidence.outcome is PolicyOutcome.SKIPPED_SPEND
    assert decision.evidence.reason is PolicyReason.PROVIDER_SPEND_SUSPENDED


@pytest.mark.parametrize(
    ("payload", "state"),
    [
        (
            {"decision": "reply", "content": "Черновик."},
            capabilities(private_reply_drafts_enabled=False),
        ),
        (
            {"decision": "propose_publication", "draft": "Черновик."},
            capabilities(publication_drafts_enabled=False),
        ),
    ],
)
def test_application_capability_configuration_is_default_deny(payload, state):
    decision = ActionPolicy().evaluate(
        parse_agent_proposal(payload), trusted("a"), state
    )

    assert decision.evidence.outcome is PolicyOutcome.UNAVAILABLE_CAPABILITY
    assert decision.evidence.reason is PolicyReason.CAPABILITY_DISABLED


def test_fake_effect_sink_receives_no_denied_operations():
    policy = ActionPolicy()
    route = trusted("a")
    cases = (
        (
            parse_agent_proposal({"decision": "shell", "command": "echo denied"}),
            capabilities(),
        ),
        (
            parse_agent_proposal({"decision": "research", "brief": "denied"}),
            capabilities(),
        ),
        (
            parse_agent_proposal({"decision": "reply", "content": "denied"}),
            capabilities(emergency_stop_active=True),
        ),
        (
            parse_agent_proposal({"decision": "reply", "content": "denied"}),
            capabilities(private_reply_drafts_enabled=False),
        ),
    )
    effects = []

    for parsed, state in cases:
        decision = policy.evaluate(parsed, route, state)
        if decision.dispatchable:
            effects.append(decision)

    assert effects == []


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "reply", "content": "Черновик."},
        {"decision": "propose_publication", "draft": "Черновик."},
    ],
)
def test_real_effect_mode_cannot_be_enabled_by_configuration(payload):
    decision = ActionPolicy().evaluate(
        parse_agent_proposal(payload),
        trusted("a"),
        capabilities(simulation_only=False),
    )

    assert decision.evidence.outcome is PolicyOutcome.REJECTED_BY_POLICY
    assert decision.evidence.reason is PolicyReason.REAL_EFFECTS_DISABLED
    assert decision.dispatchable is False


def test_policy_evidence_is_bounded_explicit_and_sanitized():
    decision = ActionPolicy().evaluate(
        parse_agent_proposal({"decision": "reply", "content": "private text"}),
        trusted("a"),
        capabilities(),
    )

    assert decision.evidence.to_audit_record() == {
        "correlation_id": "correlation-a",
        "decision_type": "reply",
        "policy_version": POLICY_VERSION,
        "outcome": "accepted_internal",
        "reason_code": "private_reply_draft_accepted",
        "capability_category": "private_reply_draft",
        "operational_state_version": "operations-v1",
    }
    assert "private text" not in repr(decision.evidence)


def test_stop_change_between_acceptance_and_recheck_blocks_simulated_effect():
    policy = ActionPolicy()
    parsed = parse_agent_proposal({"decision": "reply", "content": "Черновик."})
    route = trusted("a")
    initial = policy.evaluate(parsed, route, capabilities(version="state-1"))
    effects = []

    current = policy.revalidate(
        initial,
        parsed.proposal,
        route,
        capabilities(version="state-2", emergency_stop_active=True),
    )
    if current.dispatchable:
        effects.append("would-dispatch")

    assert initial.dispatchable is True
    assert current.evidence.outcome is PolicyOutcome.SKIPPED_STOP
    assert current.evidence.operational_state_version == "state-2"
    assert effects == []


@pytest.mark.parametrize(
    "state",
    [
        capabilities(version="state-2", private_reply_drafts_enabled=False),
        capabilities(version="state-2", simulation_only=False),
    ],
)
def test_other_time_sensitive_state_is_rechecked_before_simulation(state):
    policy = ActionPolicy()
    parsed = parse_agent_proposal({"decision": "reply", "content": "Черновик."})
    route = trusted("a")
    initial = policy.evaluate(parsed, route, capabilities(version="state-1"))

    current = policy.revalidate(initial, parsed.proposal, route, state)

    assert current.dispatchable is False
    assert current.evidence.operational_state_version == "state-2"


def test_recheck_rejects_proposal_or_trusted_destination_substitution():
    policy = ActionPolicy()
    original = parse_agent_proposal({"decision": "reply", "content": "A draft"})
    replacement = proposal({"decision": "reply", "content": "B draft"})
    initial = policy.evaluate(original, trusted("a"), capabilities())

    wrong_proposal = policy.revalidate(
        initial, replacement, trusted("a"), capabilities()
    )
    wrong_principal = policy.revalidate(
        initial, original.proposal, trusted("b"), capabilities()
    )

    assert wrong_proposal.evidence.outcome is PolicyOutcome.REJECTED_BY_POLICY
    assert wrong_proposal.evidence.reason is PolicyReason.GRANT_MISMATCH
    assert wrong_principal.evidence.outcome is PolicyOutcome.REJECTED_BY_POLICY
    assert wrong_principal.evidence.reason is PolicyReason.TRUSTED_ROUTE_MISMATCH


def test_denied_or_no_action_decisions_cannot_be_revalidated_as_dispatchable():
    policy = ActionPolicy()
    parsed = parse_agent_proposal({"decision": "reply", "content": "draft"})
    route = trusted("a")
    denied = policy.evaluate(
        parsed, route, capabilities(private_reply_drafts_enabled=False)
    )
    no_action = policy.evaluate(
        parse_agent_proposal({"decision": "no_action", "reason_category": "duplicate"}),
        route,
        capabilities(),
    )

    for prior, item in ((denied, parsed.proposal), (no_action, no_action.proposal)):
        result = policy.revalidate(prior, item, route, capabilities())
        assert result.evidence.reason is PolicyReason.GRANT_MISMATCH
        assert result.dispatchable is False

    without_proposal = policy.revalidate(denied, None, route, capabilities())
    assert without_proposal.evidence.capability is CapabilityCategory.NONE


def test_recheck_requires_a_typed_prior_decision():
    with pytest.raises(TypeError):
        ActionPolicy().revalidate(
            object(),
            None,
            trusted("a"),
            capabilities(),
        )


def test_same_proposal_for_alternating_participants_has_no_shared_route_state():
    policy = ActionPolicy()
    parsed = parse_agent_proposal({"decision": "reply", "content": "same draft"})

    a = policy.evaluate(parsed, trusted("a"), capabilities())
    b = policy.evaluate(parsed, trusted("b"), capabilities())
    a_again = policy.evaluate(parsed, trusted("a"), capabilities())

    assert policy.revalidate(
        a, parsed.proposal, trusted("a"), capabilities()
    ).dispatchable
    assert policy.revalidate(
        b, parsed.proposal, trusted("b"), capabilities()
    ).dispatchable
    assert policy.revalidate(
        a_again, parsed.proposal, trusted("a"), capabilities()
    ).dispatchable
    assert (
        policy.revalidate(
            a, parsed.proposal, trusted("b"), capabilities()
        ).evidence.reason
        is PolicyReason.TRUSTED_ROUTE_MISMATCH
    )


@pytest.mark.parametrize(
    "bad_state",
    [
        replace(capabilities(), version=""),
        replace(capabilities(), emergency_stop_active=1),
        replace(capabilities(), provider_spend_suspended=0),
        replace(capabilities(), private_reply_drafts_enabled=1),
        replace(capabilities(), publication_drafts_enabled=0),
        replace(capabilities(), simulation_only=1),
    ],
)
def test_malformed_trusted_capability_state_fails_closed(bad_state):
    with pytest.raises(ValueError):
        ActionPolicy().evaluate(
            parse_agent_proposal({"decision": "reply", "content": "draft"}),
            trusted("a"),
            bad_state,
        )


def test_policy_requires_a_typed_parse_result_and_capability_state():
    with pytest.raises(TypeError):
        ActionPolicy().evaluate(object(), trusted("a"), capabilities())
    with pytest.raises(TypeError):
        ActionPolicy().evaluate(ProposalValidation(None, None), trusted("a"), object())


@pytest.mark.parametrize("bad_context", [None, object(), "owner"])
def test_untyped_trusted_context_is_rejected(bad_context):
    with pytest.raises(TypeError):
        ActionPolicy().evaluate(
            parse_agent_proposal({"decision": "reply", "content": "draft"}),
            bad_context,
            capabilities(),
        )


@pytest.mark.parametrize(
    "route",
    [
        TrustedActionContext("correlation-a", object()),
        TrustedActionContext(
            "correlation-a",
            TrustedPrincipalContext(
                principal_id=object(),
                authenticated_source=AuthenticatedSourceReference("session-a"),
            ),
        ),
        TrustedActionContext(
            "correlation-a",
            TrustedPrincipalContext(
                principal_id=ParticipantId("participant-a"),
                authenticated_source=object(),
            ),
        ),
        TrustedActionContext(
            "correlation-a",
            TrustedPrincipalContext(
                principal_id=ParticipantId("participant-a"),
                authenticated_source=AuthenticatedSourceReference("session-a"),
                is_owner=1,
            ),
        ),
    ],
)
def test_malformed_trusted_route_components_are_rejected(route):
    with pytest.raises(TypeError):
        ActionPolicy().evaluate(
            parse_agent_proposal({"decision": "reply", "content": "draft"}),
            route,
            capabilities(),
        )


@pytest.mark.parametrize(
    "route",
    [
        TrustedActionContext("", trusted("a").principal),
        TrustedActionContext("bad\ud800", trusted("a").principal),
        TrustedActionContext("x" * 129, trusted("a").principal),
        TrustedActionContext(
            "correlation-a",
            TrustedPrincipalContext(
                ParticipantId(""), AuthenticatedSourceReference("session-a")
            ),
        ),
        TrustedActionContext(
            "correlation-a",
            TrustedPrincipalContext(
                ParticipantId("participant-a"), AuthenticatedSourceReference("")
            ),
        ),
    ],
)
def test_malformed_trusted_identifiers_are_rejected(route):
    with pytest.raises(ValueError):
        ActionPolicy().evaluate(
            parse_agent_proposal({"decision": "reply", "content": "draft"}),
            route,
            capabilities(),
        )


def test_unsupported_capability_is_retained_on_failed_recheck():
    parsed = parse_agent_proposal({"decision": "remember"})
    denied = ActionPolicy().evaluate(parsed, trusted("a"), capabilities())

    result = ActionPolicy().revalidate(
        denied, parsed.proposal, trusted("a"), capabilities()
    )

    assert result.evidence.capability is CapabilityCategory.UNSUPPORTED
