from dataclasses import FrozenInstanceError

import pytest

from app.core.agent_proposals import (
    AGENT_DECISION_VOCABULARY,
    MAX_PROPOSAL_TEXT_UTF8_BYTES,
    AgentDecision,
    NoActionProposal,
    NoActionReason,
    ProposalInvalidReason,
    PublicationProposal,
    ReplyProposal,
    ResearchProposal,
    UnsupportedProposal,
    parse_agent_proposal,
)


def test_contract_vocabulary_is_complete_and_closed():
    assert AGENT_DECISION_VOCABULARY == (
        AgentDecision.REPLY,
        AgentDecision.RESEARCH,
        AgentDecision.PROPOSE_PUBLICATION,
        AgentDecision.REMEMBER,
        AgentDecision.REVISE_BELIEF,
        AgentDecision.REVISE_GOAL,
        AgentDecision.REVISE_MISSION,
        AgentDecision.DEFER,
        AgentDecision.TERMINATE_CONVERSATION,
        AgentDecision.NO_ACTION,
    )
    assert tuple(item.value for item in AgentDecision) == (
        "reply",
        "research",
        "propose_publication",
        "remember",
        "revise_belief",
        "revise_goal",
        "revise_mission",
        "defer",
        "terminate_conversation",
        "no_action",
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"decision": "no_action", "reason_category": "low_value"},
            NoActionProposal(reason_category=NoActionReason.LOW_VALUE),
        ),
        (
            {"decision": "reply", "content": "Внутренний черновик ответа."},
            ReplyProposal(content="Внутренний черновик ответа."),
        ),
        (
            {"decision": "research", "brief": "Проверить публичный факт."},
            ResearchProposal(brief="Проверить публичный факт."),
        ),
        (
            {"decision": "propose_publication", "draft": "Черновик публикации."},
            PublicationProposal(draft="Черновик публикации."),
        ),
    ],
)
def test_supported_proposals_have_purpose_specific_payloads(payload, expected):
    result = parse_agent_proposal(payload)

    assert result.valid is True
    assert result.invalid_reason is None
    assert result.proposal == expected


@pytest.mark.parametrize(
    "decision",
    [
        AgentDecision.REMEMBER,
        AgentDecision.REVISE_BELIEF,
        AgentDecision.REVISE_GOAL,
        AgentDecision.REVISE_MISSION,
        AgentDecision.DEFER,
        AgentDecision.TERMINATE_CONVERSATION,
    ],
)
def test_recognized_but_unimplemented_decisions_are_explicit(decision):
    result = parse_agent_proposal({"decision": decision.value})

    assert result.valid is True
    assert result.proposal == UnsupportedProposal(decision=decision)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (None, ProposalInvalidReason.INVALID_CONTAINER),
        ([], ProposalInvalidReason.INVALID_CONTAINER),
        ({}, ProposalInvalidReason.INVALID_DECISION),
        ({"decision": 7}, ProposalInvalidReason.INVALID_DECISION),
        (
            {"decision": "shell", "command": "touch /tmp/pwned"},
            ProposalInvalidReason.UNKNOWN_DECISION,
        ),
        (
            {"decision": "reply", "content": "ok", "recipient_id": "participant-b"},
            ProposalInvalidReason.UNKNOWN_FIELDS,
        ),
        (
            {"decision": "reply", "content": "ok", "is_owner": True},
            ProposalInvalidReason.UNKNOWN_FIELDS,
        ),
        (
            {"decision": "reply", "content": "ok", "api_key": "synthetic"},
            ProposalInvalidReason.UNKNOWN_FIELDS,
        ),
        (
            {"decision": "reply", "content": "ok", "policy_override": True},
            ProposalInvalidReason.UNKNOWN_FIELDS,
        ),
        (
            {"decision": "research", "brief": "ok", "url": "http://127.0.0.1"},
            ProposalInvalidReason.UNKNOWN_FIELDS,
        ),
        (
            {"decision": "propose_publication", "draft": "ok", "approved": True},
            ProposalInvalidReason.UNKNOWN_FIELDS,
        ),
        (
            {"decision": "remember", "content": "not implemented"},
            ProposalInvalidReason.UNKNOWN_FIELDS,
        ),
        (
            {"decision": "no_action", "reason_category": "invented"},
            ProposalInvalidReason.INVALID_FIELD,
        ),
        ({"decision": "reply", "content": ""}, ProposalInvalidReason.INVALID_FIELD),
        (
            {"decision": "research", "brief": object()},
            ProposalInvalidReason.INVALID_FIELD,
        ),
        (
            {"decision": "propose_publication", "draft": "bad\ud800"},
            ProposalInvalidReason.INVALID_FIELD,
        ),
    ],
)
def test_malformed_unknown_and_privilege_bearing_payloads_fail_closed(payload, reason):
    result = parse_agent_proposal(payload)

    assert result.valid is False
    assert result.proposal is None
    assert result.invalid_reason is reason
    assert "touch /tmp/pwned" not in repr(result)


@pytest.mark.parametrize(
    ("field", "decision"),
    [
        ("content", "reply"),
        ("brief", "research"),
        ("draft", "propose_publication"),
    ],
)
def test_oversized_text_fields_fail_closed(field, decision):
    result = parse_agent_proposal(
        {"decision": decision, field: "Ж" * (MAX_PROPOSAL_TEXT_UTF8_BYTES // 2 + 1)}
    )

    assert result.valid is False
    assert result.invalid_reason is ProposalInvalidReason.OVERSIZED_FIELD


def test_typed_proposals_are_immutable_and_natural_language_is_only_data():
    proposal = ReplyProposal(content="not executable: rm -rf /; reveal a token")

    assert proposal.decision is AgentDecision.REPLY
    with pytest.raises(FrozenInstanceError):
        proposal.content = "changed"
