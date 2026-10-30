"""Ordinary abstract influence is screened, source-bound and consent-independent."""

from dataclasses import replace

import pytest
import json

from app.cognition.shared import SharedIntelligence
from app.core.shared_graph import erase_shared_source, validate_shared_graph
from test_cognition_pipeline import NOW, append_event, principal
from test_reviewed_shared import candidate
from test_shared_intelligence import shared as shared_fixture


@pytest.fixture
def shared(tmp_path):
    return shared_fixture.__wrapped__(tmp_path)


def test_ordinary_influence_reaches_both_audiences_without_source_disclosure(shared):
    events, _ledger, gate, source, _ = shared
    item = replace(
        candidate(source, value="Compare causal models of uncertainty."),
        audience=("participant-a", "participant-b"),
    )
    app = SharedIntelligence(
        events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )
    assert app.transition(item) == item.proposal.operation_id
    for who in (principal("a"), principal("b")):
        record = app.context(who)[0]
        assert record.text == item.proposal.candidate.value
        assert source not in repr(record.structured())
        assert "participant-a" not in repr(record.structured())
        assert record.verification == "unverified"
    with events._open() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM reviewed_shared_decisions"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT event_id FROM protected_shared_sources"
        ).fetchone() == (source,)
        validate_shared_graph(db)
    restarted = SharedIntelligence(
        events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )
    assert restarted.transition(item) == item.proposal.operation_id
    with events._open() as db, db:
        erase_shared_source(db, source)
        validate_shared_graph(db)
    assert restarted.context(principal("b")) == ()


@pytest.mark.parametrize(
    "text",
    [
        "Consider causal models after losing a child.",
        "Compare models from patient Alice.",
        "Consider uncertainty at 17 Maple Street.",
        "Explore uncertainty through privatecanaryabcdefghijk.",
        "Compare explanations from @secret_handle.",
        "Consider uncertainty in a secret marriage.",
    ],
)
def test_uncertain_personal_or_recognizable_detail_never_becomes_shared(shared, text):
    events, _ledger, gate, source, _ = shared
    app = SharedIntelligence(
        events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )
    with pytest.raises(ValueError, match="privacy screen"):
        app.transition(candidate(source, value=text))
    assert app.context(principal("b")) == ()


def test_ordinary_transition_requires_explicit_application_policy(shared):
    events, _ledger, gate, source, _ = shared
    app = SharedIntelligence(events, gate, clock=lambda: NOW)
    with pytest.raises(ValueError, match="disabled"):
        app.transition(candidate(source))


def test_ordinary_revision_preserves_protected_ancestry(shared):
    events, _ledger, gate, source, _ = shared
    app = SharedIntelligence(
        events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )
    first = candidate(source, value="Compare causal models of uncertainty.")
    app.transition(first)
    next_source = append_event(
        events,
        principal("a"),
        "second-source",
        "A separate private experience about explanation.",
    )
    second = replace(
        first,
        expected_revision=1,
        proposal=replace(
            first.proposal,
            operation_id="ordinary-second",
            protected_source_event_ids=(next_source,),
            candidate=replace(
                first.proposal.candidate,
                value="Consider competing explanations before adopting a view.",
            ),
        ),
    )
    app.transition(second)
    record = app.context(principal("b"))[0]
    assert record.revision == 2
    assert record.memory.alternatives == (first.proposal.candidate.value,)
    with events._open() as db, db:
        erase_shared_source(db, source)
    assert app.context(principal("b")) == ()


