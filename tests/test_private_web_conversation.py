from datetime import UTC, datetime, timedelta

import pytest

from app.core.context import ContextLimits, PrivateContextAssembler
from app.core.events import (
    AuthenticatedSourceReference,
    ParticipantId,
    TrustedPrincipalContext,
)
from app.core.store import EventStore
from app.private_web.conversation import (
    ConversationService,
    DisabledInferenceAdapter,
    MessageStatus,
    ModelState,
    SyntheticInferenceAdapter,
)


class Clock:
    def __init__(self):
        self.value = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self):
        result = self.value
        self.value += timedelta(seconds=1)
        return result


def principal(name):
    return TrustedPrincipalContext(
        ParticipantId(f"participant-{name}"),
        AuthenticatedSourceReference(f"session-{name}"),
    )


@pytest.fixture
def components(tmp_path):
    event_store = EventStore(tmp_path / "events.sqlite3")
    assembler = PrivateContextAssembler(
        event_store,
        ContextLimits(max_utf8_bytes=32_000, max_history_events=20),
    )
    inference = SyntheticInferenceAdapter()
    service = ConversationService(event_store, assembler, inference, clock=Clock())
    return event_store, inference, service


def test_two_synthetic_conversations_are_isolated_and_continuous(components):
    _, inference, service = components
    alice = principal("alice")
    bob = principal("bob")

    service.submit(alice, "alice-1", "ALICE-CANARY first")
    service.submit(bob, "bob-1", "BOB-CANARY only")
    service.submit(alice, "alice-2", "ALICE-CANARY second")

    assert [item.content for item in service.history(alice)] == [
        "ALICE-CANARY first",
        "ALICE-CANARY second",
    ]
    assert [item.content for item in service.history(bob)] == ["BOB-CANARY only"]
    assert [segment.content for segment in inference.contexts[-1].history] == [
        "ALICE-CANARY first",
        "ALICE-CANARY second",
    ]
    assert "BOB-CANARY" not in repr(inference.contexts[-1])


def test_message_identity_is_bound_to_authenticated_principal(components):
    _, inference, service = components
    alice = principal("alice")
    bob = principal("bob")
    service.submit(alice, "shared-browser-id", "private A")
    service.submit(bob, "shared-browser-id", "private B")

    assert len(inference.contexts) == 2
    assert service.history(alice)[0].content == "private A"
    assert service.history(bob)[0].content == "private B"


def test_duplicate_incoming_message_id_does_not_repeat_context_or_inference(components):
    _, inference, service = components
    alice = principal("alice")

    first = service.submit(alice, "same-id", "first payload wins")
    duplicate = service.submit(alice, "same-id", "ignored replay")

    assert first.message_status is MessageStatus.CREATED
    assert duplicate.message_status is MessageStatus.DUPLICATE
    assert len(inference.contexts) == 1
    assert [item.content for item in service.history(alice)] == ["first payload wins"]


def test_disabled_adapter_never_calls_a_real_model(tmp_path):
    event_store = EventStore(tmp_path / "events.sqlite3")
    assembler = PrivateContextAssembler(
        event_store,
        ContextLimits(max_utf8_bytes=32_000, max_history_events=20),
    )
    service = ConversationService(
        event_store, assembler, DisabledInferenceAdapter(), clock=Clock()
    )

    result = service.submit(principal("alice"), "message-1", "hello")

    assert result.model_state is ModelState.UNAVAILABLE
    assert result.response_content is None


def test_synthetic_adapter_exposes_pending_state_but_no_model_response(components):
    _, _, service = components
    result = service.submit(principal("alice"), "message-1", "hello")

    assert result.model_state is ModelState.PENDING
    assert result.response_content is None


@pytest.mark.parametrize(
    ("message_id", "content"),
    [
        ("", "hello"),
        ("bad id", "hello"),
        ("x" * 129, "hello"),
        ("message", ""),
        ("message", "x" * 8193),
        (123, "hello"),
        ("message", None),
    ],
)
def test_malformed_message_is_rejected_before_persistence_or_inference(
    components, message_id, content
):
    _, inference, service = components
    with pytest.raises(ValueError):
        service.submit(principal("alice"), message_id, content)
    assert service.history(principal("alice")) == ()
    assert inference.contexts == []


def test_restart_preserves_synthetic_conversation(tmp_path):
    database = tmp_path / "events.sqlite3"
    first_store = EventStore(database)
    first_service = ConversationService(
        first_store,
        PrivateContextAssembler(
            first_store,
            ContextLimits(max_utf8_bytes=32_000, max_history_events=20),
        ),
        SyntheticInferenceAdapter(),
        clock=Clock(),
    )
    first_service.submit(principal("alice"), "message-1", "before restart")

    reopened_store = EventStore(database)
    inference = SyntheticInferenceAdapter()
    restarted_clock = Clock()
    restarted_clock.value += timedelta(minutes=1)
    reopened = ConversationService(
        reopened_store,
        PrivateContextAssembler(
            reopened_store,
            ContextLimits(max_utf8_bytes=32_000, max_history_events=20),
        ),
        inference,
        clock=restarted_clock,
    )
    reopened.submit(principal("alice"), "message-2", "after restart")

    assert [segment.content for segment in inference.contexts[0].history] == [
        "before restart",
        "after restart",
    ]


def test_default_clocks_and_disabled_adapter_are_usable(tmp_path):
    event_store = EventStore(tmp_path / "events.sqlite3")
    service = ConversationService(
        event_store,
        PrivateContextAssembler(
            event_store,
            ContextLimits(max_utf8_bytes=32_000, max_history_events=20),
        ),
    )
    result = service.submit(principal("alice"), "live-clock", "synthetic only")
    assert result.model_state is ModelState.UNAVAILABLE
