"""A local reviewer can release only an exact, consented abstraction."""

from dataclasses import replace
from datetime import timedelta
import json
import multiprocessing
import os
import runpy
import sqlite3
import sys

import pytest

from app.cognition.shared import ReviewedSharedCandidate, SharedIntelligence
from app.cognition.review_cli import main as review_main
from app.cognition.contracts import PrivacyScreenDecision, PrivacyScreenResult
from app.core.agent_output_store import AgentOutputStore
from app.core.shared_graph import erase_shared_source, validate_shared_graph
from app.core.openai_worker_client import ClientResult, OpenAIWorkerClient
from app.core.agent_proposals import ProposalValidation, ReplyProposal
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore, FaultPoint, schema_objects
from app.privacy.application import (
    EventStoreRestoreAdapter,
    PrivacyReadBlocked,
    PrivacyReadGate,
)
from app.privacy.application import EventStoreErasureAdapter
from app.privacy.consent import ConsentService
from app.privacy.deletion import DeletionCoordinator
from app.privacy.recovery import QuarantinedRestore, create_sqlite_backup
from app.privacy.models import (
    ConsentGrant,
    ConsentPurpose,
    OperationOrigin,
    TrustedPrivacyContext,
)
from app.private_web.runtime import Stage0ConversationRuntime, WorkerConversationRuntime
from app.private_web.research_bridge import (
    SyntheticResearchBridge,
    SyntheticResearchClient,
    started_id,
)
from app.trusted.research_worker import ResearchWorkerRequest
from app.trusted.openai_transport import FakeOpenAITransport
from test_cognition_pipeline import NOW, append_event, principal
from test_private_conversation_runtime import response
from test_int19_worker_runtime import _app as worker_app, _worker as worker_process
from test_privacy_deletion import (
    context as deletion_context,
    request as deletion_request,
)
from test_research_worker import FIXTURE
from test_shared_intelligence import approval
from shared_schema_helpers import remove_v11
import test_shared_intelligence as shared_tests


@pytest.fixture
def shared(tmp_path):
    return shared_tests.shared.__wrapped__(tmp_path)


def consent(ledger, event_id, audience="participant-b", principal_id="participant-a"):
    service = ConsentService(ledger, clock=lambda: NOW)
    suffix = principal_id.rsplit("-", 1)[-1]
    actor = f"actor-{suffix}"
    authentication = f"auth-{suffix}"
    grant = ConsentGrant(
        f"consent-{event_id}-{audience}",
        f"grant-{event_id}-{audience}",
        principal_id,
        event_id,
        audience,
        ConsentPurpose.SHARED_ABSTRACTION,
        NOW - timedelta(minutes=1),
        NOW + timedelta(days=730),
        actor,
        1,
        f"correlation-{event_id}-{audience}",
        authentication,
    )
    context = TrustedPrivacyContext(
        principal_id, actor, authentication, OperationOrigin.CONTROL
    )
    assert service.grant(context, grant).created
    return service, context, grant


def candidate(event_id, value="Explicit alternatives can improve decision review."):
    synthetic = approval(event_id, value=value)
    return ReviewedSharedCandidate(
        synthetic.principal,
        synthetic.proposal,
        synthetic.item_id,
        synthetic.expected_revision,
        ("participant-b",),
    )


def test_reviewed_candidate_requires_exact_review_and_consent(shared, monkeypatch):
    events, ledger, gate, a, _ = shared
    item = candidate(a)
    app = SharedIntelligence(events, gate, clock=lambda: NOW)
    assert app.quarantine(item) == "shared_commit_disabled"
    assert app.context(principal("b")) == ()
    with pytest.raises(ValueError, match="consent"):
        app.review(
            item,
            decision_id="decision-a",
            review_version="local-review-v1",
            approve=True,
        )
    consent(ledger, a)
    assert gate.reconcile()
    assert (
        app.review(
            item,
            decision_id="decision-a",
            review_version="local-review-v1",
            approve=True,
        )
        == "shared-a"
    )
    with events._open() as db:
        assert db.execute(
            "SELECT review_identity FROM reviewed_shared_decisions WHERE operation_id=?",
            (item.proposal.operation_id,),
        ).fetchone() == (f"os-uid:{os.geteuid()}",)
    with monkeypatch.context() as patcher:
        patcher.setattr(
            SharedIntelligence,
            "_review_identity",
            staticmethod(lambda: "os-uid:987654"),
        )
        with pytest.raises(ValueError, match="identity conflict"):
            app.review(
                item,
                decision_id="decision-a",
                review_version="local-review-v1",
                approve=True,
            )
    assert app.context(principal("b"))[0].text == item.proposal.candidate.value
    assert (
        app.review(
            item,
            decision_id="decision-a",
            review_version="local-review-v1",
            approve=True,
        )
        == "shared-a"
    )
    with pytest.raises(ValueError, match="conflict"):
        app.review(
            item,
            decision_id="decision-b",
            review_version="local-review-v1",
            approve=True,
        )
    with pytest.raises(ValueError, match="conflict"):
        app.quarantine(
            replace(
                item,
                proposal=replace(
                    item.proposal,
                    candidate=replace(item.proposal.candidate, value="Another idea."),
                ),
            )
        )
    other_source = append_event(
        events, principal("a"), "other-source", "A separate private example"
    )
    assert gate.reconcile()
    with pytest.raises(ValueError, match="conflict"):
        app.quarantine(
            replace(
                item,
                proposal=replace(
                    item.proposal, protected_source_event_ids=(a, other_source)
                ),
            )
        )


