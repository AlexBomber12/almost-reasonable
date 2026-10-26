"""Closed data types and validation for the Threads integration boundary."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import hashlib
import re
from typing import Protocol


MAX_CONTENT_CHARS = 500
MAX_CONTENT_BYTES = 8_192
MAX_INBOUND_CONTENT_BYTES = 16_384
MAX_REFERENCE_LENGTH = 128
MAX_PROVIDER_RESPONSE_BYTES = 2_097_152
REQUIRED_PUBLISH_SCOPES = frozenset(("threads_basic", "threads_content_publish"))
REQUIRED_REPLY_READ_SCOPES = frozenset(("threads_basic", "threads_read_replies"))
REQUIRED_MENTION_SCOPES = frozenset(("threads_basic", "threads_manage_mentions"))
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_APPROVAL_AUTHORITY = object()


class OutboxState(StrEnum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_policy_or_operator_approval"
    READY = "ready"
    SENDING = "sending"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"
    UNKNOWN_ABANDONED = "unknown_abandoned"
    CANCELLED = "cancelled"
    FAILED_TERMINAL = "failed_terminal"


class PublicationType(StrEnum):
    POST = "post"
    REPLY = "reply"


class ApprovalPurpose(StrEnum):
    PUBLICATION = "publication"
    ABANDON_UNKNOWN = "abandon_unknown"
    RECOVER_RECONCILIATION = "recover_reconciliation"


class SourceProvenance(StrEnum):
    PUBLIC = "public"
    APPROVED_PUBLIC_REFERENCE = "approved_public_reference"
    STRUCTURED_PRIVATE_CONSENT = "structured_private_consent"
    PRIVATE_UNAUTHORIZED = "private_unauthorized"


class Stage(StrEnum):
    DISABLED = "disabled"
    SIMULATION = "simulation"
    STAGE_3 = "stage_3"


class InboundKind(StrEnum):
    REPLY = "reply"
    MENTION = "mention"


class ConversationOutcome(StrEnum):
    NO_ACTION = "no_action"
    DEFER = "defer"
    TERMINATE_CONVERSATION = "terminate_conversation"
    COOLDOWN = "cooldown"


@dataclass(frozen=True, slots=True)
class Approval:
    reference: str
    account_ref: str
    operation_id: str
    expires_at: datetime
    revoked: bool
    duplicate_risk_acknowledged: bool
    _authority: object
    purpose: ApprovalPurpose = ApprovalPurpose.PUBLICATION


@dataclass(frozen=True, slots=True)
class PublicationAuthorizationSnapshot:
    """Atomic trusted view of publication approval and per-draft privacy."""

    approval: Approval | None
    privacy_authorized: bool


def issue_approval(
    *,
    reference: str,
    account_ref: str,
    operation_id: str,
    expires_at: datetime,
    revoked: bool = False,
    duplicate_risk_acknowledged: bool = False,
    purpose: ApprovalPurpose = ApprovalPurpose.PUBLICATION,
) -> Approval:
    """Represent approval evidence authenticated by a trusted boundary."""

    require_reference("approval reference", reference)
    require_reference("account reference", account_ref)
    require_reference("operation ID", operation_id)
    require_aware_time("approval expiry", expires_at)
    if type(revoked) is not bool or type(duplicate_risk_acknowledged) is not bool:
        raise ValueError("invalid approval flags")
    if type(purpose) is not ApprovalPurpose:
        raise ValueError("invalid approval purpose")
    return Approval(
        reference,
        account_ref,
        operation_id,
        expires_at,
        revoked,
        duplicate_risk_acknowledged,
        _APPROVAL_AUTHORITY,
        purpose,
    )


@dataclass(slots=True)
class AccountPolicy:
    """Mutable trusted runtime controls, rechecked immediately before effects."""

    account_ref: str
    stage: Stage
    simulation_enabled: bool
    account_authorized: bool
    scopes: frozenset[str]
    privacy_gate_available: bool
    russian_output_gate_available: bool
    publication_capability_enabled: bool
    emergency_stop: bool = False

    def validate(self) -> None:
        require_reference("account reference", self.account_ref)
        if type(self.stage) is not Stage:
            raise ValueError("invalid deployment stage")
        flags = (
            self.simulation_enabled,
            self.account_authorized,
            self.privacy_gate_available,
            self.russian_output_gate_available,
            self.publication_capability_enabled,
            self.emergency_stop,
        )
        if any(type(flag) is not bool for flag in flags):
            raise ValueError("invalid policy flag")
        if not isinstance(self.scopes, frozenset) or any(
            not isinstance(scope, str) for scope in self.scopes
        ):
            raise ValueError("invalid account scopes")

    def publication_blockers(
        self, publication_type: PublicationType, *, live_transport_enabled: bool = False
    ) -> tuple[str, ...]:
        self.validate()
        blockers = []
        if live_transport_enabled and self.stage is not Stage.STAGE_3:
            blockers.append("live Threads transport requires Stage 3")
        if self.stage is Stage.DISABLED:
            blockers.append("public stage is disabled")
        elif self.stage is Stage.STAGE_3:
            if not live_transport_enabled:
                blockers.append("live Threads transport is not implemented")
        elif not self.simulation_enabled:
            blockers.append("simulation capability is disabled")
        if not self.account_authorized:
            blockers.append("account is not authorized")
        required = REQUIRED_PUBLISH_SCOPES
        if not required <= self.scopes:
            blockers.append("required publication scopes are unavailable")
        if publication_type is PublicationType.REPLY and not (
            REQUIRED_REPLY_READ_SCOPES <= self.scopes
        ):
            blockers.append("required reply scopes are unavailable")
        if (
            live_transport_enabled
            and publication_type is PublicationType.REPLY
            and "threads_manage_replies" not in self.scopes
        ):
            blockers.append("required reply publication scope is unavailable")
        if not self.privacy_gate_available:
            blockers.append("privacy gate is unavailable")
        if not self.russian_output_gate_available:
            blockers.append("Russian output gate is unavailable")
        if not self.publication_capability_enabled:
            blockers.append("publication capability is disabled")
        if self.emergency_stop:
            blockers.append("emergency stop is active")
        return tuple(blockers)


@dataclass(frozen=True, slots=True)
class OutboxItem:
    operation_id: str
    account_ref: str
    publication_type: PublicationType
    parent_message_id: str | None
    approval_reference: str | None
    approval_status: str
    draft_id: str
    content: str
    content_fingerprint: str
    source_provenance: SourceProvenance
    privacy_authorized: bool
    russian_output_authorized: bool
    state: OutboxState
    revision: int
    correlation_id: str
    attempt_count: int
    provider_container_id: str | None
    provider_publication_id: str | None
    dispatch_evidence: str | None
    reconciliation_status: str
    replacement_of: str | None
    duplicate_risk_approval_ref: str | None
    created_at: datetime
    updated_at: datetime


class CurrentAuthorizationProvider(Protocol):
    """Trusted application boundary for current approval and privacy state."""

    def current_approval(
        self,
        *,
        account_ref: str,
        operation_id: str,
        approval_reference: str,
        now: datetime,
    ) -> Approval | None: ...

    def current_publication_authorization(
        self,
        *,
        item: OutboxItem,
        approval_reference: str,
        now: datetime,
    ) -> PublicationAuthorizationSnapshot: ...


@dataclass(frozen=True, slots=True)
class InboundEvent:
    event_id: str
    correlation_id: str
    account_ref: str
    source_ref: str
    kind: InboundKind
    conversation_id: str
    parent_message_id: str | None
    occurred_at: datetime
    content: str


@dataclass(frozen=True, slots=True)
class DeferredInboundEvent:
    event: InboundEvent
    outcome_id: str
    outcome_correlation_id: str
    reason: str
    resume_condition: str
    deferred_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationControl:
    account_ref: str
    stream: InboundKind
    conversation_id: str
    source_outcome_id: str
    control: ConversationOutcome
    reason: str
    started_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class PollResult:
    new_events: tuple[InboundEvent, ...]
    duplicate_count: int
    pages: int
    final_cursor: str | None


def validate_approval(
    approval: Approval,
    *,
    account_ref: str,
    operation_id: str,
    now: datetime,
    require_duplicate_ack: bool = False,
    purpose: ApprovalPurpose = ApprovalPurpose.PUBLICATION,
) -> None:
    if type(approval) is not Approval or approval._authority is not _APPROVAL_AUTHORITY:
        raise ValueError("approval is not authenticated")
    require_aware_time("current time", now)
    if approval.account_ref != account_ref or approval.operation_id != operation_id:
        raise ValueError("approval target mismatch")
    if approval.purpose is not purpose:
        raise ValueError("approval purpose mismatch")
    if approval.revoked:
        raise ValueError("approval is revoked")
    if approval.expires_at <= now:
        raise ValueError("approval is expired")
    if require_duplicate_ack and not approval.duplicate_risk_acknowledged:
        raise ValueError("duplicate risk was not acknowledged")


def authenticated_approval_reference(
    approval: object, *, account_ref: str, operation_id: str
) -> str | None:
    """Return attribution only for an authentic approval bound to this operation."""

    if (
        type(approval) is Approval
        and approval._authority is _APPROVAL_AUTHORITY
        and approval.account_ref == account_ref
        and approval.operation_id == operation_id
    ):
        return approval.reference
    return None


def validate_content(content: object, *, inbound: bool = False) -> str:
    if not isinstance(content, str) or not content or "\x00" in content:
        raise ValueError("invalid content")
    byte_limit = MAX_INBOUND_CONTENT_BYTES if inbound else MAX_CONTENT_BYTES
    if len(content.encode("utf-8")) > byte_limit:
        raise ValueError("content exceeds byte bound")
    if not inbound and len(content) > MAX_CONTENT_CHARS:
        raise ValueError("content exceeds Threads text bound")
    return content


def content_fingerprint(content: str) -> str:
    normalized = " ".join(content.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def require_reference(field: str, value: object) -> str:
    if not isinstance(value, str) or _REFERENCE.fullmatch(value) is None:
        raise ValueError(f"invalid {field}")
    return value


def require_aware_time(field: str, value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"invalid {field}")
    try:
        offset = value.utcoffset()
    except Exception as error:
        raise ValueError(f"invalid {field}") from error
    if offset is None:
        raise ValueError(f"invalid {field}")
    return value