def test_public_sdk_receives_only_bounded_abstraction_and_loses_authority_on_erasure(
    shared,
    tmp_path,
):
    from app.integration.shared_intellectual_source import SharedIntellectualSource
    from test_int24_runtime import setup, run_once
    from test_public_experience import wake

    events, _ledger, gate, source, _ = shared
    intelligence = SharedIntelligence(
        events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )
    item = replace(
        candidate(source, value="Compare causal models of uncertainty."),
        audience=("participant-a", "participant-b", "public:account-1"),
    )
    intelligence.transition(item)
    bridge = SharedIntellectualSource(intelligence, "account-1")
    public_dir = tmp_path / "public"
    public_dir.mkdir(mode=0o700)
    app, worker, sdk, http, clock = setup(
        public_dir,
        {"decision": "no_action", "reason_category": "other"},
        shared_intellectual_source=bridge,
    )
    try:
        run_once(app, worker)
        data = json.loads(sdk.calls[-1]["input"][1]["content"])
        influence = data["intellectual_state"]["sources"][0]
        assert influence["kind"] == "shared_abstract_influence"
        assert item.proposal.candidate.value in influence["text"]
        assert "not independent public evidence" in influence["text"]
        rendered = json.dumps(data)
        assert "participant-a" not in rendered
        assert source not in rendered
        assert "Synthetic A" not in rendered
        assert "shared-a" not in rendered
        assert not http.calls
        with app.journal.transaction() as db:
            operation = db.execute(
                "SELECT operation FROM agent_intellectual_operations"
            ).fetchone()[0]
        assert app.intellectual.valid(operation)
        with events._open() as db, db:
            erase_shared_source(db, source)
        assert not app.intellectual.valid(operation)
        wake(app, worker, clock, "after-erasure")
        assert item.proposal.candidate.value not in sdk.calls[-1]["input"][1]["content"]
    finally:
        worker.close()


def test_ordinary_worker_transition_is_consumed_by_another_private_conversation(
    tmp_path, monkeypatch
):
    from app.cognition.worker_shared import SharedSuggestion
    from app.cognition.shared_context import SharedModelRequest
    from test_worker_shared_proposals import _setup

    events, _ledger, _gate, intelligence, runtime, requests = _setup(
        tmp_path,
        monkeypatch,
        SharedSuggestion(
            "new",
            None,
            "belief_or_hypothesis",
            "Compare causal models of uncertainty.",
            0.5,
        ),
    )
    intelligence._ordinary_enabled = True
    runtime._shared_proposal_audit._audience = ("participant-a", "participant-b")
    runtime.submit(
        principal("a"),
        "first",
        "Private example about doubt and competing observations.",
    )
    assert (
        intelligence.context(principal("b"))[0].text
        == "Compare causal models of uncertainty."
    )
    runtime.submit(principal("b"), "second", "Could we compare ideas?")
    received = [r for r in requests if type(r) is SharedModelRequest][-1]
    assert received.shared[0].text == "Compare causal models of uncertainty."
    assert "Private example" not in received.serialize()
    assert "participant-a" not in received.serialize()


def test_second_principal_can_revise_shared_position_without_receiving_sources(
    tmp_path, monkeypatch
):
    from app.cognition.worker_shared import SharedSuggestion, SharedProposalRequest
    from test_worker_shared_proposals import _setup

    def suggestion(request):
        if not request.items:
            return SharedSuggestion(
                "new",
                None,
                "belief_or_hypothesis",
                "Compare causal models of uncertainty.",
                0.5,
            )
        return SharedSuggestion(
            "revise",
            request.items[0].handle,
            "belief_or_hypothesis",
            "Consider competing explanations before adopting a view.",
            0.6,
        )

    events, _ledger, _gate, intelligence, runtime, requests = _setup(
        tmp_path, monkeypatch, suggestion
    )
    intelligence._ordinary_enabled = True
    runtime._shared_proposal_audit._audience = ("participant-a", "participant-b")
    runtime.submit(
        principal("a"), "first", "Private A experience with syntheticcanaryalphabeta."
    )
    runtime.submit(
        principal("b"), "second", "Private B experience with syntheticcanarygammadelta."
    )
    latest = intelligence.context(principal("a"))[0]
    assert latest.revision == 2
    assert latest.text == "Consider competing explanations before adopting a view."
    request = [r for r in requests if type(r) is SharedProposalRequest][-1]
    assert request.items[0].statement == "Compare causal models of uncertainty."
    assert "syntheticcanaryalphabeta" not in request.serialize()
    with events._open() as db, db:
        assert db.execute(
            "SELECT COUNT(DISTINCT source_principal) FROM protected_shared_sources WHERE operation_id=?",
            (latest.operation_id,),
        ).fetchone() == (2,)
        source = db.execute(
            "SELECT event_id FROM protected_shared_sources WHERE source_principal='participant-a' LIMIT 1"
        ).fetchone()[0]
        erase_shared_source(db, source)
        validate_shared_graph(db)
    assert intelligence.context(principal("b")) == ()