def test_two_reviewed_ideas_reach_b_worker_without_a_material(shared):
    events, ledger, gate, a, _ = shared
    canary = "CANARY-A-PRIVATE-ALBATROSS-93"
    second_source = append_event(
        events, principal("a"), "a-extra", "A private example " + canary
    )
    assert gate.reconcile()
    first = candidate(a)
    second = candidate(
        second_source, "Compare distinct explanations before settling a view."
    )
    second = replace(
        second,
        proposal=replace(second.proposal, operation_id="shared-second"),
        item_id="second-question",
    )
    app = SharedIntelligence(events, gate, clock=lambda: NOW)
    assert app.quarantine(first) == "shared_commit_disabled"
    assert app.quarantine(second) == "shared_commit_disabled"
    fake = FakeOpenAITransport([response(), response()])
    runtime = Stage0ConversationRuntime(
        events, gate, fake, shared_intelligence=app, clock=lambda: NOW
    )
    runtime.submit(principal("b"), "before-review", "Обсудим идеи")
    before = fake.calls[0].request.input_text
    assert first.proposal.candidate.value not in before
    assert second.proposal.candidate.value not in before
    for source in (a, second_source):
        consent(ledger, source)
    assert gate.reconcile()
    app.review(
        first,
        decision_id="decision-first",
        review_version="local-review-v1",
        approve=True,
    )
    app.review(
        second,
        decision_id="decision-second",
        review_version="local-review-v1",
        approve=True,
    )
    runtime.submit(principal("b"), "after-review", "Что следует обсудить?")
    request = fake.calls[1].request.input_text
    shared_text = [
        row["text"] for row in json.loads(request)["shared_intellectual_state"]
    ]
    assert set(shared_text) == {
        first.proposal.candidate.value,
        second.proposal.candidate.value,
    }
    for forbidden in (canary, a, second_source, "participant-a", "decision-first"):
        assert forbidden not in request
    with events._open() as db:
        reply_id = db.execute(
            "SELECT operation_id FROM protected_reply_shared WHERE shared_operation=?",
            (first.proposal.operation_id,),
        ).fetchone()[0]
    assert (
        DeletionCoordinator(ledger, EventStoreErasureAdapter(events))
        .delete(deletion_context(), deletion_request(content_reference=a))
        .disposition.value
        == "applied"
    )
    assert gate.reconcile()
    assert [record.text for record in app.context(principal("b"))] == [
        second.proposal.candidate.value
    ]
    assert AgentOutputStore(events).read_reply(principal("b"), reply_id) is None


def test_rejection_revocation_and_restore_quarantine(shared, tmp_path):
    events, ledger, gate, a, _ = shared
    item = candidate(a)
    app = SharedIntelligence(events, gate, clock=lambda: NOW)
    assert (
        app.review(
            item,
            decision_id="decision-reject",
            review_version="local-review-v1",
            approve=False,
        )
        == "rejected"
    )
    assert app.context(principal("b")) == ()
    with pytest.raises(ValueError, match="conflict"):
        app.review(
            item,
            decision_id="decision-approve",
            review_version="local-review-v1",
            approve=True,
        )
    approved = replace(
        item,
        proposal=replace(item.proposal, operation_id="reviewed-active"),
        item_id="active-item",
    )
    consent(ledger, a)
    assert gate.reconcile()
    app.review(
        approved,
        decision_id="decision-active",
        review_version="local-review-v1",
        approve=True,
    )
    fake = FakeOpenAITransport([response()])
    runtime = Stage0ConversationRuntime(
        events, gate, fake, shared_intelligence=app, clock=lambda: NOW
    )
    runtime.submit(principal("b"), "dependent", "Другая беседа")
    with events._open() as db:
        reply_id = db.execute(
            "SELECT operation_id FROM protected_reply_shared WHERE shared_operation=?",
            ("reviewed-active",),
        ).fetchone()[0]
    backup = tmp_path / "before-review-revocation.db"
    create_sqlite_backup(events.database_path, backup, 13, NOW)
    assert app.revoke_review("reviewed-active", "revoke-active") == "revoked"
    assert app.revoke_review("reviewed-active", "revoke-active") == "revoked"
    with pytest.raises(ValueError, match="conflict"):
        app.revoke_review("reviewed-active", "revoke-other")
    assert app.context(principal("b")) == ()
    assert AgentOutputStore(events).read_reply(principal("b"), reply_id) is None
    restored = QuarantinedRestore(ledger, 13, EventStoreRestoreAdapter).restore(
        backup, tmp_path / "restored.db", NOW
    )
    assert restored.activation_eligible
    reopened = EventStore(events.database_path)
    reopened_gate = PrivacyReadGate(ledger, reopened)
    assert reopened_gate.reconcile()
    assert (
        SharedIntelligence(reopened, reopened_gate, clock=lambda: NOW).context(
            principal("b")
        )
        == ()
    )


def test_local_cli_inspection_confirmation_and_revocation(shared, monkeypatch, capsys):
    events, ledger, gate, a, _ = shared
    item = candidate(a)
    app = SharedIntelligence(events, gate, clock=lambda: NOW)
    app.quarantine(item)
    consent(ledger, a)
    assert gate.reconcile()
    args = [
        "--events",
        str(events.database_path),
        "--ledger",
        str(ledger.path),
        "--operation-id",
        item.proposal.operation_id,
    ]
    assert review_main(["inspect", *args]) == "quarantined"
    output = capsys.readouterr().out
    assert item.proposal.candidate.value in output
    assert "Synthetic A asks" in output
    monkeypatch.setattr("builtins.input", lambda _prompt: "WRONG")
    with pytest.raises(ValueError, match="confirmation"):
        review_main(["approve", *args, "--decision-id", "cli-approval"])
    monkeypatch.setattr("builtins.input", lambda _prompt: "APPROVE shared-a")
    assert (
        review_main(["approve", *args, "--decision-id", "cli-approval"]) == "shared-a"
    )
    assert app.context(principal("b"))[0].text == item.proposal.candidate.value
    assert (
        review_main(["revoke", *args, "--decision-id", "cli-revocation"]) == "revoked"
    )
    assert app.context(principal("b")) == ()


