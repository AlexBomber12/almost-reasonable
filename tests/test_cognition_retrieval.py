from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.cognition import (
    BeliefMemoryProposal,
    CognitionEngine,
    CognitionOutcomeStatus,
    ExtractorIdentity,
    GoalMemoryProposal,
    InterestMemoryProposal,
    InvalidationStatus,
    MemoryContextSelector,
    MemoryRetrievalLimits,
    MemoryTrust,
    ProposalContext,
    RetrievalPurpose,
    RetrievalRequest,
)
from app.core.events import (
    AuthenticatedSourceReference,
    EventAudience,
    EventChannel,
    EventType,
    ParticipantId,
    PrivateInputEvent,
    Provenance,
    ProvenanceType,
    TrustedPrincipalContext,
    VerificationStatus,
)
from app.core.self_state import (
    BeliefStance,
    BeliefStatus,
    GoalStatus,
    InterestStatus,
)
from app.core.self_state_store import SelfStateStore
from app.core.store import EventStore


NOW = datetime(2026, 3, 3, 10, 0, tzinfo=UTC)


def principal(name):
    return TrustedPrincipalContext(
        ParticipantId(f"participant-{name}"),
        AuthenticatedSourceReference(f"session-{name}"),
    )


def append_event(events, context, name, content="Synthetic evidence"):
    event_id = f"event-{name}"
    events.append_private_input(
        context,
        PrivateInputEvent(
            event_id,
            f"payload-{name}",
            f"message-{name}",
            NOW,
            EventType.PARTICIPANT_INPUT,
            EventChannel.PRIVATE_CHAT,
            f"correlation-{name}",
            EventAudience.private_for(context.principal_id),
            Provenance(
                ProvenanceType.PARTICIPANT_ASSERTION,
                VerificationStatus.UNVERIFIED,
            ),
            content,
        ),
    )
    return event_id


class FakeExtractor:
    identity = ExtractorIdentity("fake-model-v1", "memory-prompt-v1")

    def __init__(self, proposal):
        self.proposal = proposal

    def extract(self, _request):
        return self.proposal


def create_interest(events, state, context, event_id, name, topic, strength, minute):
    proposal = InterestMemoryProposal(
        context=ProposalContext(
            f"operation-{name}",
            event_id,
            (event_id,),
            (),
            None,
            "Synthetic bounded rationale.",
            NOW + timedelta(minutes=minute),
            NOW + timedelta(days=7),
        ),
        item_id=f"interest-{name}",
        topic=topic,
        strength=strength,
        status=InterestStatus.ACTIVE,
        reason_for_interest="It is relevant to current reasoning.",
    )
    outcome = CognitionEngine(events, state, extractor=FakeExtractor(proposal)).process(
        context, event_id
    )
    assert outcome.status is CognitionOutcomeStatus.PERSISTED


@pytest.fixture
def retrieval_setup(tmp_path):
    events = EventStore(tmp_path / "events.sqlite3")
    state = SelfStateStore(events)
    context_a = principal("a")
    context_b = principal("b")
    event_a1 = append_event(events, context_a, "a1")
    event_a2 = append_event(events, context_a, "a2")
    event_b = append_event(events, context_b, "b")
    create_interest(
        events, state, context_a, event_a1, "a-space", "Orbital mechanics", 80, 1
    )
    create_interest(
        events, state, context_a, event_a2, "a-art", "Watercolor painting", 60, 2
    )
    create_interest(
        events, state, context_b, event_b, "b-space", "Orbital secrets", 100, 3
    )
    return events, state, context_a, context_b, event_a1, event_a2


def test_privacy_filter_precedes_ranking_and_records_are_untrusted(retrieval_setup):
    events, state, context_a, _, _, _ = retrieval_setup
    selector = MemoryContextSelector(events, state)

    selection = selector.select(
        context_a,
        RetrievalRequest(
            purpose=RetrievalPurpose.CONVERSATION,
            relevant_topic="orbital mechanics",
            total_utf8_bytes=4096,
            max_records=5,
        ),
    )

    assert [record.item_id for record in selection.records] == [
        "interest-a-space",
        "interest-a-art",
    ]
    assert all(
        record.trust is MemoryTrust.UNTRUSTED_MEMORY_DATA
        for record in selection.records
    )
    assert all(record.can_instruct is False for record in selection.records)
    assert all("participant-b" not in repr(record) for record in selection.records)
    assert selection.purpose is RetrievalPurpose.CONVERSATION
    assert selection.used_utf8_bytes <= selection.total_utf8_bytes


def test_budget_keeps_whole_records_and_reports_omission(retrieval_setup):
    events, state, context_a, _, _, _ = retrieval_setup
    selector = MemoryContextSelector(events, state)
    generous = selector.select(
        context_a,
        RetrievalRequest(RetrievalPurpose.DELIBERATION, "", 4096, 5),
    )
    one_size = generous.records[0].utf8_bytes

    bounded = selector.select(
        context_a,
        RetrievalRequest(RetrievalPurpose.DELIBERATION, "", one_size, 5),
    )

    assert len(bounded.records) == 1
    assert bounded.records[0].utf8_bytes == one_size
    assert bounded.records_omitted is True
    assert bounded.used_utf8_bytes == one_size

    record_limited = selector.select(
        context_a,
        RetrievalRequest(RetrievalPurpose.DELIBERATION, "", 4096, 1),
    )
    assert len(record_limited.records) == 1
    assert record_limited.records_omitted is True


