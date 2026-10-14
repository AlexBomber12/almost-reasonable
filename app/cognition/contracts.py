"""Closed, model-facing contracts for cognition proposals and outcomes.

All proposal text is untrusted application data.  These dataclasses describe
what a model may propose; constructing one does not authorize persistence or
grant operational authority.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from app.core.events import Provenance, ProvenanceType, VerificationStatus
from app.core.self_state import (
    BeliefStance,
    BeliefStatus,
    GoalStatus,
    InterestStatus,
    PrivateCandidate,
    StateTransitionResult,
)


class MemoryKind(StrEnum):
    """Closed memory extraction vocabulary."""

    DURABLE_INTEREST = "durable_interest"
    BELIEF_OR_HYPOTHESIS = "belief_or_hypothesis"
    GOAL_OR_INTENTION = "goal_or_intention"
    UNRESOLVED_QUESTION = "unresolved_question"
    CANDIDATE_USEFUL_MEMORY = "candidate_useful_memory"
    CONTRADICTION_COUNTEREVIDENCE = "contradiction_counterevidence"


class CognitionOutcomeStatus(StrEnum):
    PERSISTED = "persisted"
    PENDING_NOT_PERSISTED = "pending_not_persisted"
    REJECTED = "rejected"
    NO_CHANGE = "no_change"


class GenericReviewState(StrEnum):
    PENDING_REVIEW = "pending_review"


class CognitionReason(StrEnum):
    AUTHORIZED = "authorized"
    EXTRACTOR_NOT_CONFIGURED = "extractor_not_configured"
    EXTRACTOR_FAILED = "extractor_failed"
    CAPABILITY_DISABLED = "capability_disabled"
    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_MISMATCH = "source_mismatch"
    EVIDENCE_NOT_FOUND = "evidence_not_found"
    EVIDENCE_ROLE_CONFLICT = "evidence_role_conflict"
    INVALID_PROPOSAL = "invalid_proposal"
    INVALID_MEMORY_KIND = "invalid_memory_kind"
    VERIFICATION_MISMATCH = "verification_mismatch"
    INCONSISTENT_CONFIDENCE = "inconsistent_confidence"
    INVALID_STATUS_TRANSITION = "invalid_status_transition"
    STALE_REVISION = "stale_revision"
    OPERATION_ID_CONFLICT = "operation_id_conflict"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    PERSISTENCE_NOT_AVAILABLE = "persistence_not_available"
    STORE_REJECTED = "store_rejected"


@dataclass(frozen=True, slots=True)
class CognitionCapabilities:
    """Trusted operational switches which never come from model output."""

    memory_extraction: bool = True
    private_state_write: bool = True
    shared_abstraction_screening: bool = True
    generic_memory_write: bool = False
    private_consolidation: bool = False

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (
                self.memory_extraction,
                self.private_state_write,
                self.shared_abstraction_screening,
                self.generic_memory_write,
                self.private_consolidation,
            )
        ):
            raise ValueError("invalid cognition capabilities")


@dataclass(frozen=True, slots=True)
class ExtractorIdentity:
    model_identifier: str
    prompt_version: str


@dataclass(frozen=True, slots=True)
class ProposalContext:
    operation_id: str
    source_event_id: str
    supporting_event_ids: tuple[str, ...]
    counterevidence_event_ids: tuple[str, ...]
    expected_revision: int | None
    reason: str
    proposed_at: datetime
    review_at: datetime


@dataclass(frozen=True, slots=True)
class InterestMemoryProposal:
    context: ProposalContext
    item_id: str
    topic: str
    strength: int
    status: InterestStatus
    reason_for_interest: str
    memory_kind: MemoryKind = MemoryKind.DURABLE_INTEREST


@dataclass(frozen=True, slots=True)
class BeliefMemoryProposal:
    context: ProposalContext
    item_id: str
    proposition: str
    stance: BeliefStance
    confidence: float
    status: BeliefStatus
    alternatives: tuple[str, ...]
    verification: VerificationStatus
    memory_kind: MemoryKind = MemoryKind.BELIEF_OR_HYPOTHESIS


@dataclass(frozen=True, slots=True)
class GoalMemoryProposal:
    context: ProposalContext
    item_id: str
    description: str
    rationale: str
    priority: int
    status: GoalStatus
    constraints: tuple[str, ...]
    review_condition: str
    memory_kind: MemoryKind = MemoryKind.GOAL_OR_INTENTION


@dataclass(frozen=True, slots=True)
class GenericMemoryProposal:
    context: ProposalContext
    memory_kind: MemoryKind
    statement: str
    confidence: float
    verification: VerificationStatus
    item_id: str | None = None
    parent_revisions: tuple[tuple[str, int], ...] = ()
    review_state: GenericReviewState = GenericReviewState.PENDING_REVIEW


@dataclass(frozen=True, slots=True)
class GenericMemoryView:
    """Opaque private snapshot entry exposed to the synthetic extractor."""

    item_id: str
    revision: int
    memory_kind: MemoryKind
    statement: str
    confidence: float
    verification: VerificationStatus
    supporting_count: int
    counterevidence_count: int
    trusted_as_instructions: bool = False


@dataclass(frozen=True, slots=True)
class NoChangeProposal:
    source_event_id: str
    reason: str
    considered_evidence_ids: tuple[str, ...]


MemoryProposal = (
    InterestMemoryProposal
    | BeliefMemoryProposal
    | GoalMemoryProposal
    | GenericMemoryProposal
    | NoChangeProposal
)


@dataclass(frozen=True, slots=True)
class Experience:
    """Model-visible private experience without participant identity."""

    event_id: str
    occurred_at: datetime
    content: str
    provenance: Provenance
    trusted_as_instructions: bool = False


@dataclass(frozen=True, slots=True)
class ExtractionRequest:
    experience: Experience
    current_candidates: tuple[PrivateCandidate, ...]
    core_self_version: str
    additional_evidence: tuple[Experience, ...] = ()
    participant_id: None = None
    private_memories: tuple[GenericMemoryView, ...] = ()


class MemoryExtractor(Protocol):
    """Optional non-authoritative adapter implemented by fakes in this slice."""

    identity: ExtractorIdentity

    def extract(self, request: ExtractionRequest) -> object: ...


@dataclass(frozen=True, slots=True)
class CognitionOutcome:
    status: CognitionOutcomeStatus
    reason: CognitionReason
    memory_kind: MemoryKind | None = None
    transition: StateTransitionResult | None = None
    pending_proposal: GenericMemoryProposal | None = None
    no_change: NoChangeProposal | None = None
    generic_revision: tuple[str, int] | None = None


class InvalidationStatus(StrEnum):
    PENDING_INTEGRATION = "pending_integration"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class InvalidationPlan:
    status: InvalidationStatus
    source_event_id: str
    affected_items: tuple[tuple[str, str, int], ...]
    durable_changes_applied: bool = False


@dataclass(frozen=True, slots=True)
class AbstractCandidate:
    """Shareable-shaped value; it deliberately contains no origin metadata."""

    memory_kind: MemoryKind
    value: str
    confidence: float
    verification: VerificationStatus


@dataclass(frozen=True, slots=True)
class AbstractToSharedProposal:
    operation_id: str
    candidate: AbstractCandidate
    protected_source_event_ids: tuple[str, ...]
    proposed_at: datetime


class PrivacyScreenDecision(StrEnum):
    ALLOW = "allow"
    REJECT = "reject"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class PrivacyScreenResult:
    version: str
    decision: PrivacyScreenDecision
    reason: str


class AbstractDecisionStatus(StrEnum):
    REJECTED = "rejected"
    QUARANTINED = "quarantined"


class AbstractReason(StrEnum):
    SHARED_COMMIT_DISABLED = "shared_commit_disabled"
    PRIVACY_REJECTED = "privacy_rejected"
    PRIVACY_UNCERTAIN = "privacy_uncertain"
    CAPABILITY_DISABLED = "capability_disabled"
    INVALID_PROPOSAL = "invalid_proposal"
    INVALID_KIND = "invalid_kind"
    INVALID_VALUE = "invalid_value"
    INVALID_PROVENANCE = "invalid_provenance"
    MISSING_PROVENANCE = "missing_provenance"
    PROTECTED_METADATA = "protected_metadata"
    IDENTIFIER = "identifier"
    QUOTATION = "quotation"
    EXCESSIVE_SOURCE_OVERLAP = "excessive_source_overlap"
    VERIFICATION_MISMATCH = "verification_mismatch"


@dataclass(frozen=True, slots=True)
class AbstractAuditRecord:
    operation_id: str
    protected_source_event_ids: tuple[str, ...]
    source_participant_count: int
    screen_version: str | None
    screen_reason: str | None


@dataclass(frozen=True, slots=True)
class AbstractValidationResult:
    status: AbstractDecisionStatus
    reason: AbstractReason
    quarantined_candidate: AbstractCandidate | None = None
    shared_candidate: None = None
    audit: AbstractAuditRecord | None = None


class RetrievalPurpose(StrEnum):
    CONVERSATION = "conversation"
    DELIBERATION = "deliberation"
    REVISION = "revision"


class MemoryTrust(StrEnum):
    UNTRUSTED_MEMORY_DATA = "untrusted_memory_data"


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    purpose: RetrievalPurpose
    relevant_topic: str
    total_utf8_bytes: int
    max_records: int
    invalidated_event_ids: tuple[str, ...] = ()
    include_terminal: bool = False


@dataclass(frozen=True, slots=True)
class MemoryRetrievalLimits:
    max_utf8_bytes: int
    max_records: int

    def __post_init__(self) -> None:
        if (
            type(self.max_utf8_bytes) is not int
            or not 1 <= self.max_utf8_bytes <= 1024 * 1024
            or type(self.max_records) is not int
            or not 1 <= self.max_records <= 100
        ):
            raise ValueError("invalid retrieval limits")


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    item_id: str
    memory_kind: MemoryKind
    revision: int
    revision_reason: str
    text: str
    status: str
    scope: str
    confidence: float | None
    priority: int | None
    stance: BeliefStance | None
    supporting_evidence_count: int
    counterevidence_count: int
    reason_for_interest: str | None
    alternatives: tuple[str, ...]
    rationale: str | None
    constraints: tuple[str, ...]
    review_condition: str | None
    verification: VerificationStatus
    provenance: ProvenanceType
    core_self_version: str
    model_identifier: str | None
    prompt_version: str | None
    recency: datetime
    review_at: datetime
    trust: MemoryTrust
    can_instruct: bool
    utf8_bytes: int


@dataclass(frozen=True, slots=True)
class MemorySelection:
    purpose: RetrievalPurpose
    records: tuple[MemoryRecord, ...]
    used_utf8_bytes: int
    total_utf8_bytes: int
    records_omitted: bool