def test_consent_revocation_and_source_deletion_invalidate_reviewed_sharing(
    shared, monkeypatch
):
    events, ledger, _, a, _ = shared
    current = [NOW]
    gate = PrivacyReadGate(ledger, events, clock=lambda: current[0])
    item = candidate(a)
    assert (
        ledger.active_shared_consent("participant-a", a, "participant-b", NOW) is None
    )

    service, control, grant = consent(ledger, a)
    assert (
        ledger.active_shared_consent("participant-a", a, "participant-b", NOW)
        == grant.consent_id
    )
    assert (
        ledger.active_shared_consent(
            "participant-a", a, "participant-b", NOW - timedelta(days=1)
        )
        is None
    )
    assert gate.reconcile()
    app = SharedIntelligence(events, gate, clock=lambda: NOW)
    app.review(
        item, decision_id="decision-a", review_version="local-review-v1", approve=True
    )
    fake = FakeOpenAITransport([response()])
    Stage0ConversationRuntime(
        events, gate, fake, shared_intelligence=app, clock=lambda: NOW
    ).submit(principal("b"), "dependent", "Обсудим это")
    with events._open() as db:
        reply_id = db.execute(
            "SELECT operation_id FROM protected_reply_shared"
        ).fetchone()[0]
    assert service.revoke(
        control,
        consent_id=grant.consent_id,
        operation_id="revoke-consent-a",
        revoked_at=NOW + timedelta(minutes=2),
        correlation_id="revoke-correlation-a",
    ).created
    assert (
        ledger.active_shared_consent("participant-a", a, "participant-b", NOW)
        == grant.consent_id
    )
    assert (
        ledger.active_shared_consent(
            "participant-a", a, "participant-b", NOW + timedelta(minutes=3)
        )
        is None
    )
    with monkeypatch.context() as patcher:
        patcher.setattr(
            EventStoreErasureAdapter,
            "apply_revocation",
            lambda _self, _consent_id, _revoked_at, _scope=None: False,
        )
        assert gate.reconcile()
    assert app.context(principal("b"))[0].text == item.proposal.candidate.value
    assert AgentOutputStore(events).read_reply(principal("b"), reply_id) is not None
    with pytest.raises(PrivacyReadBlocked, match="privacy_reconciliation_required"):
        gate.authorized(lambda: current.__setitem__(0, NOW + timedelta(minutes=2)))
    with pytest.raises(PrivacyReadBlocked, match="privacy_reconciliation_required"):
        app.context(principal("b"))
    with monkeypatch.context() as patcher:
        patcher.setattr(
            EventStoreErasureAdapter,
            "apply_revocation",
            lambda _self, _consent_id, _revoked_at, _scope=None: False,
        )
        assert gate.reconcile() is False
    assert gate.reconcile()
    assert app.context(principal("b")) == ()
    assert AgentOutputStore(events).read_reply(principal("b"), reply_id) is None
    assert (
        DeletionCoordinator(ledger, EventStoreErasureAdapter(events))
        .delete(deletion_context(), deletion_request(content_reference=a))
        .disposition.value
        == "applied"
    )
    assert gate.reconcile()
    assert app.context(principal("b")) == ()
    assert (
        ledger.active_shared_consent("participant-a", a, "participant-b", NOW) is None
    )


@pytest.mark.parametrize("expiry_point", ["after_model", "after_reply_commit"])
def test_consent_expiry_before_reply_persistence_or_release(shared, expiry_point):
    events, ledger, gate, a, _ = shared
    item = candidate(a)
    _, _, grant = consent(ledger, a)
    assert gate.reconcile()
    current = [NOW]
    app = SharedIntelligence(events, gate, clock=lambda: current[0])
    app.review(
        item,
        decision_id="decision-expiry",
        review_version="local-review-v1",
        approve=True,
    )
    fake = FakeOpenAITransport([response()])

    def advance(point):
        if point == expiry_point:
            current[0] = grant.expires_at

    runtime = Stage0ConversationRuntime(
        events,
        gate,
        fake,
        shared_intelligence=app,
        clock=lambda: current[0],
        checkpoint=advance,
    )
    outcome = runtime.submit(principal("b"), "expiry-turn", "Обсудим это")
    assert outcome.model_state.value == "unavailable"
    assert len(fake.calls) == 1
    with events._open() as db:
        reply_id = db.execute(
            "SELECT operation_id FROM protected_reply_shared WHERE shared_operation=?",
            (item.proposal.operation_id,),
        ).fetchone()[0]
        persisted = db.execute(
            "SELECT 1 FROM agent_reply_payloads WHERE operation_id=?", (reply_id,)
        ).fetchone()
        assert (persisted is not None) is (expiry_point == "after_reply_commit")
        assert (
            db.execute(
                "SELECT 1 FROM agent_reply_resolutions WHERE operation_id=? AND outcome='completed'",
                (reply_id,),
            ).fetchone()
            is None
        )
    assert all(entry.role != "agent" for entry in runtime.history(principal("b")))
    restarted = Stage0ConversationRuntime(
        events, gate, FakeOpenAITransport([]), clock=lambda: current[0]
    )
    assert all(entry.role != "agent" for entry in restarted.history(principal("b")))


def test_review_rejects_wrong_audience_changed_source_and_stale_revision(shared):
    events, ledger, gate, a, b = shared
    item = candidate(a)
    app = SharedIntelligence(events, gate, clock=lambda: NOW)
    consent(ledger, a)
    assert gate.reconcile()
    wider = replace(item, audience=("participant-a", "participant-b"))
    with pytest.raises(ValueError, match="consent"):
        app.review(
            wider,
            decision_id="wider-decision",
            review_version="local-review-v1",
            approve=True,
        )
    with pytest.raises(ValueError, match="conflict"):
        app.quarantine(item)
    exact = replace(
        wider,
        proposal=replace(wider.proposal, operation_id="exact-operation"),
        item_id="exact-item",
    )
    consent(ledger, a, "participant-a")
    assert gate.reconcile()
    app.review(
        exact,
        decision_id="exact-decision",
        review_version="local-review-v1",
        approve=True,
    )
    changed = replace(
        exact, proposal=replace(exact.proposal, protected_source_event_ids=(a, b))
    )
    with pytest.raises(ValueError):
        app.quarantine(changed)
    stale = replace(
        exact, proposal=replace(exact.proposal, operation_id="stale-operation")
    )
    with pytest.raises(ValueError, match="revision"):
        app.review(
            stale,
            decision_id="stale-decision",
            review_version="local-review-v1",
            approve=True,
        )


