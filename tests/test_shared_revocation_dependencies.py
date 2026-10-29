"""Revocation follows protected model inputs, never an operation prefix."""

from dataclasses import replace
from datetime import timedelta
import json
import sqlite3

import pytest

from app.cognition.worker_shared import SharedSuggestion
from app.core.openai_worker_client import ClientResult
from app.core.agent_output_store import AgentOutputStore
from app.core.store import EventStore
from app.core.shared_graph import validate_shared_graph
from app.cognition.shared import SharedIntelligence
from app.cognition.review_cli import _inspect
from app.private_web.runtime import Stage0ConversationRuntime
from app.trusted.openai_transport import FakeOpenAITransport
from app.privacy.application import PrivacyReadGate, EventStoreRestoreAdapter
from app.privacy.recovery import QuarantinedRestore, create_sqlite_backup
from test_cognition_pipeline import NOW, append_event, principal
from test_reviewed_shared import consent, candidate
from test_worker_shared_proposals import _direct_runtime
from test_private_conversation_runtime import response


SUGGESTION = SharedSuggestion(
    "new", None, "belief_or_hypothesis", "Compare independent explanations.", 0.5
)


def revoke(
    ledger,
    source,
    owner="participant-b",
    audience="participant-a",
    *,
    at=NOW,
    existing=None,
):
    service, actor, grant = existing or consent(ledger, source, audience, owner)
    assert service.revoke(
        actor,
        consent_id=grant.consent_id,
        operation_id="revoke-" + source,
        revoked_at=at,
        correlation_id="revocation-" + source,
    ).created


@pytest.mark.parametrize("parent", [False, True])
def test_unrelated_revocation_preserves_candidate_receipt_and_read_gate(
    tmp_path, monkeypatch, parent
):
    events, ledger, gate, shared, runtime, _, source, content, reply = _direct_runtime(
        tmp_path, monkeypatch, SUGGESTION
    )
    if parent:
        item = candidate(source)
        shared.quarantine(item)
        consent(ledger, source)
        assert gate.reconcile()
        shared.review(
            item,
            decision_id="approve-parent",
            review_version="local-review-v1",
            approve=True,
        )

        def revise(request, **kw):
            return ClientResult(
                kw["operation_id"],
                kw["correlation_id"],
                "completed",
                1,
                None,
                shared=replace(
                    SUGGESTION, decision="revise", handle=request.items[0].handle
                ),
            )

        monkeypatch.setattr(runtime._worker, "infer", revise)
        content = "Further private observations about uncertainty."
        source = append_event(events, principal("a"), "child", content)
        reply = runtime._operation_id(source)
    runtime._propose_shared(principal("a"), source, content, "corr", reply)
    audit = runtime._shared_proposal_audit
    audit.reserve("sharedgen:pending-independent", principal("a"), source, content)
    other = append_event(
        events, principal("b"), "unrelated", "Independent private source."
    )
    revoke(ledger, other)
    assert gate.reconcile()
    with events._open() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_operations").fetchone() == (
            2 if parent else 1,
        )
        assert db.execute(
            "SELECT COUNT(*) FROM shared_operations WHERE status='invalidated'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM private_extraction_receipts"
        ).fetchone() == (1,)
    assert bool(shared.context(principal("b"))) is parent
    assert gate.reconcile()
    audit.reserve("sharedgen:later-independent", principal("a"), source, content)
    assert gate.reconcile()
    with events._open() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM private_extraction_receipts"
        ).fetchone() == (2,)


def parent_setup(tmp_path, monkeypatch, decision="new"):
    def suggestion(request):
        if not request.items:
            return SUGGESTION
        return replace(
            SUGGESTION,
            decision=decision,
            handle=request.items[0].handle if decision == "revise" else None,
        )

    values = _direct_runtime(tmp_path, monkeypatch, suggestion)
    events, ledger, gate, shared, runtime, _, source, *_ = values
    item = candidate(source)
    shared.quarantine(item)
    grant = consent(ledger, source)
    assert gate.reconcile()
    shared.review(
        item,
        decision_id="parent-approved",
        review_version="local-review-v1",
        approve=True,
    )
    return values, item, grant


def source_for(events, owner, name):
    content = "Private independent observations about uncertain patterns."
    return append_event(events, principal(owner), name, content), content


