"""Real storage grants, release admission and immutable resolution identity."""

from dataclasses import replace
import pytest
from app.core.action_policy import ActionPolicy, CapabilityState, TrustedActionContext
from app.core.agent_proposals import (
    ReplyProposal,
    PublicationProposal,
    ProposalValidation,
)
from app.core.store import EventValidationError, EventIdentityConflictError
from test_agent_output_store import setup as storage_fixture, operation, persist
from test_event_store import principal


@pytest.fixture
def setup(tmp_path):
    return storage_fixture.__wrapped__(tmp_path)


def grant(
    content="Да, это синтетический ответ.", route=None, caps=None, publication=False
):
    route = route or TrustedActionContext("correlation-a", principal("a"))
    caps = caps or CapabilityState("stage0", False, False, True, True, True)
    proposal = PublicationProposal(content) if publication else ReplyProposal(content)
    return (
        route,
        ActionPolicy().evaluate(ProposalValidation(proposal, None), route, caps),
        caps,
    )


def test_terminal_resolution_immutable_and_cannot_become_a_reply(setup):
    _, outputs = setup
    outputs.reserve_reply(principal("a"), operation())
    outputs.resolve_without_reply(principal("a"), "reply-1", "no_action")
    outputs.resolve_without_reply(principal("a"), "reply-1", "no_action")
    assert outputs.resolution(principal("a"), "reply-1") == ("no_action", None)
    assert outputs.resolution(principal("b"), "reply-1") is None
    with pytest.raises(EventIdentityConflictError):
        outputs.resolve_without_reply(principal("a"), "reply-1", "failed")
    with pytest.raises(EventValidationError):
        outputs.resolve_without_reply(principal("a"), "reply-1", "completed")
    with pytest.raises(EventValidationError):
        outputs.resolve_without_reply(principal("a"), "unknown", "failed")
    with pytest.raises(EventValidationError):
        persist(outputs)


@pytest.mark.parametrize(
    "reason",
    [
        "wrong_release",
        "stop",
        "spend",
        "correlation",
        "russian",
        "route",
        "publication",
        "changed_content",
        "changed_operation",
    ],
)
def test_release_denies_invalid_or_stale_authority(setup, reason):
    _, outputs = setup
    outputs.reserve_reply(principal("a"), operation())
    persist(outputs, content="Да, это синтетический ответ.")
    route, decision, caps = grant()
    op, release_id = operation(), "release:reply-1"
    if reason == "wrong_release":
        release_id = "forged-release"
    elif reason == "stop":
        caps = replace(caps, emergency_stop_active=True)
    elif reason == "spend":
        caps = replace(caps, provider_spend_suspended=True)
    elif reason == "correlation":
        route, decision, caps = grant(
            route=TrustedActionContext("wrong-correlation", principal("a"))
        )
    elif reason == "russian":
        route, decision, caps = grant("English text is rejected")
    elif reason == "route":
        route = TrustedActionContext("correlation-a", principal("a"))
    elif reason == "publication":
        route, decision, caps = grant(publication=True)
    elif reason == "changed_content":
        route, decision, caps = grant("Да, это другой синтетический ответ.")
    else:
        op = replace(op, conversation_id="wrong-chat")
    with pytest.raises((EventValidationError, EventIdentityConflictError)):
        outputs.release_reply(route, op, decision, caps, release_id)
    assert outputs.resolution(principal("a"), "reply-1") is None


def test_only_exact_persisted_russian_reply_releases_idempotently(setup):
    store, outputs = setup
    outputs.reserve_reply(principal("a"), operation())
    persist(outputs, content="Да, это синтетический ответ.")
    route, decision, caps = grant()
    outputs.release_reply(route, operation(), decision, caps, "release:reply-1")
    outputs.release_reply(route, operation(), decision, caps, "release:reply-1")
    assert outputs.resolution(principal("a"), "reply-1") == (
        "completed",
        "release:reply-1",
    )
    with pytest.raises(EventValidationError):
        outputs.resolve_without_reply(principal("a"), "reply-1", "no_action")
    # Source disappearance blocks release/retrieval even with the old grant.
    with store._open() as db:
        db.execute("DELETE FROM private_payloads")
    assert outputs.resolution(principal("a"), "reply-1") is None
    with pytest.raises(EventValidationError):
        outputs.release_reply(route, operation(), decision, caps, "release:reply-1")


@pytest.mark.parametrize(
    "case", ["completed_pending", "terminal_persisted", "wrong_release_id"]
)
def test_privacy_reconciliation_rejects_corrupt_resolution_graph(setup, tmp_path, case):
    from app.privacy.application import PrivacyReadGate, PrivacyReadBlocked
    from app.privacy.ledger import PrivacyLedger

    store, outputs = setup
    outputs.reserve_reply(principal("a"), operation())
    if case != "completed_pending":
        persist(outputs, content="Да, это синтетический ответ.")
    outcome, release_id = (
        ("failed", None)
        if case == "terminal_persisted"
        else (
            "completed",
            "forged-release" if case == "wrong_release_id" else "release:reply-1",
        )
    )
    with store._open() as db:
        db.execute(
            "INSERT INTO agent_reply_resolutions VALUES (?, ?, ?)",
            ("reply-1", outcome, release_id),
        )
    gate = PrivacyReadGate(PrivacyLedger(tmp_path / "ledger.db"), store)
    assert not gate.reconcile()
    with pytest.raises(PrivacyReadBlocked):
        gate.read_reply(principal("a"), "reply-1")