@pytest.mark.parametrize(
    "value",
    (
        "Contact private-person@example.org for details.",
        'The source said "my unique childhood example".',
        "A's event-a should be shared.",
    ),
)
def test_known_private_markers_cannot_pass_review(shared, value):
    events, ledger, gate, a, _ = shared
    item = candidate(a, value)
    app = SharedIntelligence(events, gate, clock=lambda: NOW)
    if "event-a" in value:
        with pytest.raises(ValueError, match="screening evidence"):
            app.quarantine(item)
    else:
        assert app.quarantine(item) != "shared_commit_disabled"
    consent(ledger, a)
    assert gate.reconcile()
    with pytest.raises(ValueError, match="screening evidence|privacy screen"):
        app.review(
            item,
            decision_id="bad-review",
            review_version="local-review-v1",
            approve=True,
        )
    assert app.context(principal("b")) == ()


def test_uncertain_screen_and_forged_approval_cannot_promote(shared):
    events, _, gate, a, _ = shared
    item = candidate(a)
    app = SharedIntelligence(
        events,
        gate,
        clock=lambda: NOW,
        privacy_screen=lambda _candidate, _sources: PrivacyScreenResult(
            "abstract-privacy-screen-v1", PrivacyScreenDecision.UNCERTAIN, "uncertain"
        ),
    )
    assert app.quarantine(item) == "privacy_uncertain"
    with pytest.raises(ValueError, match="privacy screen"):
        app.review(
            item,
            decision_id="uncertain-review",
            review_version="local-review-v1",
            approve=True,
        )
    assert app.context(principal("b")) == ()
    forged = approval(a, value=item.proposal.candidate.value)
    synthetic = SharedIntelligence(events, gate, approvals=(forged,), clock=lambda: NOW)
    with pytest.raises(ValueError):
        synthetic.commit(forged)


def test_permissive_h_result_does_not_override_deterministic_screen(shared):
    events, ledger, gate, a, _ = shared
    item = candidate(a, "Send details to private-person@example.org.")
    app = SharedIntelligence(
        events,
        gate,
        clock=lambda: NOW,
        privacy_screen=lambda _candidate, _sources: PrivacyScreenResult(
            "abstract-privacy-screen-v1", PrivacyScreenDecision.ALLOW, "allowed"
        ),
    )
    assert app.quarantine(item) == "privacy_rejected"
    consent(ledger, a)
    assert gate.reconcile()
    with pytest.raises(ValueError, match="privacy screen"):
        app.review(
            item,
            decision_id="permissive-review",
            review_version="local-review-v1",
            approve=True,
        )
    assert app.context(principal("b")) == ()


def test_old_quarantine_cannot_be_reapproved_after_restore(shared, tmp_path):
    events, ledger, gate, a, _ = shared
    item = candidate(a)
    app = SharedIntelligence(events, gate, clock=lambda: NOW)
    app.quarantine(item)
    backup = tmp_path / "quarantined-before-review.db"
    create_sqlite_backup(events.database_path, backup, 13, NOW)
    restored_path = tmp_path / "restored-quarantine.db"
    assert (
        QuarantinedRestore(ledger, 13, EventStoreRestoreAdapter)
        .restore(backup, restored_path, NOW)
        .activation_eligible
    )
    with sqlite3.connect(f"file:{restored_path}?immutable=1", uri=True) as db:
        assert db.execute(
            "SELECT status FROM shared_operations WHERE operation_id=?",
            (item.proposal.operation_id,),
        ).fetchone() == ("invalidated",)


def test_forged_active_status_and_interrupted_review_fail_closed(shared):
    events, ledger, gate, a, _ = shared
    item = candidate(a)
    app = SharedIntelligence(events, gate, clock=lambda: NOW)
    app.quarantine(item)
    with events._open() as db:
        db.execute(
            "UPDATE shared_operations SET status='active',reason='approved' WHERE operation_id=?",
            (item.proposal.operation_id,),
        )
    with pytest.raises(ValueError, match="invalid_protected_shared_graph"):
        app.context(principal("b"))
    with events._open() as db:
        db.execute(
            "UPDATE shared_operations SET status='quarantined',reason='shared_commit_disabled' WHERE operation_id=?",
            (item.proposal.operation_id,),
        )
    consent(ledger, a)
    assert gate.reconcile()

    def crash(point):
        if point == "before_shared_commit":
            raise RuntimeError("synthetic review crash")

    interrupted = SharedIntelligence(events, gate, clock=lambda: NOW, checkpoint=crash)
    with pytest.raises(RuntimeError, match="synthetic review crash"):
        interrupted.review(
            item,
            decision_id="crash-decision",
            review_version="local-review-v1",
            approve=True,
        )
    with events._open() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM reviewed_shared_decisions"
        ).fetchone() == (0,)
    reopened = EventStore(events.database_path)
    reopened_gate = PrivacyReadGate(ledger, reopened)
    assert reopened_gate.reconcile()
    restarted = SharedIntelligence(reopened, reopened_gate, clock=lambda: NOW)
    assert restarted.context(principal("b")) == ()
    assert (
        restarted.review(
            item,
            decision_id="crash-decision",
            review_version="local-review-v1",
            approve=True,
        )
        == item.proposal.operation_id
    )
    assert restarted.context(principal("b"))[0].text == item.proposal.candidate.value
    second = replace(
        item,
        proposal=replace(item.proposal, operation_id="after-commit-operation"),
        item_id="after-commit-item",
    )

    def after_commit(point):
        if point == "after_shared_commit":
            raise RuntimeError("synthetic lost review result")

    uncertain = SharedIntelligence(
        reopened, reopened_gate, clock=lambda: NOW, checkpoint=after_commit
    )
    with pytest.raises(RuntimeError, match="lost review result"):
        uncertain.review(
            second,
            decision_id="after-commit-decision",
            review_version="local-review-v1",
            approve=True,
        )
    assert (
        restarted.review(
            second,
            decision_id="after-commit-decision",
            review_version="local-review-v1",
            approve=True,
        )
        == "after-commit-operation"
    )