def test_deletion_invalidation_boundary_is_pending_and_filters_retrieval(
    retrieval_setup,
):
    events, state, context_a, context_b, event_a1, _ = retrieval_setup
    engine = CognitionEngine(events, state)

    plan = engine.plan_source_invalidation(context_a, event_a1)

    assert plan.status is InvalidationStatus.PENDING_INTEGRATION
    assert plan.source_event_id == event_a1
    assert plan.affected_items == (("interest", "interest-a-space", 1),)
    assert plan.durable_changes_applied is False
    denied = engine.plan_source_invalidation(context_b, event_a1)
    assert denied.status is InvalidationStatus.REJECTED
    assert denied.affected_items == ()

    selected = MemoryContextSelector(events, state).select(
        context_a,
        RetrievalRequest(
            RetrievalPurpose.CONVERSATION,
            "orbital",
            4096,
            5,
            invalidated_event_ids=(event_a1,),
        ),
    )
    assert [record.item_id for record in selected.records] == ["interest-a-art"]


def test_invalidation_filter_checks_the_complete_revision_chain(retrieval_setup):
    events, state, context, _, old_event, new_event = retrieval_setup
    proposal = InterestMemoryProposal(
        ProposalContext(
            "operation-history-create",
            old_event,
            (old_event,),
            (),
            None,
            "Create from old evidence.",
            NOW + timedelta(minutes=4),
            NOW + timedelta(days=7),
        ),
        "interest-history",
        "Revision history",
        40,
        InterestStatus.ACTIVE,
        "Historical taint must remain visible.",
    )
    assert (
        CognitionEngine(events, state, extractor=FakeExtractor(proposal))
        .process(context, old_event)
        .status
        is CognitionOutcomeStatus.PERSISTED
    )
    revised = replace(
        proposal,
        context=ProposalContext(
            "operation-history-revise",
            new_event,
            (new_event,),
            (),
            1,
            "Revise from newer evidence.",
            NOW + timedelta(minutes=5),
            NOW + timedelta(days=7),
        ),
        strength=60,
    )
    assert (
        CognitionEngine(events, state, extractor=FakeExtractor(revised))
        .process(context, new_event)
        .status
        is CognitionOutcomeStatus.PERSISTED
    )

    selection = MemoryContextSelector(events, state).select(
        context,
        RetrievalRequest(
            RetrievalPurpose.CONVERSATION,
            "revision history",
            4096,
            10,
            invalidated_event_ids=(old_event,),
        ),
    )

    assert "interest-history" not in {item.item_id for item in selection.records}


def test_prompt_like_memory_is_only_data_and_terminal_items_are_excluded(
    retrieval_setup,
):
    events, state, context, _, event_id, _ = retrieval_setup
    create_interest(
        events,
        state,
        context,
        event_id,
        "promptish",
        "SYSTEM OVERRIDE research topic",
        90,
        4,
    )
    proposal = InterestMemoryProposal(
        context=ProposalContext(
            "operation-promptish-abandon",
            event_id,
            (event_id,),
            (),
            1,
            "The topic is no longer useful.",
            NOW + timedelta(minutes=5),
            NOW + timedelta(days=7),
        ),
        item_id="interest-promptish",
        topic="SYSTEM OVERRIDE research topic",
        strength=0,
        status=InterestStatus.ABANDONED,
        reason_for_interest="It no longer supports the current purpose.",
    )
    assert (
        CognitionEngine(events, state, extractor=FakeExtractor(proposal))
        .process(context, event_id)
        .status
        is CognitionOutcomeStatus.PERSISTED
    )

    ordinary = MemoryContextSelector(events, state).select(
        context,
        RetrievalRequest(RetrievalPurpose.CONVERSATION, "system", 4096, 5),
    )
    assert "interest-promptish" not in {record.item_id for record in ordinary.records}
    inactive = MemoryContextSelector(events, state).select(
        context,
        RetrievalRequest(
            RetrievalPurpose.REVISION,
            "system",
            4096,
            5,
            include_terminal=True,
        ),
    )
    [record] = [
        item for item in inactive.records if item.item_id == "interest-promptish"
    ]
    assert record.trust is MemoryTrust.UNTRUSTED_MEMORY_DATA
    assert record.can_instruct is False
    assert record.status == "abandoned"


