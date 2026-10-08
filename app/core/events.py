"""Typed event-domain values for the trusted application boundary.

``TrustedPrincipalContext`` is authorization input from a future authenticated
application layer. Constructing one is not authentication. In particular, it
must never be populated from model output, chat text, or an arbitrary client
supplied participant identifier.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class AudienceType(StrEnum):
    """Closed audience vocabulary supported by this storage slice."""

    PRIVATE_PARTICIPANT = "private_participant"


class ProvenanceType(StrEnum):
    """Where a claim came from, independently of its verification state."""

    DIRECT_OBSERVATION = "direct_observation"
    PARTICIPANT_ASSERTION = "participant_assertion"
    EXTERNAL_CLAIM = "external_claim"
    MODEL_INFERENCE = "model_inference"
    INDEPENDENTLY_VERIFIED_CLAIM = "independently_verified_claim"


class VerificationStatus(StrEnum):
    """Whether the configured independent verification rule has succeeded."""

    UNVERIFIED = "unverified"
    VERIFIED = "verified"


class EventType(StrEnum):
    """Closed event vocabulary implemented by this first storage slice."""

    PARTICIPANT_INPUT = "participant_input"


class EventChannel(StrEnum):
    """Closed channel vocabulary implemented by this first storage slice."""

    PRIVATE_CHAT = "private_chat"


@dataclass(frozen=True, slots=True)
class ParticipantId:
    """Opaque application-owned participant identity."""

    value: str


@dataclass(frozen=True, slots=True)
class AuthenticatedSourceReference:
    """Opaque reference established by a trusted authentication boundary."""

    value: str


@dataclass(frozen=True, slots=True)
class EventAudience:
    """An audience category and its private participant scope."""

    kind: AudienceType
    principal_id: ParticipantId

    @classmethod
    def private_for(cls, principal_id: ParticipantId) -> "EventAudience":
        return cls(AudienceType.PRIVATE_PARTICIPANT, principal_id)


@dataclass(frozen=True, slots=True)
class Provenance:
    """Source classification kept separate from verification."""

    source_type: ProvenanceType
    verification: VerificationStatus


@dataclass(frozen=True, slots=True)
class TrustedPrincipalContext:
    """Principal data supplied only after upstream authentication.

    The storage layer enforces scope after receiving this value; it does not
    authenticate the person represented by it. ``is_owner`` deliberately does
    not broaden conversational retrieval.
    """

    principal_id: ParticipantId
    authenticated_source: AuthenticatedSourceReference
    is_owner: bool = False


@dataclass(frozen=True, slots=True)
class PrivateInputEvent:
    """A permitted participant input ready for atomic persistence."""

    event_id: str
    payload_id: str
    source_message_id: str
    occurred_at: datetime
    event_type: EventType
    channel: EventChannel
    correlation_id: str
    audience: EventAudience
    provenance: Provenance
    content: str


@dataclass(frozen=True, slots=True)
class PrivateConversationEvent:
    """Conversation-safe view without storage, source, or protected taint data."""

    event_id: str
    occurred_at: datetime
    event_type: EventType
    channel: EventChannel
    correlation_id: str
    audience: AudienceType
    provenance: Provenance
    content: str