def test_review_boundary_rejects_invalid_shapes_stale_screen_and_missing_authorization(
    shared,
):
    events, ledger, gate, a, _ = shared
    item = candidate(a)
    app = SharedIntelligence(events, gate, clock=lambda: NOW)
    with pytest.raises(ValueError, match="reviewed candidate"):
        app.quarantine(object())
    with pytest.raises(ValueError, match="review decision"):
        app.review(
            item, decision_id="bad", review_version="local-review-v1", approve="yes"
        )
    app.quarantine(item)
    with events._open() as db:
        db.execute(
            "UPDATE shared_operations SET screen_result='privacy_rejected' WHERE operation_id=?",
            (item.proposal.operation_id,),
        )
    consent(ledger, a)
    assert gate.reconcile()
    with pytest.raises(ValueError, match="privacy screen"):
        app.review(
            item,
            decision_id="tampered-screen",
            review_version="local-review-v1",
            approve=True,
        )
    with events._open() as db:
        db.execute(
            "UPDATE shared_operations SET screen_result='shared_commit_disabled' WHERE operation_id=?",
            (item.proposal.operation_id,),
        )

    def stale_screen(point):
        if point == "after_shared_screen":
            with events._open() as db:
                db.execute(
                    "UPDATE shared_operations SET screen_version='old-screen' WHERE operation_id=?",
                    (item.proposal.operation_id,),
                )

    interrupted = SharedIntelligence(
        events, gate, clock=lambda: NOW, checkpoint=stale_screen
    )
    with pytest.raises(ValueError, match="reviewed quarantine"):
        interrupted.review(
            item,
            decision_id="stale-screen",
            review_version="local-review-v1",
            approve=False,
        )
    with pytest.raises(ValueError, match="active reviewed"):
        app.revoke_review(item.proposal.operation_id, "no-approval")


def test_expired_consent_is_not_retrieved(shared):
    events, ledger, gate, a, _ = shared
    item = candidate(a)
    consent(ledger, a)
    assert gate.reconcile()
    tick = [NOW]
    app = SharedIntelligence(events, gate, clock=lambda: tick[0])
    app.review(
        item, decision_id="decision-a", review_version="local-review-v1", approve=True
    )
    assert app.context(principal("b"))
    tick[0] = NOW + timedelta(days=731)
    assert app.context(principal("b")) == ()


def test_local_cli_denials_and_module_entry(shared, monkeypatch, capsys):
    events, ledger, gate, a, _ = shared
    args = [
        "--events",
        str(events.database_path),
        "--ledger",
        str(ledger.path),
        "--operation-id",
        "shared-a",
    ]
    with pytest.raises(ValueError, match="unavailable"):
        review_main(["inspect", *args])
    synthetic = approval(a)
    SharedIntelligence(events, gate, approvals=(synthetic,), clock=lambda: NOW).commit(
        synthetic
    )
    with pytest.raises(ValueError, match="reviewed candidate"):
        review_main(["inspect", *args])
    other = candidate(a)
    other = replace(
        other, proposal=replace(other.proposal, operation_id="reviewed-other")
    )
    SharedIntelligence(events, gate, clock=lambda: NOW).quarantine(other)
    args[-1] = "reviewed-other"
    with pytest.raises(ValueError, match="decision identity"):
        review_main(["reject", *args])
    with pytest.raises(ValueError, match="revocation identity"):
        review_main(["revoke", *args])
    monkeypatch.setattr("builtins.input", lambda _prompt: "REJECT reviewed-other")
    assert (
        review_main(["reject", *args, "--decision-id", "cli-rejection"]) == "rejected"
    )
    monkeypatch.setattr(PrivacyReadGate, "reconcile", lambda _gate: False)
    with pytest.raises(ValueError, match="reconciliation"):
        review_main(["inspect", *args])
    monkeypatch.setattr(sys, "argv", ["review_cli", "inspect", *args])
    with pytest.raises(ValueError, match="reconciliation"):
        runpy.run_module("app.cognition.review_cli", run_name="__main__")
    capsys.readouterr()


def test_v10_to_v11_migration_is_atomic_and_historical_backup_stays_readable(
    shared, tmp_path
):
    events, ledger, gate, a, _ = shared
    synthetic = approval(a)
    SharedIntelligence(events, gate, approvals=(synthetic,), clock=lambda: NOW).commit(
        synthetic
    )
    with sqlite3.connect(events.database_path) as db:
        remove_v11(db)
        db.execute("UPDATE schema_metadata SET version=10")
        db.execute("PRAGMA user_version=10")
        original = schema_objects(db)
    backup = tmp_path / "historical-v10.db"
    create_sqlite_backup(events.database_path, backup, 10, NOW)
    service, control, grant = consent(ledger, a)
    assert service.revoke(
        control,
        consent_id=grant.consent_id,
        operation_id="historical-revocation",
        revoked_at=NOW,
        correlation_id="historical-revocation-correlation",
    ).created
    assert (
        QuarantinedRestore(ledger, 10, EventStoreRestoreAdapter)
        .restore(backup, tmp_path / "historical-v10-restored.db", NOW)
        .activation_eligible
    )

    def crash(point):
        if point is FaultPoint.BEFORE_V11_MIGRATION_COMMIT:
            raise RuntimeError("synthetic v11 migration crash")

    with pytest.raises(RuntimeError, match="v11 migration crash"):
        EventStore(events.database_path, fault_injector=crash)
    with sqlite3.connect(events.database_path) as db:
        assert schema_objects(db) == original
        assert db.execute("PRAGMA user_version").fetchone() == (10,)
        assert db.execute(
            "SELECT event_id FROM event_envelopes WHERE event_id=?", (a,)
        ).fetchone() == (a,)
        erase_shared_source(db, a)
        validate_shared_graph(db)
        assert db.execute("SELECT COUNT(*) FROM shared_payloads").fetchone() == (0,)
    upgraded = EventStore(events.database_path)
    assert upgraded.diagnostics().schema_version == 13


