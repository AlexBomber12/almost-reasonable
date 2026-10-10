from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import sqlite3

import pytest

from app.cognition import (
    CognitionEngine,
    CognitionOutcomeStatus,
    ExtractorIdentity,
    InterestMemoryProposal,
    ProposalContext,
)
from app.cognition.read_store import CognitionReadStore
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
from app.core.self_state import InterestStatus
from app.core.self_state_store import SelfStateStore
from app.core.store import EventStore, EventStoreError


NOW = datetime(2026, 3, 4, 10, 0, tzinfo=UTC)


class FakeExtractor:
    identity = ExtractorIdentity("fake-model-v1", "memory-prompt-v1")

    def __init__(self, proposal):
        self.proposal = proposal

    def extract(self, _request):
        return self.proposal


@pytest.fixture
def read_setup(tmp_path):
    events = EventStore(tmp_path / "events.sqlite3")
    state = SelfStateStore(events)
    context = TrustedPrincipalContext(
        ParticipantId("participant-a"),
        AuthenticatedSourceReference("session-a"),
    )
    events.append_private_input(
        context,
        PrivateInputEvent(
            "event-a",
            "payload-a",
            "message-a",
            NOW,
            EventType.PARTICIPANT_INPUT,
            EventChannel.PRIVATE_CHAT,
            "correlation-a",
            EventAudience.private_for(context.principal_id),
            Provenance(
                ProvenanceType.PARTICIPANT_ASSERTION,
                VerificationStatus.UNVERIFIED,
            ),
            "Synthetic private evidence.",
        ),
    )
    proposal = InterestMemoryProposal(
        ProposalContext(
            "operation-a",
            "event-a",
            ("event-a",),
            (),
            None,
            "Synthetic rationale.",
            NOW + timedelta(minutes=1),
            NOW + timedelta(days=1),
        ),
        "interest-a",
        "State integrity",
        50,
        InterestStatus.ACTIVE,
        "Corruption must fail closed.",
    )
    assert (
        CognitionEngine(events, state, extractor=FakeExtractor(proposal))
        .process(context, "event-a")
        .status
        is CognitionOutcomeStatus.PERSISTED
    )
    return events, state, context


def test_malformed_exact_event_is_translated(read_setup):
    events, state, context = read_setup
    connection = sqlite3.connect(events.database_path)
    with connection:
        connection.execute(
            "UPDATE protected_event_taints SET source_principal_id = 'corrupt'"
        )
    connection.close()

    with pytest.raises(EventStoreError, match="event store operation failed"):
        CognitionReadStore(events, state).retrieve_exact_private_event(
            context, "event-a"
        )


def test_exact_event_sql_failure_is_translated(read_setup, monkeypatch):
    events, state, context = read_setup
    monkeypatch.setattr(
        events,
        "_verify_current_schema",
        lambda _connection: (_ for _ in ()).throw(sqlite3.OperationalError("boom")),
    )

    with pytest.raises(EventStoreError, match="event store operation failed"):
        CognitionReadStore(events, state).retrieve_exact_private_event(
            context, "event-a"
        )


def test_replay_sql_failure_is_translated(read_setup, monkeypatch):
    events, state, context = read_setup
    monkeypatch.setattr(
        state,
        "_find_operation",
        lambda *_args: (_ for _ in ()).throw(sqlite3.OperationalError("boom")),
    )
    change = SimpleNamespace(revision=SimpleNamespace(operation_id="operation-a"))

    with pytest.raises(EventStoreError, match="event store operation failed"):
        CognitionReadStore(events, state).replay_private_operation(context, change)


def test_malformed_candidate_is_translated(read_setup):
    events, state, context = read_setup
    connection = sqlite3.connect(events.database_path)
    with connection:
        connection.execute("UPDATE state_revision_payloads SET topic = '   '")
    connection.close()

    with pytest.raises(EventStoreError, match="event store operation failed"):
        CognitionReadStore(events, state).retrieve_exact_private_candidate(
            context, "interest-a"
        )


def test_candidate_sql_failure_is_translated(read_setup, monkeypatch):
    events, state, context = read_setup
    monkeypatch.setattr(
        events,
        "_verify_current_schema",
        lambda _connection: (_ for _ in ()).throw(sqlite3.OperationalError("boom")),
    )

    with pytest.raises(EventStoreError, match="event store operation failed"):
        CognitionReadStore(events, state).retrieve_all_private_candidates(context)
