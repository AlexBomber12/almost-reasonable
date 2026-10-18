"""Combined byte bounds, ranking, request closure, and snapshot provenance."""

from dataclasses import replace
import json
from threading import Event
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.cognition.conversation_context import MemoryConversationAssembler, ModelMemory
from app.core.context import ContextLimits, minimum_context_utf8_bytes
from app.core.self_state_store import SelfStateStore
from app.privacy.application import PrivacyReadBlocked
from app.private_web.runtime import Stage0ConversationRuntime
from test_cognition_pipeline import append_event, proposal_context, FakeExtractor
from test_private_memory_runtime import interest, extractor
from test_private_web_conversation import principal
import test_private_conversation_runtime as runtime_tests


@pytest.fixture
def setup(tmp_path):
    return runtime_tests.setup.__wrapped__(tmp_path)


def build(store, event_id, limits=ContextLimits(32_000, 99)):
    db = store._open()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            return MemoryConversationAssembler(store, limits).build(
                principal("a"), event_id, db
            )
    finally:
        db.close()


def memory(**values):
    return ModelMemory(
        "belief_or_hypothesis",
        "Ocean hypothesis",
        "active",
        0.4,
        "uncertain",
        None,
        "New evidence",
        1,
        0,
        None,
        ("Alternative explanation",),
        None,
        (),
        None,
        "unverified",
        "model_inference",
        **values,
    )


def test_generic_model_memory_evidence_bound_matches_persisted_graph():
    generic = replace(
        memory(), kind="candidate_useful_memory", supporting_evidence_count=100
    )
    assert generic.supporting_evidence_count == 100
    with pytest.raises(ValueError, match="invalid model memory evidence count"):
        replace(generic, supporting_evidence_count=101)
    with pytest.raises(ValueError, match="invalid model memory evidence count"):
        replace(memory(), supporting_evidence_count=51)


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "mission"),
        ("status", "public"),
        ("stance", "verified"),
        ("verification", "verified"),
        ("provenance", []),
        ("text", ""),
        ("text", 42),
        ("text", "\ud800"),
        ("text", "x" * 4097),
        ("revision_reason", " "),
        ("reason_for_interest", ""),
        ("rationale", 0),
        ("review_condition", ""),
        ("alternatives", []),
        ("constraints", ("x",) * 33),
        ("alternatives", ("",)),
        ("confidence", 1),
        ("confidence", float("nan")),
        ("confidence", float("inf")),
        ("confidence", 1.1),
        ("priority", True),
        ("priority", 101),
        ("supporting_evidence_count", -1),
        ("counterevidence_count", 51),
    ],
)
def test_closed_model_memory_rejects_invalid_values(field, value):
    with pytest.raises(ValueError):
        replace(memory(), **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("conversation", object()),
        ("memories", []),
        ("memories", (object(),)),
        ("memories", (memory(),) * 17),
        ("memories_omitted", 0),
    ],
)
def test_request_rejects_malformed_extension(setup, field, value):
    store, _, gate, _, runtime = setup
    runtime.submit(principal("a"), "seed", "Synthetic source")
    source = gate.retrieve_private_events(principal("a"))[-1].event_id
    request, _ = build(store, source)
    with pytest.raises(ValueError):
        replace(request, **{field: value}).serialize()


def test_serialized_request_hard_limit_and_provider_v1_compatibility(
    setup, monkeypatch
):
    import app.cognition.conversation_context as module
    from app.core.provider_contract import (
        default_openai_provider_config,
        build_openai_request,
    )

    store, _, gate, _, runtime = setup
    runtime.submit(principal("a"), "seed", "Synthetic source")
    source = gate.retrieve_private_events(principal("a"))[-1].event_id
    request, _ = build(store, source)
    assert (
        json.loads(
            build_openai_request(
                request.conversation, default_openai_provider_config()
            ).input_text
        )["contract_version"]
        == "model-contract-conversation-transcript-v4"
    )
    with pytest.raises(TypeError):
        build_openai_request(request, default_openai_provider_config())
    with pytest.raises(TypeError):
        runtime._gateway.submit(request, correlation_id="c", operation_id="op")
    monkeypatch.setattr(module, "MAX_MODEL_REQUEST_UTF8_BYTES", 1)
    with pytest.raises(ValueError, match="serialized byte"):
        request.serialize()