def test_authenticated_a_b_reviewed_projection_reaches_fake_sdk(tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    auth, events, ledger, gate, web, a, b = worker_app(tmp_path, socket_path)
    parent, child = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=worker_process,
        args=(str(socket_path), str(tmp_path / "worker-provider.db"), child, 3),
        kwargs={"sdk": True},
    )
    process.start()
    child.close()
    try:
        assert parent.poll(5) and parent.recv() is True
        canary = "PRIVATE-A-SDK-CANARY-42"
        assert (
            json.loads(a.submit("a-source", "A's private story " + canary)[2])[
                "model_state"
            ]
            == "completed"
        )
        a_context = auth.authenticate(a.cookies["ar_session"]).trusted_context
        source = gate.retrieve_private_events(a_context)[0].event_id
        item = replace(candidate(source), principal=a_context)
        shared = SharedIntelligence(events, gate, clock=lambda: NOW)
        shared.quarantine(item)
        web._conversations = WorkerConversationRuntime(
            events,
            gate,
            OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
            provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
            shared_intelligence=shared,
            clock=lambda: NOW,
        )
        assert (
            json.loads(b.submit("b-before", "Какие идеи есть?")[2])["model_state"]
            == "completed"
        )
        consent(ledger, source)
        assert gate.reconcile()
        shared.review(
            item,
            decision_id="sdk-review",
            review_version="local-review-v1",
            approve=True,
        )
        assert (
            json.loads(b.submit("b-after", "Обсудим решение")[2])["model_state"]
            == "completed"
        )
        assert parent.poll(5)
        a_input, before_input, after_input = parent.recv()
        assert canary in a_input
        assert item.proposal.candidate.value not in before_input
        assert item.proposal.candidate.value in after_input
        for forbidden in (canary, source, "participant-a", "sdk-review", "Synthetic A"):
            assert forbidden not in after_input
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()


def test_reviewed_revision_preserves_counterevidence_and_current_head(shared):
    events, ledger, gate, a, b = shared
    first = candidate(a)
    consent(ledger, a)
    consent(ledger, b, principal_id="participant-b")
    assert gate.reconcile()
    app = SharedIntelligence(events, gate, clock=lambda: NOW)
    app.review(
        first,
        decision_id="first-decision",
        review_version="local-review-v1",
        approve=True,
    )
    response = approval(
        b,
        op="reviewed-counter",
        revision=1,
        author="b",
        counter=(b,),
        value="Independent viewpoints can reveal omitted alternatives.",
    )
    second = ReviewedSharedCandidate(
        response.principal,
        response.proposal,
        response.item_id,
        response.expected_revision,
        ("participant-b",),
        response.counter_event_ids,
    )
    app.review(
        second,
        decision_id="counter-decision",
        review_version="local-review-v1",
        approve=True,
    )
    context = app.context(principal("b"))
    assert len(context) == 1
    assert context[0].revision == 2
    assert context[0].text == second.proposal.candidate.value
    assert context[0].counterevidence_count == 1
    assert context[0].memory.alternatives == (first.proposal.candidate.value,)
    with events._open() as db:
        assert db.execute(
            "SELECT event_id,role,classification FROM protected_shared_sources "
            "WHERE operation_id=? ORDER BY event_id",
            (second.proposal.operation_id,),
        ).fetchall() == [(a, "inherited", "supporting"), (b, "counter", "counter")]


def test_expired_older_revision_is_absent_from_current_alternatives(shared):
    events, ledger, gate, a, b = shared
    first = candidate(a)
    source_grants = (
        consent(ledger, a),
        consent(ledger, b, principal_id="participant-b"),
    )
    assert gate.reconcile()
    current = [NOW]
    app = SharedIntelligence(events, gate, clock=lambda: current[0])
    app.review(
        first,
        decision_id="first-expiry-decision",
        review_version="local-review-v1",
        approve=True,
    )
    current[0] = NOW + timedelta(days=731)
    for service, control, old in source_grants:
        renewed = replace(
            old,
            consent_id=old.consent_id + "-renewed",
            operation_id=old.operation_id + "-renewed",
            granted_at=NOW + timedelta(days=729),
            expires_at=NOW + timedelta(days=1000),
            revision=2,
            correlation_id=old.correlation_id + "-renewed",
        )
        assert service.grant(control, renewed).created
    assert gate.reconcile()
    newer = approval(
        b,
        op="reviewed-new-consent",
        revision=1,
        author="b",
        counter=(b,),
        value="Independent viewpoints can reveal omitted alternatives.",
    )
    second = ReviewedSharedCandidate(
        newer.principal,
        newer.proposal,
        newer.item_id,
        newer.expected_revision,
        ("participant-b",),
        newer.counter_event_ids,
    )
    app.review(
        second,
        decision_id="second-expiry-decision",
        review_version="local-review-v1",
        approve=True,
    )
    context = app.context(principal("b"))
    assert len(context) == 1
    assert context[0].text == second.proposal.candidate.value
    assert context[0].memory.alternatives == ()
    fake = FakeOpenAITransport([response()])
    Stage0ConversationRuntime(
        events, gate, fake, shared_intelligence=app, clock=lambda: current[0]
    ).submit(principal("b"), "renewed-context", "Обсудим идеи")
    assert first.proposal.candidate.value not in fake.calls[0].request.input_text
    assert second.proposal.candidate.value in fake.calls[0].request.input_text


