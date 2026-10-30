"""Shared intellectual influence and private fiction keep separate authority."""

from dataclasses import replace
import json

import pytest

from app.cognition.conversation_context import FictionEpisode
from app.cognition.shared_context import SharedModelRequest
from app.core.provider_contract import (
    build_worker_context_request,
    default_openai_provider_config,
)
from app.trusted.openai_worker_protocol import (
    InferenceRequest,
    decode_request,
    encode_request,
)
from test_cognition_pipeline import principal
from test_worker_shared_proposals import _setup
from app.cognition.worker_shared import SharedSuggestion


def test_shared_narrative_round_trip_retains_fiction_and_provider_purpose(
    tmp_path, monkeypatch
):
    _events, _ledger, _gate, _shared, runtime, requests = _setup(
        tmp_path, monkeypatch, SharedSuggestion("no_change", None, None, None, None)
    )
    runtime.submit(principal("a"), "first", "Could we compare explanations?")
    shared = next(
        request for request in requests if type(request) is SharedModelRequest
    )
    private = replace(
        shared.private,
        narrative_mode=True,
        episodes=(FictionEpisode("An explicitly fictional lighthouse.", 1),),
    )
    combined = replace(shared, private=private)
    restored = decode_request(encode_request(InferenceRequest("op", "corr", combined)))
    data = json.loads(restored.model_request.serialize())
    assert data["contract_version"] == "model-contract-private-narrative-context-v1"
    assert data["fictional_episodes"][0]["evidence"] is False
    assert data["fictional_episodes"][0]["verified_action"] is False
    assert data["shared_intellectual_state"] == []
    request = build_worker_context_request(
        restored.model_request,
        default_openai_provider_config(),
    )
    assert request.purpose == "narrative_reply"


