"""Typed mutable intellectual state and deterministic revision requests."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .events import AudienceType, ProvenanceType, VerificationStatus


class StateType(StrEnum):
    INTEREST = "interest"
    BELIEF = "belief"
    GOAL = "goal"


class StateScope(StrEnum):
    PRIVATE_CANDIDATE = "private_candidate"
    SHARED_AGENT = "shared_agent"


class InterestStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ABANDONED = "abandoned"


class BeliefStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ABANDONED = "abandoned"


class BeliefStance(StrEnum):
    SUPPORTS = "supports"
    OPPOSES = "opposes"
    UNCERTAIN = "uncertain"


class GoalStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class RevisionKind(StrEnum):
    EVIDENCE_BACKED = "evidence_backed"
    TENTATIVE_INTERPRETATION = "tentative_interpretation"
    OPERATIONAL_TRANSITION = "operational_transition"


class DecisionOrigin(StrEnum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"


class EvidenceRole(StrEnum):
    INITIATING = "initiating"
    SUPPORTING = "supporting"
    COUNTER = "counter"


class TransitionStatus(StrEnum):
    CREATED = "created"
    REVISED = "revised"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class RevisionContext:
    operation_id: str
    expected_revision: int | None
    reason: str
    decided_at: datetime
    initiating_event_id: str
    kind: RevisionKind
    decision_origin: DecisionOrigin
    model_identifier: str | None = None
    prompt_version: str | None = None


@dataclass(frozen=True, slots=True)
class InterestCandidateChange:
    item_id: str
    topic: str
    strength: int
    status: InterestStatus
    reason_for_interest: str
    supporting_event_ids: tuple[str, ...]
    review_at: datetime
    revision: RevisionContext


@dataclass(frozen=True, slots=True)
class BeliefCandidateChange:
    item_id: str
    proposition: str
    stance: BeliefStance
    confidence: float
    status: BeliefStatus
    supporting_event_ids: tuple[str, ...]
    conflicting_event_ids: tuple[str, ...]
    alternatives: tuple[str, ...]
    verification: VerificationStatus
    review_at: datetime
    revision: RevisionContext


@dataclass(frozen=True, slots=True)
class GoalCandidateChange:
    item_id: str
    description: str
    rationale: str
    priority: int
    status: GoalStatus
    constraints: tuple[str, ...]
    supporting_event_ids: tuple[str, ...]
    review_condition: str
    review_at: datetime
    revision: RevisionContext


@dataclass(frozen=True, slots=True)
class InterestCandidate:
    item_id: str
    topic: str
    strength: int
    status: InterestStatus
    reason_for_interest: str
    created_at: datetime
    review_at: datetime
    revision: int
    supporting_evidence_count: int
    scope: StateScope = StateScope.PRIVATE_CANDIDATE


@dataclass(frozen=True, slots=True)
class BeliefCandidate:
    item_id: str
    proposition: str
    stance: BeliefStance
    confidence: float
    status: BeliefStatus
    alternatives: tuple[str, ...]
    verification: VerificationStatus
    created_at: datetime
    review_at: datetime
    revision: int
    supporting_evidence_count: int
    conflicting_evidence_count: int
    scope: StateScope = StateScope.PRIVATE_CANDIDATE


@dataclass(frozen=True, slots=True)
class GoalCandidate:
    item_id: str
    description: str
    rationale: str
    priority: int
    status: GoalStatus
    constraints: tuple[str, ...]
    review_condition: str
    created_at: datetime
    review_at: datetime
    revision: int
    supporting_evidence_count: int
    scope: StateScope = StateScope.PRIVATE_CANDIDATE


PrivateCandidate = InterestCandidate | BeliefCandidate | GoalCandidate


@dataclass(frozen=True, slots=True)
class StateTransitionResult:
    status: TransitionStatus
    state: PrivateCandidate


@dataclass(frozen=True, slots=True)
class StateRevision:
    item_id: str
    state_type: StateType
    revision: int
    previous_revision: int | None
    previous_status: str | None
    new_status: str
    previous_confidence: float | None
    new_confidence: float | None
    reason: str
    supporting_evidence_count: int
    counterevidence_count: int
    decided_at: datetime
    review_at: datetime
    core_self_version: str
    kind: RevisionKind
    decision_origin: DecisionOrigin
    model_identifier: str | None
    prompt_version: str | None
    state: PrivateCandidate


@dataclass(frozen=True, slots=True)
class ProtectedEvidence:
    event_id: str
    role: EvidenceRole
    audience: AudienceType
    source_principal_id: str
    provenance: ProvenanceType
    verification: VerificationStatus