def test_research_does_not_persist_after_shared_consent_expires(shared, tmp_path):
    events, ledger, gate, a, _ = shared
    item = candidate(a)
    _, _, grant = consent(ledger, a)
    assert gate.reconcile()
    current = [NOW]
    app = SharedIntelligence(events, gate, clock=lambda: current[0])
    app.review(
        item,
        decision_id="research-expiry-decision",
        review_version="local-review-v1",
        approve=True,
    )
    request = ResearchWorkerRequest(FIXTURE, "0" * 64)
    bridge = SyntheticResearchBridge(
        events,
        gate,
        SyntheticResearchClient(
            tmp_path / "unused-research.sock",
            (request,),
            worker_state=ProviderStateStore(events),
        ),
        enabled=True,
        clock=lambda: NOW,
    )

    def expire(point):
        if point == "after_model":
            event = gate.retrieve_private_events(principal("b"))[-1]
            bridge.authorize_input(principal("b"), event.event_id, FIXTURE)
            current[0] = grant.expires_at

    fake = FakeOpenAITransport([response("research", FIXTURE.value)])
    result = Stage0ConversationRuntime(
        events,
        gate,
        fake,
        shared_intelligence=app,
        research_bridge=bridge,
        clock=lambda: current[0],
        checkpoint=expire,
    ).submit(principal("b"), "expired-research", "Исследуй этот вопрос")
    assert result.model_state.value == "unavailable"
    assert len(fake.calls) == 1
    with events._open() as db:
        reply_id = db.execute(
            "SELECT operation_id FROM protected_reply_shared WHERE shared_operation=?",
            (item.proposal.operation_id,),
        ).fetchone()[0]
        assert (
            db.execute(
                "SELECT 1 FROM agent_reply_payloads WHERE operation_id=?", (reply_id,)
            ).fetchone()
            is None
        )
    assert not ProviderStateStore(events).attempt_exists(started_id(reply_id))


def test_reviewed_alternative_is_bound_behind_synthetic_head(shared):
    events, ledger, gate, a, b = shared
    reviewed = candidate(a)
    _, _, grant = consent(ledger, a)
    assert gate.reconcile()
    synthetic = replace(
        approval(
            b,
            op="synthetic-after-review",
            revision=1,
            author="b",
            counter=(b,),
            value="Consider competing explanations of uncertainty.",
        ),
        audience=("participant-b",),
    )
    current = [NOW]
    app = SharedIntelligence(
        events, gate, approvals=(synthetic,), clock=lambda: current[0]
    )
    app.review(
        reviewed,
        decision_id="historical-review-decision",
        review_version="local-review-v1",
        approve=True,
    )
    assert app.commit(synthetic) == synthetic.proposal.operation_id
    assert app.context(principal("b"))[0].memory.alternatives == (
        reviewed.proposal.candidate.value,
    )

    def expire(point):
        if point == "after_model":
            current[0] = grant.expires_at

    fake = FakeOpenAITransport([response()])
    result = Stage0ConversationRuntime(
        events,
        gate,
        fake,
        shared_intelligence=app,
        clock=lambda: current[0],
        checkpoint=expire,
    ).submit(principal("b"), "historical-dependent", "Обсудим идеи")
    assert reviewed.proposal.candidate.value in fake.calls[0].request.input_text
    assert result.model_state.value == "unavailable"
    with events._open() as db:
        reply_id = db.execute(
            "SELECT operation_id FROM protected_reply_shared WHERE shared_operation=?",
            (synthetic.proposal.operation_id,),
        ).fetchone()[0]
        assert db.execute(
            "SELECT shared_operation FROM protected_reply_shared "
            "WHERE operation_id=? ORDER BY shared_operation",
            (reply_id,),
        ).fetchall() == sorted(
            [
                (reviewed.proposal.operation_id,),
                (synthetic.proposal.operation_id,),
            ]
        )
        assert (
            db.execute(
                "SELECT 1 FROM agent_reply_payloads WHERE operation_id=?", (reply_id,)
            ).fetchone()
            is None
        )


def test_reviewed_historical_dependencies_are_closed(shared):
    events, ledger, gate, a, _ = shared
    consent(ledger, a)
    assert gate.reconcile()
    app = SharedIntelligence(events, gate, clock=lambda: NOW)
    app.review(
        candidate(a),
        decision_id="dependency-shape-decision",
        review_version="local-review-v1",
        approve=True,
    )
    record = app.context(principal("b"))[0]
    for invalid in ([], (object(),), ("two", "one"), ("same", "same"), ("",)):
        with pytest.raises(ValueError):
            replace(record, historical_reviewed_operation_ids=invalid)


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("admission_change", ["none", "expired", "superseded"])
def test_worker_rechecks_bound_shared_consent_before_ipc_reservation(
    shared, tmp_path, monkeypatch, historical, admission_change
):
    events, ledger, gate, a, b = shared
    reviewed = candidate(a)
    service, control, grant = consent(ledger, a)
    assert gate.reconcile()
    synthetic = replace(
        approval(
            b,
            op="synthetic-worker-head",
            revision=1,
            author="b",
            counter=(b,),
            value="Consider competing explanations of uncertainty.",
        ),
        audience=("participant-b",),
    )
    current = [NOW]
    intelligence = SharedIntelligence(
        events,
        gate,
        approvals=(synthetic,) if historical else (),
        clock=lambda: current[0],
    )
    intelligence.review(
        reviewed,
        decision_id="worker-admission-review",
        review_version="local-review-v1",
        approve=True,
    )
    if historical:
        intelligence.commit(synthetic)
    ipc_store = EventStore(tmp_path / "ipc-state.db")
    calls = []

    def infer(_client, request, *, correlation_id, operation_id):
        calls.append(request)
        return ClientResult(
            operation_id,
            correlation_id,
            "completed",
            1,
            None,
            ProposalValidation(ReplyProposal("Синтетический русский ответ."), None),
        )

    monkeypatch.setattr(OpenAIWorkerClient, "infer", infer)

    def advance(point):
        if point == "before_worker_admission":
            if admission_change == "expired":
                current[0] = grant.expires_at
            elif admission_change == "superseded":
                renewed = replace(
                    grant,
                    consent_id=grant.consent_id + "-renewed",
                    operation_id=grant.operation_id + "-renewed",
                    revision=2,
                    correlation_id=grant.correlation_id + "-renewed",
                )
                assert service.grant(control, renewed).created
                assert gate.reconcile()

    runtime = WorkerConversationRuntime(
        events,
        gate,
        OpenAIWorkerClient(
            tmp_path / "unused-worker.sock", expected_worker_uid=os.getuid()
        ),
        provider_state=ProviderStateStore(ipc_store),
        shared_intelligence=intelligence,
        clock=lambda: current[0],
        checkpoint=advance,
    )
    result = runtime.submit(principal("b"), "worker-admission", "Обсудим идеи")
    blocked = admission_change != "none"
    assert result.model_state.value == ("unavailable" if blocked else "completed")
    assert len(calls) == (0 if blocked else 1)
    if calls:
        assert reviewed.proposal.candidate.value in calls[0].serialize()
    with events._open() as db:
        reply_id = db.execute(
            "SELECT operation_id FROM protected_reply_shared WHERE shared_operation=?",
            (reviewed.proposal.operation_id,),
        ).fetchone()[0]
        dependencies = db.execute(
            "SELECT shared_operation FROM protected_reply_shared WHERE operation_id=?",
            (reply_id,),
        ).fetchall()
        assert {row[0] for row in dependencies} == (
            {reviewed.proposal.operation_id, synthetic.proposal.operation_id}
            if historical
            else {reviewed.proposal.operation_id}
        )
        assert db.execute(
            "SELECT COUNT(*) FROM agent_reply_payloads WHERE operation_id=?",
            (reply_id,),
        ).fetchone() == (0 if blocked else 1,)
        assert db.execute(
            "SELECT COUNT(*) FROM agent_reply_resolutions "
            "WHERE operation_id=? AND outcome='completed'",
            (reply_id,),
        ).fetchone() == (0 if blocked else 1,)
    with ipc_store._open() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM provider_attempt_reservations WHERE attempt_id=?",
            ("ipc:" + reply_id,),
        ).fetchone() == (0 if blocked else 1,)