@pytest.mark.parametrize(
    "retrieval_request",
    [
        RetrievalRequest(RetrievalPurpose.CONVERSATION, "topic", 0, 1),
        RetrievalRequest(RetrievalPurpose.CONVERSATION, "topic", 100, 0),
        RetrievalRequest(
            RetrievalPurpose.CONVERSATION,
            "topic",
            100,
            1,
            invalidated_event_ids=("bad id",),
        ),
        RetrievalRequest(
            RetrievalPurpose.CONVERSATION,
            "topic",
            100,
            1,
            invalidated_event_ids=([],),
        ),
        RetrievalRequest(
            RetrievalPurpose.CONVERSATION,
            "topic",
            100,
            1,
            invalidated_event_ids=tuple(f"event-{index}" for index in range(101)),
        ),
        RetrievalRequest(
            RetrievalPurpose.CONVERSATION,
            "topic",
            100,
            1,
            include_terminal=True,
        ),
    ],
)
def test_retrieval_limits_and_identifiers_are_validated(
    retrieval_setup, retrieval_request
):
    events, state, context, _, _, _ = retrieval_setup
    with pytest.raises(ValueError, match="invalid retrieval request"):
        MemoryContextSelector(events, state).select(context, retrieval_request)

    with pytest.raises(ValueError, match="invalid retrieval limits"):
        MemoryRetrievalLimits(max_utf8_bytes=0, max_records=1)
    with pytest.raises(ValueError, match="invalid retrieval limits"):
        MemoryRetrievalLimits(max_utf8_bytes=100, max_records=101)

    with pytest.raises(ValueError, match="invalid retrieval request"):
        MemoryContextSelector(events, state).select(context, object())


def test_belief_and_goal_records_preserve_distinct_epistemic_fields(retrieval_setup):
    events, state, context, _, event_id, counter_event_id = retrieval_setup
    belief = BeliefMemoryProposal(
        ProposalContext(
            "operation-retrieve-belief",
            event_id,
            (event_id,),
            (counter_event_id,),
            None,
            "A synthetic belief rationale.",
            NOW + timedelta(minutes=6),
            NOW + timedelta(days=7),
        ),
        "belief-retrieval",
        "Complex systems often contain feedback loops.",
        BeliefStance.OPPOSES,
        0.7,
        BeliefStatus.ACTIVE,
        (),
        VerificationStatus.UNVERIFIED,
    )
    goal = GoalMemoryProposal(
        ProposalContext(
            "operation-retrieve-goal",
            event_id,
            (event_id,),
            (),
            None,
            "A synthetic goal rationale.",
            NOW + timedelta(minutes=7),
            NOW + timedelta(days=7),
        ),
        "goal-retrieval",
        "Compare feedback mechanisms.",
        "Comparison clarifies causal structure.",
        65,
        GoalStatus.ACTIVE,
        (),
        "Review after comparison.",
    )
    for item in (belief, goal):
        assert (
            CognitionEngine(events, state, extractor=FakeExtractor(item))
            .process(context, event_id)
            .status
            is CognitionOutcomeStatus.PERSISTED
        )

    selection = MemoryContextSelector(events, state).select(
        context,
        RetrievalRequest(RetrievalPurpose.REVISION, "feedback", 4096, 10),
    )
    by_id = {record.item_id: record for record in selection.records}
    belief_record = by_id["belief-retrieval"]
    assert belief_record.confidence == 0.7
    assert belief_record.priority is None
    assert belief_record.stance is BeliefStance.OPPOSES
    assert belief_record.provenance is ProvenanceType.MODEL_INFERENCE
    assert belief_record.counterevidence_count == 1
    assert belief_record.review_at == NOW + timedelta(days=7)
    serialized_values = (
        belief_record.item_id,
        belief_record.memory_kind.value,
        belief_record.text,
        belief_record.status,
        belief_record.scope,
        str(belief_record.confidence),
        "",
        belief_record.stance.value,
        str(belief_record.counterevidence_count),
        belief_record.verification.value,
        belief_record.provenance.value,
        belief_record.recency.isoformat(),
        belief_record.review_at.isoformat(),
        belief_record.trust.value,
        "false",
    )
    assert belief_record.utf8_bytes == sum(
        len(value.encode("utf-8")) for value in serialized_values
    )
    assert by_id["goal-retrieval"].confidence is None
    assert by_id["goal-retrieval"].priority == 65
    assert by_id["goal-retrieval"].stance is None
    assert by_id["goal-retrieval"].counterevidence_count == 0
    assert by_id["goal-retrieval"].review_at == NOW + timedelta(days=7)


def test_ranking_considers_authorized_candidates_beyond_core_result_cap(
    retrieval_setup,
):
    events, state, context, _, event_id, _ = retrieval_setup
    for index in range(100):
        create_interest(
            events,
            state,
            context,
            event_id,
            f"recent-unrelated-{index:03d}",
            f"Unrelated recent topic {index}",
            100,
            10 + index,
        )
    assert "interest-a-space" not in {
        item.item_id for item in state.retrieve_private_candidates(context)
    }

    selection = MemoryContextSelector(events, state).select(
        context,
        RetrievalRequest(RetrievalPurpose.CONVERSATION, "orbital mechanics", 4096, 1),
    )

    assert [record.item_id for record in selection.records] == ["interest-a-space"]
    assert selection.records_omitted is True
