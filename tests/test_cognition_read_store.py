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
from app.core.self_state import DecisionOrigin, InterestStatus
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


def test_mismatched_event_and_state_databases_are_rejected(tmp_path):
    events = EventStore(tmp_path / "events-a.sqlite3")
    other_events = EventStore(tmp_path / "events-b.sqlite3")

    with pytest.raises(ValueError, match="same database"):
        CognitionReadStore(events, SelfStateStore(other_events))


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


def test_event_envelope_sql_failure_is_translated(read_setup, monkeypatch):
    events, state, context = read_setup
    monkeypatch.setattr(
        events,
        "_verify_current_schema",
        lambda _connection: (_ for _ in ()).throw(sqlite3.OperationalError("boom")),
    )

    with pytest.raises(EventStoreError, match="event store operation failed"):
        CognitionReadStore(events, state).has_private_event_envelope(context, "event-a")


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


def test_candidate_snapshot_read_malformed_state_is_translated(read_setup):
    events, state, context = read_setup
    connection = sqlite3.connect(events.database_path)
    with connection:
        connection.execute("UPDATE state_revision_payloads SET topic = '   '")
    connection.close()

    with pytest.raises(EventStoreError, match="event store operation failed"):
        CognitionReadStore(events, state).retrieve_all_private_candidate_reads(context)


@pytest.mark.parametrize("invalid_pointer", [0, 2])
def test_all_candidate_read_modes_reject_missing_current_revision_pointers(
    read_setup, invalid_pointer
):
    events, state, context = read_setup
    with sqlite3.connect(events.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE state_items SET current_revision = ?",
            (invalid_pointer,),
        )
    read_store = CognitionReadStore(events, state)

    reads = (
        lambda: state.retrieve_private_candidates(context),
        lambda: read_store.retrieve_exact_private_candidate(context, "interest-a"),
        lambda: read_store.retrieve_all_private_candidates(context),
        lambda: read_store.retrieve_all_private_candidate_reads(context),
    )
    for read in reads:
        with pytest.raises(EventStoreError, match="event store operation failed"):
            read()


def test_candidate_reads_accept_valid_model_and_deterministic_revision_metadata(
    read_setup,
):
    events, state, context = read_setup
    read_store = CognitionReadStore(events, state)

    for include_history in (False, True):
        [model_read] = read_store.retrieve_all_private_candidate_reads(
            context, include_belief_history=include_history
        )
        assert model_read.decision_origin is DecisionOrigin.MODEL
        assert model_read.core_self_version == "core-self-v1"
        assert model_read.model_identifier == "fake-model-v1"
        assert model_read.prompt_version == "memory-prompt-v1"

    with sqlite3.connect(events.database_path) as connection:
        connection.execute(
            """
            UPDATE state_revisions
            SET decision_origin = 'deterministic',
                model_identifier = NULL,
                prompt_version = NULL
            """
        )

    for include_history in (False, True):
        [deterministic_read] = read_store.retrieve_all_private_candidate_reads(
            context, include_belief_history=include_history
        )
        assert deterministic_read.decision_origin is DecisionOrigin.DETERMINISTIC
        assert deterministic_read.core_self_version == "core-self-v1"
        assert deterministic_read.model_identifier is None
        assert deterministic_read.prompt_version is None


@pytest.mark.parametrize("include_history", [False, True])
@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE state_revisions SET core_self_version = ''",
        "UPDATE state_revisions SET core_self_version = 'private-core-secret'",
        "UPDATE state_revisions SET core_self_version = CAST(X'00' AS BLOB)",
        "UPDATE state_revisions SET decision_origin = 'unsupported'",
        "UPDATE state_revisions SET decision_origin = CAST(X'00' AS BLOB)",
        "UPDATE state_revisions SET model_identifier = NULL",
        "UPDATE state_revisions SET model_identifier = 'private model secret'",
        "UPDATE state_revisions SET model_identifier = CAST(X'00' AS BLOB)",
        "UPDATE state_revisions SET prompt_version = NULL",
        "UPDATE state_revisions SET prompt_version = 'private prompt secret'",
        "UPDATE state_revisions SET prompt_version = CAST(X'00' AS BLOB)",
        "UPDATE state_revisions SET model_identifier = NULL, prompt_version = NULL",
        "UPDATE state_revisions SET decision_origin = 'deterministic'",
    ],
)
def test_candidate_reads_sanitize_malformed_revision_metadata(
    read_setup, mutation, include_history
):
    events, state, context = read_setup
    with sqlite3.connect(events.database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(mutation)

    with pytest.raises(EventStoreError) as error:
        CognitionReadStore(events, state).retrieve_all_private_candidate_reads(
            context, include_belief_history=include_history
        )

    assert str(error.value) == "event store operation failed"
    assert "private" not in str(error.value)


def test_candidate_snapshot_read_sql_failure_is_translated(read_setup, monkeypatch):
    events, state, context = read_setup
    monkeypatch.setattr(
        events,
        "_verify_current_schema",
        lambda _connection: (_ for _ in ()).throw(sqlite3.OperationalError("boom")),
    )

    with pytest.raises(EventStoreError, match="event store operation failed"):
        CognitionReadStore(events, state).retrieve_all_private_candidate_reads(context)


def test_candidate_snapshot_batches_provenance_reads(read_setup, monkeypatch):
    events, state, context = read_setup
    second = InterestMemoryProposal(
        ProposalContext(
            "operation-b",
            "event-a",
            ("event-a",),
            (),
            None,
            "Second synthetic rationale.",
            NOW + timedelta(minutes=2),
            NOW + timedelta(days=1),
        ),
        "interest-b",
        "Bounded database work",
        60,
        InterestStatus.ACTIVE,
        "Retrieval must not issue one provenance query per candidate.",
    )
    assert (
        CognitionEngine(events, state, extractor=FakeExtractor(second))
        .process(context, "event-a")
        .status
        is CognitionOutcomeStatus.PERSISTED
    )
    statements = []
    original_open = events._open

    def traced_open():
        connection = original_open()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(events, "_open", traced_open)

    reads = CognitionReadStore(events, state).retrieve_all_private_candidate_reads(
        context
    )

    provenance_queries = [
        statement
        for statement in statements
        if "revision.item_key, source.event_id, source.retention_status" in statement
    ]
    assert len(reads) == 2
    assert len(provenance_queries) == 1


def test_revision_reference_sql_failure_is_translated(read_setup, monkeypatch):
    events, state, context = read_setup
    monkeypatch.setattr(
        events,
        "_verify_current_schema",
        lambda _connection: (_ for _ in ()).throw(sqlite3.OperationalError("boom")),
    )

    with pytest.raises(EventStoreError, match="event store operation failed"):
        CognitionReadStore(events, state).retrieve_private_revision_references(
            context, "event-a"
        )