def propose(runtime, owner, source, content):
    reply = runtime._operation_id(source)
    runtime._propose_shared(principal(owner), source, content, "corr", reply)
    return "sharedgen:" + reply.removeprefix("reply:")


def pending(runtime, owner, source, content, operation, received=False):
    audit = runtime._shared_proposal_audit
    audit.reserve(operation, principal(owner), source, content)
    if received:
        audit.receive(operation, "participant-" + owner, source, SUGGESTION)


@pytest.mark.parametrize("decision", ["new", "revise"])
@pytest.mark.parametrize("approved", [False, True])
def test_related_consent_cascades_chain_receipts_and_replies_only(
    tmp_path, monkeypatch, decision, approved
):
    values, item, grant = parent_setup(tmp_path, monkeypatch, decision)
    events, ledger, gate, shared, runtime, *_ = values
    child_source, child_content = source_for(events, "a", "child")
    operation = propose(runtime, "a", child_source, child_content)
    if approved:
        consent(ledger, child_source)
        assert gate.reconcile()
        shared.review(
            _inspect(events, gate, operation)[1],
            decision_id="child-approved",
            review_version="local-review-v1",
            approve=True,
        )
    for received in (False, True):
        pending(
            runtime,
            "a",
            child_source,
            child_content,
            "sharedgen:dependent-" + str(received),
            received,
        )
    # Independent model, receipt, and reply controls share the same database.
    independent_source, independent_content = source_for(events, "c", "independent")
    control = propose(runtime, "c", independent_source, independent_content)
    pending(
        runtime, "c", independent_source, independent_content, "sharedgen:control", True
    )
    replies = Stage0ConversationRuntime(
        events,
        gate,
        FakeOpenAITransport([response(), response()]),
        shared_intelligence=shared,
        clock=lambda: NOW,
    )
    replies.submit(
        principal("c"), "independent-reply", "Could we discuss independent ideas?"
    )
    replies.submit(principal("b"), "dependent-reply", "Could we compare these ideas?")
    with events._open() as db:
        independent_reply = db.execute(
            "SELECT operation_id FROM agent_reply_operations WHERE audience_principal_id='participant-c'"
        ).fetchone()[0]
        dependent_reply = db.execute(
            "SELECT operation_id FROM protected_reply_shared LIMIT 1"
        ).fetchone()[0]
    backup = tmp_path / "before-consent-revocation.db"
    create_sqlite_backup(events.database_path, backup, 14, NOW)
    revoke(ledger, values[6], "participant-a", "participant-b", existing=grant)
    assert gate.reconcile()
    assert gate.reconcile()
    with events._open() as db:
        assert db.execute("SELECT operation_id FROM shared_payloads").fetchall() == [
            (control,)
        ]
        assert db.execute(
            "SELECT operation_id FROM private_extraction_receipts"
        ).fetchall() == [("sharedgen:control",)]
        assert db.execute(
            "SELECT status FROM shared_operations WHERE operation_id=?",
            (item.proposal.operation_id,),
        ).fetchone() == ("invalidated",)
    assert AgentOutputStore(events).read_reply(principal("b"), dependent_reply) is None
    assert (
        AgentOutputStore(events).read_reply(principal("c"), independent_reply)
        is not None
    )
    reopened = EventStore(events.database_path)
    restarted_gate = PrivacyReadGate(ledger, reopened, clock=lambda: NOW)
    assert restarted_gate.reconcile()
    assert (
        SharedIntelligence(reopened, restarted_gate, clock=lambda: NOW).context(
            principal("b")
        )
        == ()
    )
    # The old grant must not consume a later proposal using an independent source.
    later, text = source_for(events, "c", "later")
    later_op = propose(runtime, "c", later, text)
    assert gate.reconcile()
    with events._open() as db:
        assert db.execute(
            "SELECT status FROM shared_operations WHERE operation_id=?", (later_op,)
        ).fetchone() == ("quarantined",)
    restored = tmp_path / "restored.db"
    assert (
        QuarantinedRestore(ledger, 14, EventStoreRestoreAdapter)
        .restore(backup, restored, NOW)
        .activation_eligible
    )
    with sqlite3.connect(f"file:{restored}?immutable=1", uri=True) as db:
        assert db.execute("SELECT COUNT(*) FROM shared_payloads").fetchone() == (0,)
        assert db.execute(
            "SELECT operation_id FROM private_extraction_receipts"
        ).fetchall() == [("sharedgen:control",)]
        assert (
            db.execute(
                "SELECT 1 FROM agent_reply_payloads WHERE operation_id=?",
                (dependent_reply,),
            ).fetchone()
            is None
        )
        assert db.execute(
            "SELECT 1 FROM agent_reply_payloads WHERE operation_id=?",
            (independent_reply,),
        ).fetchone() == (1,)