def test_combined_budget_omits_whole_memory_and_history(setup):
    store, _, gate, fake, runtime = setup
    runtime = Stage0ConversationRuntime(
        store,
        gate,
        fake,
        memory_extractor=extractor(
            lambda r: replace(
                interest(r),
                topic="Astronomy " + "x" * 300,
                reason_for_interest="y" * 300,
            )
        ),
    )
    runtime.submit(principal("a"), "seed", "Synthetic source")
    source = append_event(store, principal("a"), "current", "Astronomy")
    full, _ = build(store, source)
    assert full.memories
    current_only = replace(
        full,
        memories=(),
        conversation=replace(
            full.conversation, conversation=full.conversation.conversation[-1:]
        ),
    )
    budget = len(current_only.serialize().encode()) + 100
    limited, sources = build(store, source, ContextLimits(budget, 1))
    assert limited.memories == ()
    assert limited.memories_omitted
    assert limited.conversation.history_omitted
    assert len(limited.serialize().encode()) <= budget
    assert sources == (source,)
    # A message larger than raw context, and a message whose JSON overhead cannot
    # fit, both fail rather than supplying a request without its current input.
    with pytest.raises(PrivacyReadBlocked, match="combined_budget"):
        build(store, source, ContextLimits(minimum_context_utf8_bytes(), 1))
    with pytest.raises(PrivacyReadBlocked, match="combined_budget"):
        build(store, source, ContextLimits(minimum_context_utf8_bytes() + 100, 1))
    # A large old message is omitted whole while the short current input remains.
    append_event(store, principal("a"), "large", "z" * 2000)
    current = append_event(store, principal("a"), "small", "Astronomy")
    limited, _ = build(store, current, ContextLimits(budget, 99))
    assert limited.conversation.history_omitted
    assert "z" * 2000 not in limited.serialize()


def test_held_answer_never_enters_transcript(setup):
    store, _, gate, fake, _ = setup

    def crash(point):
        if point == "after_reply_commit":
            raise RuntimeError("synthetic crash")

    held = Stage0ConversationRuntime(
        store, gate, fake, memory_extractor=extractor(), checkpoint=crash
    )
    with pytest.raises(RuntimeError, match="synthetic crash"):
        held.submit(principal("a"), "held", "Первый вопрос")
    followup = Stage0ConversationRuntime(
        store, gate, fake, memory_extractor=extractor()
    )
    assert (
        followup.submit(principal("a"), "next", "Второй вопрос").model_state.value
        == "completed"
    )
    projection = json.loads(fake.calls[-1].request.input_text)
    assert [part["role"] for part in projection["conversation"]] == [
        "participant",
        "participant",
    ]
    with store._open() as db:
        assert {
            row[0]
            for row in db.execute("SELECT prompt_version FROM agent_reply_operations")
        } == {"private-memory-stage0-v2-transcript-v4"}


def test_optional_agent_answer_is_omitted_whole_under_serialized_budget(setup):
    store, _, gate, fake, _ = setup
    runtime = Stage0ConversationRuntime(store, gate, fake, memory_extractor=extractor())
    runtime.submit(principal("a"), "seed", "Первый вопрос")
    current = append_event(store, principal("a"), "next", "Второй вопрос")
    full, _ = build(store, current)
    assert [part.role.value for part in full.conversation.conversation] == [
        "participant",
        "agent",
        "participant",
    ]
    without_agent = replace(
        full,
        conversation=replace(
            full.conversation,
            conversation=(
                full.conversation.conversation[0],
                full.conversation.conversation[-1],
            ),
            history_omitted=True,
        ),
    )
    budget = len(full.serialize().encode()) - 1
    assert len(without_agent.serialize().encode()) < budget
    limited, sources = build(store, current, ContextLimits(budget, 99))
    assert [part.role.value for part in limited.conversation.conversation] == [
        "participant",
        "participant",
    ]
    assert limited.conversation.history_omitted
    assert len(limited.serialize().encode()) <= budget
    assert current in sources