def test_worker_rechecks_consent_inherited_through_prior_reply(
    shared, tmp_path, monkeypatch
):
    events, ledger, gate, a, _ = shared
    reviewed = candidate(a)
    _, _, grant = consent(ledger, a)
    assert gate.reconcile()
    current = [NOW]
    intelligence = SharedIntelligence(events, gate, clock=lambda: current[0])
    intelligence.review(
        reviewed,
        decision_id="inherited-admission-review",
        review_version="local-review-v1",
        approve=True,
    )
    ipc_store = EventStore(tmp_path / "ipc-state.db")
    calls = []

    def infer(_client, request, *, correlation_id, operation_id):
        calls.append(request)
        return ClientResult(
            operation_id,
            correlation_id,
            "completed",
            1,
            None,
            ProposalValidation(ReplyProposal("Синтетический русский ответ."), None),
        )

    monkeypatch.setattr(OpenAIWorkerClient, "infer", infer)
    runtime = WorkerConversationRuntime(
        events,
        gate,
        OpenAIWorkerClient(
            tmp_path / "unused-worker.sock", expected_worker_uid=os.getuid()
        ),
        provider_state=ProviderStateStore(ipc_store),
        shared_intelligence=intelligence,
        clock=lambda: current[0],
    )
    assert (
        runtime.submit(
            principal("b"), "first-dependent", "Первый вопрос"
        ).model_state.value
        == "completed"
    )
    prepared = []
    prepared_reply_ids = []
    original_infer = runtime._infer

    def inspect_then_infer(context, correlation, operation_id, principal_context):
        prepared.append(context)
        prepared_reply_ids.append(operation_id)
        return original_infer(context, correlation, operation_id, principal_context)

    runtime._infer = inspect_then_infer
    runtime._checkpoint = lambda point: (
        current.__setitem__(0, grant.expires_at)
        if point == "before_worker_admission"
        else None
    )
    result = runtime.submit(principal("b"), "second-dependent", "Следующий вопрос")
    assert result.model_state.value == "unavailable"
    assert len(calls) == 1
    assert len(prepared) == 1
    assert prepared[0].private.inherited_shared_operation_ids == (
        reviewed.proposal.operation_id,
    )
    assert prepared[0].shared == ()
    reply_id = prepared_reply_ids[0]
    with events._open() as db:
        assert db.execute(
            "SELECT shared_operation FROM protected_reply_shared WHERE operation_id=?",
            (reply_id,),
        ).fetchall() == [(reviewed.proposal.operation_id,)]
        assert db.execute(
            "SELECT COUNT(*) FROM agent_reply_payloads WHERE operation_id=?",
            (reply_id,),
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM agent_reply_resolutions WHERE operation_id=? "
            "AND outcome='completed'",
            (reply_id,),
        ).fetchone() == (0,)
    with ipc_store._open() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM provider_attempt_reservations"
        ).fetchone() == (1,)


@pytest.mark.parametrize("expire_at_admission", [False, True])
def test_stage0_rechecks_bound_shared_consent_before_fake_inference(
    shared, expire_at_admission
):
    events, ledger, gate, a, _ = shared
    reviewed = candidate(a)
    _, _, grant = consent(ledger, a)
    assert gate.reconcile()
    current = [NOW]
    intelligence = SharedIntelligence(events, gate, clock=lambda: current[0])
    intelligence.review(
        reviewed,
        decision_id="stage0-admission-review",
        review_version="local-review-v1",
        approve=True,
    )
    fake = FakeOpenAITransport([response()])

    def advance(point):
        if point == "before_stage0_admission" and expire_at_admission:
            current[0] = grant.expires_at

    runtime = Stage0ConversationRuntime(
        events,
        gate,
        fake,
        shared_intelligence=intelligence,
        clock=lambda: current[0],
        checkpoint=advance,
    )
    result = runtime.submit(principal("b"), "stage0-admission", "Обсудим идеи")
    assert result.model_state.value == (
        "unavailable" if expire_at_admission else "completed"
    )
    assert len(fake.calls) == (0 if expire_at_admission else 1)
    with events._open() as db:
        reply_id = db.execute(
            "SELECT operation_id FROM protected_reply_shared WHERE shared_operation=?",
            (reviewed.proposal.operation_id,),
        ).fetchone()[0]
        assert db.execute(
            "SELECT COUNT(*) FROM agent_reply_payloads WHERE operation_id=?",
            (reply_id,),
        ).fetchone() == (0 if expire_at_admission else 1,)
        assert db.execute(
            "SELECT COUNT(*) FROM agent_reply_resolutions "
            "WHERE operation_id=? AND outcome='completed'",
            (reply_id,),
        ).fetchone() == (0 if expire_at_admission else 1,)