@pytest.mark.parametrize("decision", ["new", "revise"])
def test_review_revocation_follows_actual_model_inputs_and_restore(
    tmp_path, monkeypatch, decision
):
    values, parent, _ = parent_setup(tmp_path, monkeypatch, decision)
    events, ledger, gate, shared, runtime, *_ = values
    source, content = source_for(events, "a", "derived")
    dependent = propose(runtime, "a", source, content)
    pending(runtime, "a", source, content, "sharedgen:pending-review")
    pending(runtime, "a", source, content, "sharedgen:received-review", True)
    control_source, control_text = source_for(events, "c", "control")
    control = propose(runtime, "c", control_source, control_text)
    pending(
        runtime, "c", control_source, control_text, "sharedgen:independent-review", True
    )
    backup = tmp_path / "before-review-revocation.db"
    create_sqlite_backup(events.database_path, backup, 14, NOW)
    assert (
        shared.revoke_review(parent.proposal.operation_id, "revoke-parent") == "revoked"
    )
    assert (
        shared.revoke_review(parent.proposal.operation_id, "revoke-parent") == "revoked"
    )
    assert gate.reconcile()
    with events._open() as db:
        assert db.execute("SELECT operation_id FROM shared_payloads").fetchall() == [
            (control,)
        ]
        assert db.execute(
            "SELECT operation_id FROM private_extraction_receipts"
        ).fetchall() == [("sharedgen:independent-review",)]
        assert db.execute(
            "SELECT status FROM shared_operations WHERE operation_id=?", (dependent,)
        ).fetchone() == ("invalidated",)
    restored = tmp_path / "restored-review.db"
    assert (
        QuarantinedRestore(ledger, 14, EventStoreRestoreAdapter)
        .restore(backup, restored, NOW)
        .activation_eligible
    )
    with sqlite3.connect(f"file:{restored}?immutable=1", uri=True) as db:
        assert db.execute("SELECT COUNT(*) FROM shared_payloads").fetchone() == (0,)
        assert db.execute(
            "SELECT operation_id FROM private_extraction_receipts"
        ).fetchall() == [("sharedgen:independent-review",)]


def test_future_consent_revocation_keeps_pending_work_until_effective(
    tmp_path, monkeypatch
):
    events, ledger, gate, shared, runtime, _, source, content, reply = _direct_runtime(
        tmp_path, monkeypatch, SUGGESTION
    )
    grant = consent(ledger, source)
    assert gate.reconcile()
    runtime._propose_shared(principal("a"), source, content, "corr", reply)
    pending(runtime, "a", source, content, "sharedgen:future-receipt", True)
    backup = tmp_path / "future.db"
    create_sqlite_backup(events.database_path, backup, 14, NOW)
    revoke(
        ledger,
        source,
        "participant-a",
        "participant-b",
        at=NOW + timedelta(minutes=1),
        existing=grant,
    )
    assert gate.reconcile()
    restored = tmp_path / "future-restored.db"
    assert (
        QuarantinedRestore(ledger, 14, EventStoreRestoreAdapter)
        .restore(backup, restored, NOW)
        .activation_eligible
    )
    with sqlite3.connect(f"file:{restored}?immutable=1", uri=True) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM private_extraction_receipts"
        ).fetchone() == (1,)
    with events._open() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_payloads").fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM private_extraction_receipts"
        ).fetchone() == (1,)
    gate._clock = lambda: NOW + timedelta(minutes=2)
    assert gate.reconcile()
    assert shared.context(principal("b")) == ()
    with events._open() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_payloads").fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM private_extraction_receipts"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "unknown"),
        ("sources", "not-a-list"),
        ("sources", [5]),
        ("sources", []),
        ("audience", []),
        ("reviews", ["one", "two"]),
        ("consents", []),
        ("consents", [["wrong", "participant-b", None]]),
    ],
)
def test_corrupt_dependency_binding_blocks_replay_and_reconciliation(
    tmp_path, monkeypatch, field, value
):
    events, _, gate, _, runtime, _, source, content, _ = _direct_runtime(
        tmp_path, monkeypatch, SUGGESTION
    )
    pending(runtime, "a", source, content, "sharedgen:corrupt")
    with events._open() as db:
        data = json.loads(
            db.execute("SELECT proposal FROM private_extraction_receipts").fetchone()[0]
        )
    data["snapshot"]["dependencies"][field] = value
    with events._open() as db, db:
        db.execute(
            "UPDATE private_extraction_receipts SET proposal=?", (json.dumps(data),)
        )
    with pytest.raises(ValueError, match="invalid shared receipt"):
        runtime._shared_proposal_audit.read(
            "sharedgen:corrupt", "participant-a", source, content
        )
    assert not gate.reconcile()


