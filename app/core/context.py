"""Privacy-scoped, deterministic assembly of bounded conversation context.

The byte budget counts the UTF-8 bytes of every model-visible string field:
Core Self version, identity, and principles, followed by each history segment's
role, trust label, and complete content. It does not estimate provider tokens or
serialized request overhead; a future model adapter must enforce those limits.
"""

from dataclasses import dataclass
from enum import StrEnum

from .core_self import CoreSelf, initial_core_self
from .events import TrustedPrincipalContext
from .store import EventStore, MAX_PRIVATE_EVENTS_PER_RETRIEVAL


MAX_CONTEXT_UTF8_BYTES = 1024 * 1024
# Keep one bounded EventStore result as lookahead so retrieval-limit omission is
# always observable without a second query or an unbounded load.
MAX_CONTEXT_HISTORY_EVENTS = MAX_PRIVATE_EVENTS_PER_RETRIEVAL - 1


class ContextRole(StrEnum):
    """Future adapter role for a context segment."""

    PARTICIPANT = "participant"


class ContextTrust(StrEnum):
    """Trust classification kept distinct from model instruction roles."""

    UNTRUSTED_CONVERSATIONAL_DATA = "untrusted_conversational_data"


@dataclass(frozen=True, slots=True)
class ConversationSegment:
    """A complete, unprivileged participant message."""

    role: ContextRole
    trust: ContextTrust
    content: str


@dataclass(frozen=True, slots=True)
class PrivateConversationContext:
    """Purpose-specific model input with no authentication or storage IDs."""

    core_self: CoreSelf
    history: tuple[ConversationSegment, ...]
    history_omitted: bool
    used_utf8_bytes: int
    max_utf8_bytes: int


def context_utf8_size(
    core_self: CoreSelf, history: tuple[ConversationSegment, ...]
) -> int:
    """Return the documented model-visible UTF-8 content size."""

    strings = (
        core_self.version,
        core_self.identity,
        *core_self.principles,
        *(
            value
            for segment in history
            for value in (segment.role.value, segment.trust.value, segment.content)
        ),
    )
    return sum(len(value.encode("utf-8")) for value in strings)


def minimum_context_utf8_bytes() -> int:
    """Return the mandatory Core Self footprint under the byte-budget rules."""

    return context_utf8_size(initial_core_self(), ())


@dataclass(frozen=True, slots=True)
class ContextLimits:
    """Explicit bounds applied before context can reach a model adapter."""

    max_utf8_bytes: int
    max_history_events: int

    def __post_init__(self) -> None:
        if (
            type(self.max_utf8_bytes) is not int
            or not 1 <= self.max_utf8_bytes <= MAX_CONTEXT_UTF8_BYTES
        ):
            raise ValueError("max_utf8_bytes must be a positive bounded integer")
        if self.max_utf8_bytes < minimum_context_utf8_bytes():
            raise ValueError("max_utf8_bytes cannot fit the mandatory Core Self")
        if (
            type(self.max_history_events) is not int
            or not 1 <= self.max_history_events <= MAX_CONTEXT_HISTORY_EVENTS
        ):
            raise ValueError("max_history_events must be a positive bounded integer")


class PrivateContextAssembler:
    """Stateless participant-private context builder over the EventStore API."""

    def __init__(self, event_store: EventStore, limits: ContextLimits):
        self._event_store = event_store
        self._limits = limits

    def build(
        self, trusted_context: TrustedPrincipalContext
    ) -> PrivateConversationContext:
        """Retrieve authorized data first, then prefer recent whole messages."""

        core_self = initial_core_self()
        retrieved = self._event_store.retrieve_private_events(trusted_context)
        history_omitted = False
        selected: tuple[ConversationSegment, ...] = ()

        for event in reversed(retrieved):
            if len(selected) == self._limits.max_history_events:
                history_omitted = True
                break
            segment = ConversationSegment(
                role=ContextRole.PARTICIPANT,
                trust=ContextTrust.UNTRUSTED_CONVERSATIONAL_DATA,
                content=event.content,
            )
            proposed = (segment, *selected)
            if context_utf8_size(core_self, proposed) <= self._limits.max_utf8_bytes:
                selected = proposed
            else:
                history_omitted = True

        used_utf8_bytes = context_utf8_size(core_self, selected)
        return PrivateConversationContext(
            core_self=core_self,
            history=selected,
            history_omitted=history_omitted,
            used_utf8_bytes=used_utf8_bytes,
            max_utf8_bytes=self._limits.max_utf8_bytes,
        )