def test_narrative_projection_preserves_shared_wrapper_and_full_byte_limit(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from app.cognition.worker_narrative import NarrativeProposal

    _events, _ledger, _gate, _shared, runtime, requests = _setup(
        tmp_path, monkeypatch, SharedSuggestion("no_change", None, None, None, None)
    )
    runtime.submit(principal("a"), "first", "Could we compare explanations?")
    base = next(request for request in requests if type(request) is SharedModelRequest)
    context = replace(base, private=replace(base.private, narrative_mode=True))
    audit = SimpleNamespace(
        proposal=NarrativeProposal("create", None, "A fictional clock tower."),
        selected=(),
    )
    projected = runtime._project_narrative(context, audit, 32000)
    assert type(projected) is SharedModelRequest
    assert projected.shared == context.shared
    assert projected.private.episodes[0].text == "A fictional clock tower."
    with pytest.raises(ValueError, match="budget"):
        runtime._project_narrative(context, audit, len(context.serialize().encode()))


@pytest.mark.parametrize("worker_narrative_enabled", [False, True])
def test_authenticated_private_sdk_combines_shared_influence_with_only_own_fiction(
    tmp_path,
    worker_narrative_enabled,
):
    from app.cognition.narrative_store import NarrativeStore
    from app.cognition.shared import SharedIntelligence
    from app.core.shared_graph import erase_shared_source
    from app.private_web.runtime import WorkerConversationRuntime
    from test_agent_intellectual_state import private_setup, private_submit
    from test_cognition_pipeline import append_event, NOW
    from test_reviewed_shared import candidate

    app, a, b, events, _ledger, gate, sdk, server = private_setup(tmp_path)
    server._narrative_enabled = worker_narrative_enabled
    source_a = append_event(
        events,
        principal("a"),
        "shared-source",
        "A private example with PRIVATE_A_CANARY_PERSONAL.",
    )
    source_b = append_event(
        events,
        principal("b"),
        "fiction-source",
        "A private request about fictional continuity.",
    )
    shared = SharedIntelligence(
        events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )
    item = replace(
        candidate(source_a, value="Compare causal models of uncertainty."),
        audience=("participant-a", "participant-b"),
    )
    shared.transition(item)
    NarrativeStore(events).commit(
        principal("b"),
        operation_id="narrative:seed",
        source_event_id=source_b,
        text="На вымышленном маяке возник FICTION_B_CANARY.",
        item_id="episode:seed",
        expected_revision=0,
        expected_heads=(),
        exposed_sources=(source_b,),
        exposed_episodes=(),
        created_at=NOW,
    )
    old = app._conversations
    app._conversations = WorkerConversationRuntime(
        events,
        gate,
        old._worker,
        provider_state=old._provider,
        shared_intelligence=shared,
        narrative_enabled=True,
        narrative_permission_current=lambda: True,
        clock=old._clock,
    )
    try:
        private_submit(server, b, "first", "Обсудим основания наших суждений.")
        if not worker_narrative_enabled:
            assert sdk.calls == []
            return
        data = json.loads(sdk.calls[-1]["input"])
        assert data["contract_version"] == "model-contract-private-narrative-context-v1"
        assert (
            data["fictional_episodes"][0]["classification"] == "fictional_autobiography"
        )
        assert data["fictional_episodes"][0]["evidence"] is False
        assert (
            data["shared_intellectual_state"][0]["text"]
            == item.proposal.candidate.value
        )
        assert "PRIVATE_A_CANARY_PERSONAL" not in sdk.calls[-1]["input"]
        assert "participant-a" not in sdk.calls[-1]["input"]
        private_submit(server, a, "first", "Обсудим логические основания.")
        assert "FICTION_B_CANARY" not in sdk.calls[-1]["input"]
        with events._open() as db, db:
            erase_shared_source(db, source_a)
        private_submit(server, b, "after-delete", "Продолжим обсуждение оснований.")
        data = json.loads(sdk.calls[-1]["input"])
        assert data["shared_intellectual_state"] == []
        assert data["fictional_episodes"][0]["text"].endswith("FICTION_B_CANARY.")
    finally:
        server.close()


def test_erasure_reaches_fiction_derived_from_shared_context_and_descendants(
    tmp_path, monkeypatch
):
    from threading import Thread
    from app.cognition.narrative_store import NarrativeStore, validate_narrative_graph
    from app.cognition.shared import SharedIntelligence
    from app.core.shared_graph import erase_shared_source
    from app.private_web.runtime import WorkerConversationRuntime
    from test_agent_intellectual_state import private_setup, private_submit
    from test_cognition_pipeline import append_event, NOW
    from test_reviewed_shared import candidate

    app, _a, b, events, _ledger, gate, sdk, server = private_setup(tmp_path)
    server._narrative_enabled = True
    source_a = append_event(
        events,
        principal("a"),
        "shared-source",
        "A private experience about competing observations.",
    )
    shared = SharedIntelligence(
        events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )
    shared.transition(
        replace(
            candidate(source_a, value="Compare causal models of uncertainty."),
            audience=("participant-a", "participant-b"),
        )
    )
    old = app._conversations
    app._conversations = WorkerConversationRuntime(
        events,
        gate,
        old._worker,
        provider_state=old._provider,
        shared_intelligence=shared,
        narrative_enabled=True,
        narrative_permission_current=lambda: True,
        clock=old._clock,
    )
    original = sdk.create

    def result(**kwargs):
        response = original(**kwargs)
        if kwargs["text"]["format"]["name"] == "private_narrative_proposal_v2":
            response["output"][0]["content"][0]["text"] = json.dumps(
                {
                    "proposal": {
                        "decision": "create",
                        "handle": None,
                        "episode": "В вымышленном саду звучит DERIVED_FICTION_CANARY.",
                    }
                }
            )
        return response

    monkeypatch.setattr(sdk, "create", result)
    worker = Thread(target=lambda: server.serve(max_connections=2), daemon=True)
    worker.start()
    try:
        response = b.submit("story", "Расскажи вымышленную историю.")
        worker.join(10)
        assert not worker.is_alive()
        assert response[0] == "201 Created", response
        store = NarrativeStore(events)
        root = store.current(principal("b"))[0]
        assert root.text.endswith("DERIVED_FICTION_CANARY.")
        from app.cognition.conversation_context import MemoryConversationAssembler
        from app.core.context import ContextLimits
        from app.core.store import EventValidationError

        fresh = append_event(
            events, principal("b"), "withheld-creator", "Обсудим основания."
        )
        with events._open() as db:
            withheld, _ = MemoryConversationAssembler(
                events, ContextLimits(32000, 1)
            ).build(
                principal("b"),
                fresh,
                db,
                reply_permitted=lambda _reply: False,
                include_narrative=True,
            )
        assert withheld.episodes == ()
        creator = "reply:" + root.operation_id.removeprefix("narrative:")
        creator_reply = app._conversations._outputs.read_reply(principal("b"), creator)
        with pytest.raises(EventValidationError, match="sources unavailable"):
            app._conversations._outputs.reserve_reply(
                principal("b"),
                replace(
                    creator_reply.operation,
                    operation_id="reply:missing-shared",
                    shared_operation_ids=(),
                ),
            )
        with events._open() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE agent_reply_operations SET retention_status='invalidated' WHERE operation_id=?",
                (creator,),
            )
            with pytest.raises(ValueError, match="narrative shared lineage"):
                validate_narrative_graph(db)
            db.rollback()
        # A later turn must inherit creator authorization even if only fiction is ranked.
        private_submit(server, b, "later", "Обсудим сад.")
        with events._open() as db:
            latest = db.execute(
                "SELECT operation_id FROM agent_reply_operations ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
            assert (
                db.execute(
                    "SELECT shared_operation FROM protected_reply_shared WHERE operation_id=?",
                    (latest,),
                ).fetchone()
                is not None
            )
        store.commit(
            principal("b"),
            operation_id="narrative:child",
            source_event_id=root.sources[0],
            text="Вымышленный сад продолжается: CHILD_FICTION_CANARY.",
            item_id="episode:child",
            expected_revision=0,
            expected_heads=((root.item_id, root.revision),),
            exposed_sources=root.sources,
            exposed_episodes=(root.operation_id,),
            created_at=NOW,
        )
        with events._open() as db, db:
            erase_shared_source(db, source_a)
            validate_narrative_graph(db)
            assert db.execute("SELECT COUNT(*) FROM narrative_payloads").fetchone() == (
                0,
            )
        assert store.current(principal("b")) == ()
        private_submit(server, b, "after-delete", "Обсудим основания.")
        assert "DERIVED_FICTION_CANARY" not in sdk.calls[-1]["input"]
        assert "CHILD_FICTION_CANARY" not in sdk.calls[-1]["input"]
    finally:
        server.close()