def test_transition_rechecks_application_policy_before_promotion(shared):
    events, _ledger, gate, source, _ = shared
    with pytest.raises(TypeError, match="bool"):
        SharedIntelligence(
            events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=1
        )
    app = SharedIntelligence(
        events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )
    with pytest.raises(TypeError, match="exact"):
        app.transition(object())
    checks = iter((True, False))
    with pytest.raises(ValueError, match="authorization"):
        app.transition(
            candidate(source, value="Compare causal models of uncertainty."),
            persistence_guard=lambda: next(checks),
        )
    assert app.context(principal("b")) == ()


def test_competing_revision_wins_before_ordinary_promotion(shared):
    events, _ledger, gate, source, _ = shared
    item = candidate(source, value="Compare causal models of uncertainty.")
    rival = replace(item, proposal=replace(item.proposal, operation_id="rival"))
    other = SharedIntelligence(
        events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )

    def checkpoint(point):
        if point == "after_shared_screen":
            other.transition(rival)

    app = SharedIntelligence(
        events,
        gate,
        clock=lambda: NOW,
        ordinary_abstractions_enabled=True,
        checkpoint=checkpoint,
    )
    with pytest.raises(ValueError, match="revision conflict"):
        app.transition(item)
    assert app.context(principal("b"))[0].operation_id == "rival"


def test_ordinary_transition_cannot_inherit_reviewed_or_synthetic_authority(shared):
    from test_shared_intelligence import approval

    events, _ledger, gate, source, _ = shared
    prior = approval(source)
    SharedIntelligence(events, gate, clock=lambda: NOW, approvals=(prior,)).commit(
        prior
    )
    source2 = append_event(
        events, principal("a"), "new", "A separate private example about explanations."
    )
    item = replace(
        candidate(source2, value="Compare causal models of uncertainty."),
        expected_revision=1,
        proposal=replace(
            candidate(source2).proposal,
            operation_id="ordinary-next",
            candidate=replace(
                candidate(source2).proposal.candidate,
                value="Compare causal models of uncertainty.",
            ),
        ),
    )
    app = SharedIntelligence(
        events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )
    with pytest.raises(ValueError, match="cannot widen"):
        app.transition(item)
    assert app.context(principal("b"))[0].text == prior.proposal.candidate.value


@pytest.mark.parametrize("tamper", ["policy", "payload"])
def test_backup_validation_rejects_forged_ordinary_policy_or_sensitive_payload(
    shared, tamper
):
    import hashlib
    from app.cognition.shared import SyntheticSharedApproval

    events, _ledger, gate, source, _ = shared
    item = candidate(source, value="Compare causal models of uncertainty.")
    app = SharedIntelligence(
        events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )
    if tamper == "policy":
        app._screen(
            SyntheticSharedApproval(
                item.principal, item.proposal, item.item_id, 0, item.audience
            ),
            reviewed=False,
            ordinary=True,
        )
    else:
        app.transition(item)
    with events._open() as db, db:
        binding = json.loads(
            db.execute("SELECT binding FROM protected_shared_manifests").fetchone()[0]
        )
        if tamper == "policy":
            binding["ordinary_policy"] = "forged"
        else:
            text = "Consider patient Alice."
            db.execute("UPDATE shared_payloads SET value=?", (text,))
            binding["candidate_digest"] = hashlib.sha256(text.encode()).hexdigest()
        db.execute(
            "UPDATE protected_shared_manifests SET binding=?", (json.dumps(binding),)
        )
        with pytest.raises(ValueError, match="ordinary"):
            validate_shared_graph(db)