@pytest.mark.parametrize(
    "mismatch",
    [
        "source",
        "receipt-owner",
        "candidate",
        "review",
        "review-lineage",
        "snapshot",
        "version",
    ],
)
def test_protected_dependency_lineage_and_identity_are_validated(
    tmp_path, monkeypatch, mismatch
):
    values, _, _ = parent_setup(tmp_path, monkeypatch)
    events, _, gate, _, runtime, _, parent_source, *_ = values
    source, content = source_for(events, "a", "candidate")
    operation = propose(runtime, "a", source, content)
    pending(runtime, "a", source, content, "sharedgen:binding")
    with events._open() as db:
        data = json.loads(
            db.execute("SELECT proposal FROM private_extraction_receipts").fetchone()[0]
        )
    dependencies = data["snapshot"]["dependencies"]
    if mismatch == "source":
        dependencies["sources"] = ["missing"]
        dependencies["consents"] = [["missing", "participant-b", None]]
    elif mismatch == "receipt-owner":
        with events._open() as db, db:
            db.execute(
                "UPDATE private_extraction_receipts SET source_principal_id='participant-b'"
            )
    elif mismatch == "candidate":
        with events._open() as db, db:
            manifest = json.loads(
                db.execute(
                    "SELECT binding FROM protected_shared_manifests WHERE operation_id=?",
                    (operation,),
                ).fetchone()[0]
            )
            manifest["model_dependencies"]["audience"] = ["participant-c"]
            manifest["model_dependencies"]["consents"] = [
                [s, "participant-c", None]
                for s in manifest["model_dependencies"]["sources"]
            ]
            manifest["model_dependencies"]["reviews"] = []
            db.execute(
                "UPDATE protected_shared_manifests SET binding=? WHERE operation_id=?",
                (json.dumps(manifest), operation),
            )
    elif mismatch == "review":
        dependencies["reviews"] = ["missing-review"]
    elif mismatch == "review-lineage":
        dependencies["sources"] = [source]
        dependencies["consents"] = [[source, "participant-b", None]]
        assert parent_source != source
    elif mismatch == "snapshot":
        data["snapshot"]["selected"] = []
    else:
        data["version"] = "unknown"
    with events._open() as db, db:
        db.execute(
            "UPDATE private_extraction_receipts SET proposal=?", (json.dumps(data),)
        )
    assert not gate.reconcile()
    with events._open() as db:
        with pytest.raises(ValueError):
            validate_shared_graph(db)
    if mismatch == "snapshot":
        with pytest.raises(ValueError, match="invalid shared receipt"):
            runtime._shared_proposal_audit._receipt(
                "sharedgen:binding", "participant-a", source
            )


