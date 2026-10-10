"""Typed values for privacy control operations.

These values are accepted only from an authenticated control-plane boundary.  A
``TrustedPrivacyContext`` is evidence supplied by that boundary; constructing it
inside the application does not itself authenticate anybody.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
import re


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")


def require_token(value: object, name: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def require_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"invalid {name}")
    normalized = value.astimezone(UTC)
    if normalized.utcoffset() != value.utcoffset():
        raise ValueError(f"{name} must be represented in UTC")
    return value


class OperationOrigin(StrEnum):
    CONTROL = "authenticated_control"
    CHAT = "ordinary_chat"
    MODEL = "model_output"


class ConsentPurpose(StrEnum):
    IDENTIFIABLE_QUOTATION = "identifiable_quotation"
    IDENTIFIABLE_DISCLOSURE = "identifiable_disclosure"


class ContentCategory(StrEnum):
    PRIVATE_PAYLOAD = "private_payload"
    PRIVATE_MEMORY = "private_memory"
    SUMMARY = "summary"
    REVISION = "revision"
    SHARED_ABSTRACTION = "shared_abstraction"
    SEARCH_INDEX = "search_index"
    DUPLICATE_KEY = "duplicate_key"
    PUBLIC_ACTION = "public_action"

    @classmethod
    def required_erasure_categories(cls) -> tuple["ContentCategory", ...]:
        return tuple(category for category in cls if category is not cls.PUBLIC_ACTION)


class DeletionDisposition(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    INCOMPLETE_BLOCKED = "incomplete_blocked"
    INVALID_AUTHORIZATION = "invalid_authorization"
    NOT_ELIGIBLE = "not_eligible"


class AbstractionKind(StrEnum):
    BELIEF = "belief"
    INTEREST = "interest"
    RESEARCH_QUESTION = "research_question"
    GOAL = "goal"
    MISSION_PREFERENCE = "mission_preference"
    STYLE_DEVELOPMENT = "style_development"


class RestoreDisposition(StrEnum):
    ELIGIBLE_FOR_ACTIVATION = "eligible_for_activation"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class TrustedPrivacyContext:
    principal_id: str
    actor_reference: str
    authentication_event_id: str
    origin: OperationOrigin

    def __post_init__(self) -> None:
        require_token(self.principal_id, "principal id")
        require_token(self.actor_reference, "actor reference")
        require_token(self.authentication_event_id, "authentication event id")
        if not isinstance(self.origin, OperationOrigin):
            raise ValueError("invalid operation origin")


@dataclass(frozen=True, slots=True)
class ConsentGrant:
    consent_id: str
    operation_id: str
    principal_id: str
    content_reference: str
    audience: str
    purpose: ConsentPurpose
    granted_at: datetime
    expires_at: datetime
    actor_reference: str
    revision: int
    correlation_id: str
    authentication_event_id: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.consent_id, "consent id"),
            (self.operation_id, "operation id"),
            (self.principal_id, "principal id"),
            (self.content_reference, "content reference"),
            (self.audience, "audience"),
            (self.actor_reference, "actor reference"),
            (self.correlation_id, "correlation id"),
            (self.authentication_event_id, "authentication event id"),
        ):
            require_token(value, name)
        if not isinstance(self.purpose, ConsentPurpose):
            raise ValueError("invalid consent purpose")
        require_utc(self.granted_at, "grant timestamp")
        require_utc(self.expires_at, "expiration")
        if self.expires_at <= self.granted_at:
            raise ValueError("expiration must follow grant timestamp")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 1
        ):
            raise ValueError("invalid consent revision")


@dataclass(frozen=True, slots=True)
class OperationResult:
    created: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ConsentUseResult:
    allowed: bool
    reason: str
    consent_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeletionRequest:
    operation_id: str
    principal_id: str
    content_reference: str
    content_category: ContentCategory
    requested_at: datetime
    actor_reference: str
    correlation_id: str
    authentication_event_id: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.operation_id, "operation id"),
            (self.principal_id, "principal id"),
            (self.content_reference, "content reference"),
            (self.actor_reference, "actor reference"),
            (self.correlation_id, "correlation id"),
            (self.authentication_event_id, "authentication event id"),
        ):
            require_token(value, name)
        if not isinstance(self.content_category, ContentCategory):
            raise ValueError("invalid content category")
        require_utc(self.requested_at, "request timestamp")


@dataclass(frozen=True, slots=True)
class ErasureReport:
    payload_removed: bool
    tombstone_written: bool
    invalidated_categories: frozenset[ContentCategory]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeletionOutcome:
    operation_id: str
    disposition: DeletionDisposition
    reason: str


@dataclass(frozen=True, slots=True)
class DeletionRecord:
    request: DeletionRequest
    disposition: DeletionDisposition
    reason: str
    updated_at: datetime

    @property
    def operation_id(self) -> str:
        return self.request.operation_id

    @property
    def content_reference(self) -> str:
        return self.request.content_reference


@dataclass(frozen=True, slots=True)
class AuditRecord:
    correlation_id: str
    actor_reference: str
    action_type: str
    record_category: str
    occurred_at: datetime
    result_code: str
    redaction_status: str


@dataclass(frozen=True, slots=True)
class AbstractionCandidate:
    candidate_id: str
    kind: AbstractionKind
    text: str
    source_audience: str
    protected_source_references: tuple[str, ...]
    semantic_screen_passed: bool
    direct_identifiers: tuple[str, ...] = ()
    private_canaries: tuple[str, ...] = ()
    contains_personal_information: bool = False
    contains_quotation: bool = False
    recognizable_paraphrase: bool = False
    contains_relationship_detail: bool = False
    contains_attribution: bool = False
    max_source_overlap: float = 0.0


@dataclass(frozen=True, slots=True)
class BoundaryDecision:
    accepted: bool
    reason: str
    activation_allowed: bool = False
    protected_source_references: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BackupMetadata:
    path: Path
    schema_version: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RestoreResult:
    restore_id: str
    disposition: RestoreDisposition
    activation_eligible: bool
    reason: str
