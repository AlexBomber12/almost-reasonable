"""Authenticated private-conversation integration without real model execution."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import re
import secrets
from typing import Protocol

from app.core.context import PrivateContextAssembler, PrivateConversationContext
from app.core.events import (
    EventAudience,
    EventChannel,
    EventType,
    PrivateInputEvent,
    Provenance,
    ProvenanceType,
    TrustedPrincipalContext,
    VerificationStatus,
)
from app.core.store import AppendStatus, EventStore


MAX_MESSAGE_UTF8_BYTES = 8192
_MESSAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class ModelState(StrEnum):
    UNAVAILABLE = "unavailable"
    PENDING = "pending"
    COMPLETED = "completed"
    NO_ACTION = "no_action"
    FAILED = "failed"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"
    SKIPPED = "skipped"


class MessageStatus(StrEnum):
    CREATED = "created"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class InferenceObservation:
    """Non-deliverable internal outcome from a disabled or synthetic adapter."""

    model_state: ModelState


class InferenceAdapter(Protocol):
    """Narrow future worker seam; implementations cannot return chat content."""

    def observe(self, context: PrivateConversationContext) -> InferenceObservation: ...


class DisabledInferenceAdapter:
    """Production-safe default that performs no provider or network operation."""

    def observe(self, context: PrivateConversationContext) -> InferenceObservation:
        del context
        return InferenceObservation(ModelState.UNAVAILABLE)


class SyntheticInferenceAdapter:
    """Deterministic in-memory adapter for local continuity tests."""

    def __init__(self) -> None:
        self.contexts: list[PrivateConversationContext] = []

    def observe(self, context: PrivateConversationContext) -> InferenceObservation:
        self.contexts.append(context)
        return InferenceObservation(ModelState.PENDING)


@dataclass(frozen=True, slots=True)
class SubmittedMessage:
    message_status: MessageStatus
    model_state: ModelState
    response_content: str | None = None


@dataclass(frozen=True, slots=True)
class HistoryItem:
    event_id: str
    occurred_at: datetime
    content: str
    role: str = "participant"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ConversationService:
    """Persists and retrieves only within a pre-authenticated principal scope."""

    def __init__(
        self,
        event_store: EventStore,
        context_assembler: PrivateContextAssembler,
        inference: InferenceAdapter | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self._event_store = event_store
        self._context_assembler = context_assembler
        self._inference = inference or DisabledInferenceAdapter()
        self._clock = clock

    def submit(
        self,
        principal: TrustedPrincipalContext,
        message_id: object,
        content: object,
    ) -> SubmittedMessage:
        self._validate_message(message_id, content)
        unique = secrets.token_urlsafe(18)
        event = PrivateInputEvent(
            event_id=f"web-event:{unique}",
            payload_id=f"web-payload:{unique}",
            source_message_id=message_id,
            occurred_at=self._clock(),
            event_type=EventType.PARTICIPANT_INPUT,
            channel=EventChannel.PRIVATE_CHAT,
            correlation_id=f"web-correlation:{secrets.token_urlsafe(18)}",
            audience=EventAudience.private_for(principal.principal_id),
            provenance=Provenance(
                ProvenanceType.PARTICIPANT_ASSERTION,
                VerificationStatus.UNVERIFIED,
            ),
            content=content,
        )
        append = self._event_store.append_private_input(principal, event)
        if append.status is AppendStatus.DUPLICATE:
            return SubmittedMessage(MessageStatus.DUPLICATE, ModelState.UNAVAILABLE)
        context = self._context_assembler.build(principal)
        observation = self._inference.observe(context)
        return SubmittedMessage(MessageStatus.CREATED, observation.model_state)

    def history(self, principal: TrustedPrincipalContext) -> tuple[HistoryItem, ...]:
        events = self._event_store.retrieve_private_events(principal)
        return tuple(
            HistoryItem(event.event_id, event.occurred_at, event.content)
            for event in events
        )

    @staticmethod
    def _validate_message(message_id: object, content: object) -> None:
        if type(message_id) is not str or _MESSAGE_ID.fullmatch(message_id) is None:
            raise ValueError("message_id must be a bounded opaque token")
        if type(content) is not str:
            raise ValueError("content must be text")
        size = len(content.encode("utf-8"))
        if not 1 <= size <= MAX_MESSAGE_UTF8_BYTES:
            raise ValueError("content length is outside the supported range")