def test_legacy_receipts_are_unavailable_and_quarantined_and_legacy_scope_is_exact(
    tmp_path, monkeypatch
):
    events, ledger, gate, _, runtime, _, source, content, reply = _direct_runtime(
        tmp_path, monkeypatch, SUGGESTION
    )
    runtime._propose_shared(principal("a"), source, content, "corr", reply)
    pending(runtime, "a", source, content, "sharedgen:legacy")
    with events._open() as db, db:
        data = json.loads(
            db.execute("SELECT proposal FROM private_extraction_receipts").fetchone()[0]
        )
        data["version"] = "shared-proposal-receipt-v1"
        del data["snapshot"]["dependencies"]
        db.execute(
            "UPDATE private_extraction_receipts SET proposal=?", (json.dumps(data),)
        )
        operation, binding = db.execute(
            "SELECT operation_id,binding FROM protected_shared_manifests"
        ).fetchone()
        binding = json.loads(binding)
        del binding["model_dependencies"]
        db.execute(
            "UPDATE protected_shared_manifests SET binding=? WHERE operation_id=?",
            (json.dumps(binding), operation),
        )
    with pytest.raises(ValueError, match="invalid shared receipt"):
        runtime._shared_proposal_audit._receipt(
            "sharedgen:legacy", "participant-a", source
        )
    backup = tmp_path / "legacy.db"
    create_sqlite_backup(events.database_path, backup, 14, NOW)
    revoke(ledger, source, "participant-a", "participant-b")
    assert gate.reconcile()
    with events._open() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_payloads").fetchone() == (0,)
    restored = tmp_path / "legacy-restored.db"
    assert (
        QuarantinedRestore(ledger, 14, EventStoreRestoreAdapter)
        .restore(backup, restored, NOW)
        .activation_eligible
    )
    with sqlite3.connect(f"file:{restored}?immutable=1", uri=True) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM private_extraction_receipts"
        ).fetchone() == (0,)


def test_old_consent_identity_does_not_erase_proposals_bound_to_new_grant(
    tmp_path, monkeypatch
):
    events, ledger, gate, _, runtime, _, source, content, _ = _direct_runtime(
        tmp_path, monkeypatch, SUGGESTION
    )
    service, actor, old = consent(ledger, source)
    assert gate.reconcile()
    old_op = propose(runtime, "a", source, content)
    newer = replace(old, consent_id="new-consent", operation_id="new-grant", revision=2)
    assert service.grant(actor, newer).created
    assert gate.reconcile()
    # Reserve another one-shot proposal over the same source and audience.
    pending(runtime, "a", source, content, "sharedgen:new-grant", True)
    revoke(
        ledger, source, "participant-a", "participant-b", existing=(service, actor, old)
    )
    assert gate.reconcile()
    assert gate.reconcile()
    with events._open() as db:
        assert db.execute(
            "SELECT status FROM shared_operations WHERE operation_id=?", (old_op,)
        ).fetchone() == ("invalidated",)
        assert db.execute(
            "SELECT operation_id FROM private_extraction_receipts"
        ).fetchall() == [("sharedgen:new-grant",)]


def test_review_revocation_reaches_transitive_new_items(tmp_path, monkeypatch):
    events, ledger, gate, shared, runtime, _, source, _, _ = _direct_runtime(
        tmp_path, monkeypatch, SUGGESTION
    )
    parent = replace(candidate(source), item_id="zz-origin")
    shared.quarantine(parent)
    consent(ledger, source)
    assert gate.reconcile()
    shared.review(
        parent,
        decision_id="approve-origin",
        review_version="local-review-v1",
        approve=True,
    )
    first_source, first_text = source_for(events, "a", "first-derived")
    first = propose(runtime, "a", first_source, first_text)
    consent(ledger, first_source)
    assert gate.reconcile()
    shared.review(
        _inspect(events, gate, first)[1],
        decision_id="approve-first-derived",
        review_version="local-review-v1",
        approve=True,
    )
    second_source, second_text = source_for(events, "a", "second-derived")
    second = propose(runtime, "a", second_source, second_text)
    pending(
        runtime, "a", second_source, second_text, "sharedgen:transitive-receipt", True
    )
    with events._open() as db:
        manifest = json.loads(
            db.execute(
                "SELECT binding FROM protected_shared_manifests WHERE operation_id=?",
                (second,),
            ).fetchone()[0]
        )
        assert manifest["model_dependencies"]["reviews"] == [first]
    shared.revoke_review(parent.proposal.operation_id, "revoke-origin")
    assert gate.reconcile()
    with events._open() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM shared_operations WHERE status='invalidated'"
        ).fetchone() == (3,)
        assert db.execute(
            "SELECT COUNT(*) FROM private_extraction_receipts"
        ).fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM shared_payloads").fetchone() == (0,)
