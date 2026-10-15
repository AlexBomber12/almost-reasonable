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
NOTICE_VERSION = "privacy-notice-ru-v1"


def require_token(value: object, name: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def audit_correlation(value: object, trusted_fallback: str) -> str:
    try:
        return require_token(value, "correlation id")
    except ValueError:
        return require_token(trusted_fallback, "audit correlation fallback")


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

    def __post_init__(self) -> None:
        if (
            type(self.payload_removed) is not bool
            or type(self.tombstone_written) is not bool
        ):
            raise ValueError("invalid erasure report flags")
        if not isinstance(self.invalidated_categories, frozenset) or any(
            not isinstance(category, ContentCategory)
            for category in self.invalidated_categories
        ):
            raise ValueError("invalid erasure report categories")
        if not isinstance(self.blockers, tuple) or any(
            not isinstance(blocker, str) or not _TOKEN.fullmatch(blocker)
            for blocker in self.blockers
        ):
            raise ValueError("invalid erasure report blockers")


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
class PrivacyReconciliationSnapshot:
    generation: int
    deletions: tuple[DeletionRecord, ...]
    revocations: tuple[tuple[str, datetime], ...]


_CONSENT_USE_RESULTS = {
    "already_dispatched",
    "authorized",
    "dispatch_uncertain",
    "expired",
    "invalid_authentication",
    "invalid_authorization",
    "no_matching_consent",
    "not_yet_valid",
    "pre_dispatch_failure",
    "revoked",
    "source_deleted",
}
_DELETION_RESULTS = {
    "pending:awaiting_erasure",
    "incomplete_blocked:adapter_failure",
    "incomplete_blocked:erasure_incomplete",
    "incomplete_blocked:invalid_erasure_report",
}
_AUDIT_SHAPES = frozenset(
    {
        *(
            ("consent_grant", "consent", result, "payload_free")
            for result in {
                "already_granted",
                "granted",
                "invalid_authentication",
                "invalid_authorization",
            }
        ),
        *(
            ("consent_revocation", "consent", result, "payload_free")
            for result in {
                "already_revoked",
                "invalid_authentication",
                "invalid_authorization",
                "revoked",
            }
        ),
        ("consent_use_attempt", "consent", "dispatch_pending", "payload_free"),
        *(
            ("consent_use", "consent", result, "payload_free")
            for result in _CONSENT_USE_RESULTS
        ),
        *(
            (
                "notice_acknowledgement",
                "privacy_notice",
                result,
                "payload_free",
            )
            for result in {"invalid_authentication", "unsupported_notice_version"}
        ),
        *(
            ("deletion", category.value, result, "payload_free")
            for category in ContentCategory
            if category is not ContentCategory.PUBLIC_ACTION
            for result in _DELETION_RESULTS
        ),
        *(
            (
                "deletion",
                category.value,
                "applied:erasure_applied",
                "content_free_tombstone",
            )
            for category in ContentCategory
            if category is not ContentCategory.PUBLIC_ACTION
        ),
        (
            "deletion",
            ContentCategory.PUBLIC_ACTION.value,
            "not_eligible:public_action_not_erasable",
            "payload_free",
        ),
        *(
            ("deletion_rejected", category.value, result, "payload_free")
            for category in ContentCategory
            for result in {
                "invalid_authorization:invalid_authentication",
                "invalid_authorization:wrong_principal",
            }
        ),
    }
)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    correlation_id: str
    actor_reference: str
    action_type: str
    record_category: str
    occurred_at: datetime
    result_code: str
    redaction_status: str

    def __post_init__(self) -> None:
        require_token(self.correlation_id, "audit correlation id")
        require_token(self.actor_reference, "audit actor reference")
        require_utc(self.occurred_at, "audit timestamp")
        shape = (
            self.action_type,
            self.record_category,
            self.result_code,
            self.redaction_status,
        )
        if shape not in _AUDIT_SHAPES:
            raise ValueError("invalid audit shape")


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
    ledger_generation: int | None = None