def test_concurrent_release_order_uses_input_commit_sequence(setup):
    store, _, gate, fake, _ = setup
    entered, release = Event(), Event()

    def pause(point):
        if point == "before_release":
            entered.set()
            assert release.wait(5)

    first_runtime = Stage0ConversationRuntime(
        store, gate, fake, memory_extractor=extractor(), checkpoint=pause
    )
    next_runtime = Stage0ConversationRuntime(
        store, gate, fake, memory_extractor=extractor()
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(first_runtime.submit, principal("a"), "first", "Первый")
        assert entered.wait(5)
        second = next_runtime.submit(principal("a"), "second", "Второй")
        assert second.model_state.value == "completed"
        release.set()
        assert first.result(5).model_state.value == "completed"
    third = next_runtime.submit(principal("a"), "third", "Третий")
    assert third.model_state.value == "completed"
    conversation = json.loads(fake.calls[-1].request.input_text)["conversation"]
    assert [part["role"] for part in conversation] == [
        "participant",
        "agent",
        "participant",
        "agent",
        "participant",
    ]
    assert [
        part["content"] for part in conversation if part["role"] == "participant"
    ] == ["Первый", "Второй", "Третий"]


def test_missing_source_and_missing_memory_provenance_fail_closed(setup):
    store, _, gate, fake, _ = setup
    with pytest.raises(PrivacyReadBlocked):
        build(store, "missing")
    runtime = Stage0ConversationRuntime(store, gate, fake, memory_extractor=extractor())
    runtime.submit(principal("a"), "seed", "Astronomy")
    source = gate.retrieve_private_events(principal("a"))[-1].event_id
    with store._open() as db:
        db.execute("DELETE FROM protected_state_provenance")
    with pytest.raises(PrivacyReadBlocked, match="dependencies"):
        build(store, source)


def test_complete_revision_union_over_operation_limit_omits_memory(setup):
    from app.cognition.pipeline import CognitionEngine
    from app.cognition.contracts import InterestMemoryProposal
    from app.core.self_state import InterestStatus

    store, _, _, _, _ = setup
    context = principal("a")
    for revision in range(3):
        ids = tuple(
            append_event(store, context, f"evidence-{revision}-{i}") for i in range(49)
        )
        proposal = InterestMemoryProposal(
            proposal_context(
                ids[0],
                f"op-{revision}",
                expected_revision=revision or None,
                supporting=ids,
            ),
            "astronomy",
            "Astronomy",
            70,
            InterestStatus.ACTIVE,
            "Relevant observations",
        )
        outcome = CognitionEngine(
            store, SelfStateStore(store), extractor=FakeExtractor(proposal)
        ).process(context, ids[0], evidence_event_ids=ids[1:])
        assert outcome.status.value == "persisted"
    current = append_event(store, context, "current", "Astronomy")
    request, sources = build(store, current)
    assert request.memories == ()
    assert request.memories_omitted
    assert len(sources) == 99
    assert request.conversation.history_omitted


def test_ranked_private_records_and_dependency_snapshot_block_concurrent_revision(
    setup,
):
    from app.cognition.pipeline import CognitionEngine
    from app.cognition.contracts import InterestMemoryProposal
    from app.core.self_state import InterestStatus

    store, _, gate, fake, _ = setup
    runtime = Stage0ConversationRuntime(store, gate, fake, memory_extractor=extractor())
    runtime.submit(principal("a"), "seed", "Astronomy")
    current = append_event(store, principal("a"), "current", "Astronomy")
    source = append_event(store, principal("a"), "new-evidence", "New observations")
    started = Event()

    def revise():
        started.set()
        proposal = InterestMemoryProposal(
            proposal_context(source, "revise", expected_revision=1),
            "astronomy",
            "Planetary astronomy",
            60,
            InterestStatus.ACTIVE,
            "Reconsideration",
        )
        return CognitionEngine(
            store, SelfStateStore(store), extractor=FakeExtractor(proposal)
        ).process(principal("a"), source)

    db = store._open()
    try:
        with ThreadPoolExecutor() as pool:
            db.execute("BEGIN IMMEDIATE")
            result = pool.submit(revise)
            assert started.wait(5)
            request, sources = MemoryConversationAssembler(
                store, ContextLimits(32000, 99)
            ).build(principal("a"), current, db)
            assert request.memories[0].text == "Астрономия"
            assert source not in sources
            assert not result.done()
            db.commit()
            outcome = result.result(timeout=10)
            assert outcome.status.value == "persisted", outcome
    finally:
        db.close()
    request, sources = build(store, source)
    assert request.memories[0].text == "Planetary astronomy"
    assert source in sources


def test_ranking_and_record_cap_are_scoped_before_selection(setup):
    from app.cognition.pipeline import CognitionEngine
    from app.cognition.contracts import InterestMemoryProposal
    from app.core.self_state import InterestStatus

    store, _, _, _, _ = setup
    for name, count in [("a", 17), ("b", 1)]:
        for i in range(count):
            context = principal(name)
            source = append_event(store, context, f"{name}-{i}")
            proposal = InterestMemoryProposal(
                proposal_context(source, f"{name}-{i}"),
                f"interest-{i}",
                "Astronomy" if i == 0 else "Botany",
                99 if name == "b" else 50,
                InterestStatus.ACTIVE,
                "Synthetic observations",
            )
            assert (
                CognitionEngine(
                    store, SelfStateStore(store), extractor=FakeExtractor(proposal)
                )
                .process(context, source)
                .status.value
                == "persisted"
            )
    source = append_event(store, principal("a"), "current", "Astronomy")
    request, sources = build(store, source)
    assert len(request.memories) == 16
    assert request.memories[0].text == "Astronomy"
    assert request.memories_omitted
    assert all(item.priority == 50 for item in request.memories)
    assert "event-b-0" not in sources
    assert build(store, source) == (request, sources)