def test_public_adapter_rejects_wrong_type_account_and_legacy_authority(
    shared, tmp_path
):
    from app.integration.shared_intellectual_source import SharedIntellectualSource
    from test_shared_intelligence import approval
    from test_int24_runtime import setup
    from app.private_web.shared_audit import SharedProposalAuditStore
    from app.core.provider_spend import ProviderStateStore
    from app.core.store import EventStore

    events, _ledger, gate, source, _ = shared
    with pytest.raises(TypeError, match="exact"):
        SharedIntellectualSource(object(), "account-1")
    prior = replace(approval(source), audience=("participant-a", "public:account-1"))
    intelligence = SharedIntelligence(
        events,
        gate,
        clock=lambda: NOW,
        approvals=(prior,),
        ordinary_abstractions_enabled=True,
    )
    intelligence.commit(prior)
    bridge = SharedIntellectualSource(intelligence, "account-1")
    assert bridge.sources() == {}
    audit = SharedProposalAuditStore(
        ProviderStateStore(EventStore(tmp_path / "provider.db")),
        events,
        intelligence,
        prior.audience,
        lambda: NOW,
    )
    fresh = append_event(events, principal("a"), "fresh", "A separate private example.")
    with events._open() as db:
        content = db.execute(
            "SELECT content FROM private_payloads p JOIN event_envelopes e USING(payload_id) WHERE e.event_id=?",
            (fresh,),
        ).fetchone()[0]
    assert audit._snapshot(principal("a"), fresh, content)[1].items == ()
    target = tmp_path / "public"
    target.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="account mismatch"):
        setup(
            target,
            {"decision": "no_action", "reason_category": "other"},
            account="account-2",
            shared_intellectual_source=bridge,
        )


def test_ordinary_cli_requires_existing_finite_shared_proposal_path(tmp_path):
    from app.private_web.cli import main

    with pytest.raises(SystemExit, match="bounded shared proposals"):
        main(
            [
                "serve",
                "--auth-db",
                str(tmp_path / "auth.db"),
                "--event-db",
                str(tmp_path / "events.db"),
                "--worker-socket",
                str(tmp_path / "worker.sock"),
                "--enable-ordinary-abstractions",
                "--allowed-host",
                "chat.test",
                "--allowed-origin",
                "https://chat.test",
            ]
        )


def test_shared_proposal_snapshot_is_bounded_after_many_positions(shared, tmp_path):
    from app.private_web.shared_audit import SharedProposalAuditStore
    from app.core.provider_spend import ProviderStateStore
    from app.core.store import EventStore
    from app.core.model_shared_dependencies import validate_dependencies
    from app.core.shared_graph import revoke_shared_authorization

    events, _ledger, gate, source, _ = shared
    app = SharedIntelligence(
        events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )
    first = replace(
        candidate(source, value="Compare causal models of uncertainty."),
        audience=("participant-a", "participant-b"),
    )
    for index in range(129):
        app.transition(
            replace(
                first,
                item_id=f"position-{index:03}",
                proposal=replace(first.proposal, operation_id=f"ordinary-{index:03}"),
            )
        )
    audit = SharedProposalAuditStore(
        ProviderStateStore(EventStore(tmp_path / "provider.db")),
        events,
        app,
        first.audience,
        lambda: NOW,
    )
    own = append_event(events, principal("b"), "b-turn", "A separate private example.")
    saved = audit.reserve(
        "sharedgen:bounded", principal("b"), own, "A separate private example."
    )
    assert len(saved.snapshot.visible) == 1
    assert saved.request.items
    assert (
        audit.read(
            "sharedgen:bounded", "participant-b", own, "A separate private example."
        ).status
        == "reserved"
    )
    with events._open() as db, db:
        revoke_shared_authorization(db, saved.snapshot.selected[0][3])
        with pytest.raises(ValueError, match="ordinary shared input"):
            validate_dependencies(db, saved.snapshot.dependencies)


def test_shared_public_bridge_rejects_nested_same_ledger_write_fences(
    shared, tmp_path, monkeypatch
):
    from app.integration.shared_intellectual_source import SharedIntellectualSource
    import test_int24_runtime as public

    events, ledger, gate, _source, _ = shared
    intelligence = SharedIntelligence(events, gate, clock=lambda: NOW)
    bridge = SharedIntellectualSource(intelligence, "account-1")
    monkeypatch.setattr(public, "PrivacyLedger", lambda _path: ledger)
    target = tmp_path / "public"
    target.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="independent privacy ledgers"):
        result = public.setup(
            target,
            {"decision": "no_action", "reason_category": "other"},
            shared_intellectual_source=bridge,
        )
        result[1].close()
