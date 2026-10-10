"""Deterministic authorization bridge from experience to private self-state."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
import re

from app.core.core_self import CORE_SELF_VERSION
from app.core.events import TrustedPrincipalContext, VerificationStatus
from app.core.self_state import (
    BeliefCandidate,
    BeliefCandidateChange,
    BeliefStance,
    BeliefStatus,
    DecisionOrigin,
    GoalCandidate,
    GoalCandidateChange,
    GoalStatus,
    InterestCandidate,
    InterestCandidateChange,
    InterestStatus,
    RevisionContext,
    RevisionKind,
)
from app.core.self_state_store import (
    MAX_EVIDENCE_PER_REVISION,
    SelfStateConflictError,
    SelfStateIdentityConflictError,
    SelfStateStore,
    SelfStateValidationError,
)
from app.core.store import EventStore, MAX_STATE_TEXT_BYTES

from .contracts import (
    BeliefMemoryProposal,
    CognitionCapabilities,
    CognitionOutcome,
    CognitionOutcomeStatus,
    CognitionReason,
    Experience,
    ExtractionRequest,
    ExtractorIdentity,
    GenericMemoryProposal,
    GoalMemoryProposal,
    InterestMemoryProposal,
    InvalidationPlan,
    InvalidationStatus,
    MemoryExtractor,
    MemoryKind,
    NoChangeProposal,
    ProposalContext,
)
from .read_store import CognitionReadStore


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_GENERIC_KINDS = {
    MemoryKind.UNRESOLVED_QUESTION,
    MemoryKind.CANDIDATE_USEFUL_MEMORY,
    MemoryKind.CONTRADICTION_COUNTEREVIDENCE,
}


class CognitionEngine:
    """Runs an optional extractor, then authorizes its proposal deterministically."""

    def __init__(
        self,
        event_store: EventStore,
        state_store: SelfStateStore,
        *,
        extractor: MemoryExtractor | None = None,
        capabilities: CognitionCapabilities = CognitionCapabilities(),
        clock: Callable[[], datetime] | None = None,
    ):
        self._events = event_store
        self._state = state_store
        self._read = CognitionReadStore(event_store, state_store)
        self._extractor = extractor
        self._capabilities = capabilities
        self._clock = clock or (lambda: datetime.now(UTC))

    def process(
        self,
        context: TrustedPrincipalContext,
        source_event_id: str,
        *,
        evidence_event_ids: tuple[str, ...] = (),
    ) -> CognitionOutcome:
        """Explicitly extract from one authorized experience and validate the result."""

        if not self._capabilities.memory_extraction:
            return self._rejected(CognitionReason.CAPABILITY_DISABLED)
        if self._extractor is None:
            return self._rejected(CognitionReason.EXTRACTOR_NOT_CONFIGURED)
        if not self._valid_token(source_event_id):
            return self._rejected(CognitionReason.SOURCE_NOT_FOUND)
        source = self._read.retrieve_exact_private_event(context, source_event_id)
        if source is None:
            return self._rejected(CognitionReason.SOURCE_NOT_FOUND)
        if (
            type(evidence_event_ids) is not tuple
            or len(evidence_event_ids) >= MAX_EVIDENCE_PER_REVISION
            or source_event_id in evidence_event_ids
            or any(not self._valid_token(item) for item in evidence_event_ids)
            or len(set(evidence_event_ids)) != len(evidence_event_ids)
        ):
            return self._rejected(CognitionReason.EVIDENCE_NOT_FOUND)
        resolved_evidence = tuple(
            self._read.retrieve_exact_private_event(context, item)
            for item in evidence_event_ids
        )
        if any(item is None for item in resolved_evidence):
            return self._rejected(CognitionReason.EVIDENCE_NOT_FOUND)
        try:
            identity = self._extractor.identity
        except Exception:
            return self._rejected(CognitionReason.EXTRACTOR_FAILED)
        if not self._valid_identity(identity):
            return self._rejected(CognitionReason.INVALID_PROPOSAL)
        current = self._state.retrieve_private_candidates(context)
        request = ExtractionRequest(
            experience=self._experience(source),
            current_candidates=current,
            core_self_version=CORE_SELF_VERSION,
            additional_evidence=tuple(
                self._experience(item) for item in resolved_evidence
            ),
        )
        try:
            proposal = self._extractor.extract(request)
        except Exception:
            return self._rejected(CognitionReason.EXTRACTOR_FAILED)
        if self._state.retrieve_private_candidates(context) != current:
            return self._rejected(CognitionReason.STALE_REVISION)
        decided_at = self._canonical_time(self._clock())
        if decided_at is None:
            raise ValueError("clock returned an invalid decision time")
        return self._authorize(
            context,
            source_event_id,
            proposal,
            identity,
            decided_at,
            frozenset((source_event_id, *evidence_event_ids)),
            frozenset(candidate.item_id for candidate in current),
            tuple((candidate.item_id, candidate.revision) for candidate in current),
        )

    def plan_source_invalidation(
        self, context: TrustedPrincipalContext, source_event_id: str
    ) -> InvalidationPlan:
        """Identify affected revisions without pretending deletion integration exists."""

        if not self._valid_token(source_event_id):
            return InvalidationPlan(InvalidationStatus.REJECTED, "", ())
        if not self._read.has_private_event_envelope(context, source_event_id):
            return InvalidationPlan(InvalidationStatus.REJECTED, source_event_id, ())
        affected = self._read.retrieve_private_revision_references(
            context, source_event_id
        )
        return InvalidationPlan(
            InvalidationStatus.PENDING_INTEGRATION,
            source_event_id,
            affected,
        )

    def _authorize(
        self,
        context: TrustedPrincipalContext,
        source_event_id: str,
        proposal: object,
        identity: ExtractorIdentity,
        decided_at: datetime,
        available_evidence_ids: frozenset[str],
        visible_candidate_ids: frozenset[str],
        expected_candidate_revisions: tuple[tuple[str, int], ...],
    ) -> CognitionOutcome:
        if type(proposal) is NoChangeProposal:
            return self._authorize_no_change(
                proposal, source_event_id, available_evidence_ids
            )
        if type(proposal) not in {
            InterestMemoryProposal,
            BeliefMemoryProposal,
            GoalMemoryProposal,
            GenericMemoryProposal,
        }:
            return self._rejected(CognitionReason.INVALID_PROPOSAL)
        expected_kind = {
            InterestMemoryProposal: MemoryKind.DURABLE_INTEREST,
            BeliefMemoryProposal: MemoryKind.BELIEF_OR_HYPOTHESIS,
            GoalMemoryProposal: MemoryKind.GOAL_OR_INTENTION,
        }.get(type(proposal))
        if expected_kind is not None and proposal.memory_kind is not expected_kind:
            return self._rejected(CognitionReason.INVALID_MEMORY_KIND)
        if isinstance(proposal, GenericMemoryProposal):
            if (
                not isinstance(proposal.memory_kind, MemoryKind)
                or proposal.memory_kind not in _GENERIC_KINDS
            ):
                return self._rejected(CognitionReason.INVALID_MEMORY_KIND)
        reason = self._validate_common(
            proposal.context,
            source_event_id,
            available_evidence_ids,
            allow_counterevidence_only=isinstance(proposal, BeliefMemoryProposal),
        )
        if reason is not None:
            return self._rejected(reason, self._safe_kind(proposal))
        proposed_at = self._canonical_time(proposal.context.proposed_at)
        review_at = self._canonical_time(proposal.context.review_at)
        if proposed_at is None or review_at is None:
            return self._rejected(
                CognitionReason.INVALID_PROPOSAL, self._safe_kind(proposal)
            )
        proposal = replace(
            proposal,
            context=replace(
                proposal.context,
                proposed_at=proposed_at,
                review_at=review_at,
            ),
        )
        if isinstance(proposal, GenericMemoryProposal):
            return self._authorize_generic(proposal)
        if not self._valid_specific_types(proposal):
            return self._rejected(
                CognitionReason.INVALID_PROPOSAL, proposal.memory_kind
            )
        if (
            not isinstance(proposal, BeliefMemoryProposal)
            and proposal.context.counterevidence_event_ids
        ):
            return self._rejected(
                CognitionReason.INVALID_PROPOSAL, proposal.memory_kind
            )
        if not self._capabilities.private_state_write:
            return self._rejected(
                CognitionReason.CAPABILITY_DISABLED, proposal.memory_kind
            )
        if isinstance(proposal, BeliefMemoryProposal):
            if proposal.verification is not VerificationStatus.UNVERIFIED:
                return self._rejected(
                    CognitionReason.VERIFICATION_MISMATCH, proposal.memory_kind
                )
            if not self._consistent_confidence(proposal):
                return self._rejected(
                    CognitionReason.INCONSISTENT_CONFIDENCE, proposal.memory_kind
                )
        change = self._build_change(proposal, identity, decided_at)
        try:
            duplicate = self._read.replay_private_operation(context, change)
        except SelfStateIdentityConflictError:
            return self._rejected(
                CognitionReason.OPERATION_ID_CONFLICT, proposal.memory_kind
            )
        except SelfStateValidationError:
            return self._rejected(CognitionReason.STORE_REJECTED, proposal.memory_kind)
        if duplicate is not None:
            return CognitionOutcome(
                CognitionOutcomeStatus.PERSISTED,
                CognitionReason.AUTHORIZED,
                proposal.memory_kind,
                transition=duplicate,
            )
        if (
            proposal.context.expected_revision is not None
            and proposal.item_id not in visible_candidate_ids
        ):
            return self._rejected(CognitionReason.STALE_REVISION, proposal.memory_kind)
        existing = self._read.retrieve_exact_private_candidate(
            context, proposal.item_id
        )
        reason = self._validate_revision_target(proposal, existing)
        if reason is not None:
            try:
                duplicate = self._read.replay_private_operation(context, change)
            except SelfStateIdentityConflictError:
                return self._rejected(
                    CognitionReason.OPERATION_ID_CONFLICT, proposal.memory_kind
                )
            except SelfStateValidationError:
                return self._rejected(
                    CognitionReason.STORE_REJECTED, proposal.memory_kind
                )
            if duplicate is not None:
                return CognitionOutcome(
                    CognitionOutcomeStatus.PERSISTED,
                    CognitionReason.AUTHORIZED,
                    proposal.memory_kind,
                    transition=duplicate,
                )
            return self._rejected(reason, proposal.memory_kind)
        try:
            transition = self._persist(
                context,
                proposal,
                identity,
                decided_at,
                expected_candidate_revisions,
            )
        except SelfStateConflictError:
            return self._rejected(CognitionReason.STALE_REVISION, proposal.memory_kind)
        except SelfStateIdentityConflictError:
            try:
                duplicate = self._read.replay_private_operation(context, change)
            except SelfStateIdentityConflictError:
                duplicate = None
            except SelfStateValidationError:
                return self._rejected(
                    CognitionReason.STORE_REJECTED, proposal.memory_kind
                )
            if duplicate is None:
                return self._rejected(
                    CognitionReason.OPERATION_ID_CONFLICT, proposal.memory_kind
                )
            transition = duplicate
        except SelfStateValidationError:
            return self._rejected(CognitionReason.STORE_REJECTED, proposal.memory_kind)
        return CognitionOutcome(
            CognitionOutcomeStatus.PERSISTED,
            CognitionReason.AUTHORIZED,
            proposal.memory_kind,
            transition=transition,
        )

    def _authorize_no_change(
        self,
        proposal: NoChangeProposal,
        source_event_id: str,
        available_evidence_ids: frozenset[str],
    ) -> CognitionOutcome:
        if not CognitionEngine._valid_token(proposal.source_event_id):
            return self._rejected(CognitionReason.INVALID_PROPOSAL)
        if proposal.source_event_id != source_event_id:
            return self._rejected(CognitionReason.SOURCE_MISMATCH)
        if (
            not CognitionEngine._valid_text(proposal.reason)
            or type(proposal.considered_evidence_ids) is not tuple
            or not proposal.considered_evidence_ids
            or len(proposal.considered_evidence_ids) > MAX_EVIDENCE_PER_REVISION
        ):
            return self._rejected(CognitionReason.INVALID_PROPOSAL)
        if any(
            not CognitionEngine._valid_token(item)
            for item in proposal.considered_evidence_ids
        ) or len(set(proposal.considered_evidence_ids)) != len(
            proposal.considered_evidence_ids
        ):
            return self._rejected(CognitionReason.INVALID_PROPOSAL)
        if not set(proposal.considered_evidence_ids) <= available_evidence_ids:
            return self._rejected(CognitionReason.EVIDENCE_NOT_FOUND)
        return CognitionOutcome(
            CognitionOutcomeStatus.NO_CHANGE,
            CognitionReason.INSUFFICIENT_EVIDENCE,
            no_change=proposal,
        )

    @staticmethod
    def _validate_common(
        proposal: ProposalContext,
        source_event_id: str,
        available_evidence_ids: frozenset[str],
        *,
        allow_counterevidence_only: bool,
    ) -> CognitionReason | None:
        if type(proposal) is not ProposalContext:
            return CognitionReason.INVALID_PROPOSAL
        if not CognitionEngine._valid_token(proposal.source_event_id):
            return CognitionReason.INVALID_PROPOSAL
        if proposal.source_event_id != source_event_id:
            return CognitionReason.SOURCE_MISMATCH
        if (
            not CognitionEngine._valid_token(proposal.operation_id)
            or not CognitionEngine._valid_text(proposal.reason)
            or not CognitionEngine._valid_time(proposal.proposed_at)
            or not CognitionEngine._valid_time(proposal.review_at)
            or type(proposal.supporting_event_ids) is not tuple
            or type(proposal.counterevidence_event_ids) is not tuple
        ):
            return CognitionReason.INVALID_PROPOSAL
        all_ids = (*proposal.supporting_event_ids, *proposal.counterevidence_event_ids)
        if any(not CognitionEngine._valid_token(item) for item in all_ids):
            return CognitionReason.INVALID_PROPOSAL
        counterevidence_only = (
            allow_counterevidence_only
            and proposal.expected_revision is not None
            and bool(proposal.counterevidence_event_ids)
        )
        if (
            (not proposal.supporting_event_ids and not counterevidence_only)
            or proposal.source_event_id not in all_ids
            or len(all_ids) + 1 > MAX_EVIDENCE_PER_REVISION
        ):
            return CognitionReason.INVALID_PROPOSAL
        if len(set(all_ids)) != len(all_ids):
            return CognitionReason.EVIDENCE_ROLE_CONFLICT
        if not set(all_ids) <= available_evidence_ids:
            return CognitionReason.EVIDENCE_NOT_FOUND
        return None

    @staticmethod
    def _experience(event) -> Experience:
        return Experience(
            event_id=event.event_id,
            occurred_at=event.occurred_at,
            content=event.content,
            provenance=event.provenance,
        )

    @staticmethod
    def _authorize_generic(proposal: GenericMemoryProposal) -> CognitionOutcome:
        if (
            not CognitionEngine._valid_text(proposal.statement)
            or type(proposal.confidence) is not float
            or not 0.0 <= proposal.confidence <= 1.0
        ):
            return CognitionEngine._rejected(
                CognitionReason.INVALID_PROPOSAL, proposal.memory_kind
            )
        if proposal.verification is not VerificationStatus.UNVERIFIED:
            return CognitionEngine._rejected(
                CognitionReason.VERIFICATION_MISMATCH, proposal.memory_kind
            )
        return CognitionOutcome(
            CognitionOutcomeStatus.PENDING_NOT_PERSISTED,
            CognitionReason.PERSISTENCE_NOT_AVAILABLE,
            proposal.memory_kind,
            pending_proposal=proposal,
        )

    @staticmethod
    def _validate_revision_target(
        proposal: InterestMemoryProposal | BeliefMemoryProposal | GoalMemoryProposal,
        existing: InterestCandidate | BeliefCandidate | GoalCandidate | None,
    ) -> CognitionReason | None:
        expected = proposal.context.expected_revision
        if expected is None:
            if proposal.status in {
                InterestStatus.ABANDONED,
                BeliefStatus.ABANDONED,
                GoalStatus.COMPLETED,
                GoalStatus.ABANDONED,
            }:
                return CognitionReason.INVALID_STATUS_TRANSITION
            return None
        if (
            type(expected) is not int
            or existing is None
            or expected != existing.revision
        ):
            return CognitionReason.STALE_REVISION
        if not CognitionEngine._same_state_type(proposal, existing):
            return CognitionReason.STALE_REVISION
        if existing.status in {
            InterestStatus.ABANDONED,
            BeliefStatus.ABANDONED,
            GoalStatus.COMPLETED,
            GoalStatus.ABANDONED,
        }:
            return CognitionReason.INVALID_STATUS_TRANSITION
        return None

    def _persist(
        self,
        context: TrustedPrincipalContext,
        proposal: InterestMemoryProposal | BeliefMemoryProposal | GoalMemoryProposal,
        identity: ExtractorIdentity,
        decided_at: datetime,
        expected_candidate_revisions: tuple[tuple[str, int], ...],
    ):
        change = self._build_change(proposal, identity, decided_at)
        creating = proposal.context.expected_revision is None
        if isinstance(proposal, InterestMemoryProposal):
            method = (
                self._state.create_interest_candidate
                if creating
                else self._state.revise_interest_candidate
            )
        elif isinstance(proposal, BeliefMemoryProposal):
            method = (
                self._state.create_belief_candidate
                if creating
                else self._state.revise_belief_candidate
            )
        else:
            method = (
                self._state.create_goal_candidate
                if creating
                else self._state.revise_goal_candidate
            )
        return method(
            context,
            change,
            _expected_candidate_revisions=expected_candidate_revisions,
        )

    @staticmethod
    def _build_change(
        proposal: InterestMemoryProposal | BeliefMemoryProposal | GoalMemoryProposal,
        identity: ExtractorIdentity,
        decided_at: datetime,
    ) -> InterestCandidateChange | BeliefCandidateChange | GoalCandidateChange:
        revision = RevisionContext(
            operation_id=proposal.context.operation_id,
            expected_revision=proposal.context.expected_revision,
            reason=proposal.context.reason,
            decided_at=decided_at,
            initiating_event_id=proposal.context.source_event_id,
            kind=(
                RevisionKind.OPERATIONAL_TRANSITION
                if isinstance(proposal, GoalMemoryProposal)
                else RevisionKind.TENTATIVE_INTERPRETATION
            ),
            decision_origin=DecisionOrigin.MODEL,
            model_identifier=identity.model_identifier,
            prompt_version=identity.prompt_version,
        )
        if isinstance(proposal, InterestMemoryProposal):
            return InterestCandidateChange(
                proposal.item_id,
                proposal.topic,
                proposal.strength,
                proposal.status,
                proposal.reason_for_interest,
                proposal.context.supporting_event_ids,
                proposal.context.review_at,
                revision,
            )
        if isinstance(proposal, BeliefMemoryProposal):
            return BeliefCandidateChange(
                proposal.item_id,
                proposal.proposition,
                proposal.stance,
                proposal.confidence,
                proposal.status,
                proposal.context.supporting_event_ids,
                proposal.context.counterevidence_event_ids,
                proposal.alternatives,
                proposal.verification,
                proposal.context.review_at,
                revision,
            )
        return GoalCandidateChange(
            proposal.item_id,
            proposal.description,
            proposal.rationale,
            proposal.priority,
            proposal.status,
            proposal.constraints,
            proposal.context.supporting_event_ids,
            proposal.review_condition,
            proposal.context.review_at,
            revision,
        )

    @staticmethod
    def _consistent_confidence(proposal: BeliefMemoryProposal) -> bool:
        if type(proposal.confidence) is not float:
            return False
        if proposal.stance is BeliefStance.UNCERTAIN:
            return 0.0 <= proposal.confidence <= 0.5
        return 0.5 <= proposal.confidence <= 1.0

    @staticmethod
    def _valid_specific_types(proposal: object) -> bool:
        if not CognitionEngine._valid_token(getattr(proposal, "item_id", None)):
            return False
        if isinstance(proposal, InterestMemoryProposal):
            return (
                CognitionEngine._valid_text(proposal.topic)
                and CognitionEngine._valid_text(proposal.reason_for_interest)
                and type(proposal.strength) is int
                and isinstance(proposal.status, InterestStatus)
            )
        if isinstance(proposal, BeliefMemoryProposal):
            return (
                CognitionEngine._valid_text(proposal.proposition)
                and CognitionEngine._valid_text_tuple(proposal.alternatives)
                and isinstance(proposal.stance, BeliefStance)
                and isinstance(proposal.status, BeliefStatus)
            )
        return (
            isinstance(proposal, GoalMemoryProposal)
            and CognitionEngine._valid_text(proposal.description)
            and CognitionEngine._valid_text(proposal.rationale)
            and CognitionEngine._valid_text(proposal.review_condition)
            and CognitionEngine._valid_text_tuple(proposal.constraints)
            and type(proposal.priority) is int
            and isinstance(proposal.status, GoalStatus)
        )

    @staticmethod
    def _same_state_type(proposal: object, existing: object) -> bool:
        return (
            (
                isinstance(proposal, InterestMemoryProposal)
                and isinstance(existing, InterestCandidate)
            )
            or (
                isinstance(proposal, BeliefMemoryProposal)
                and isinstance(existing, BeliefCandidate)
            )
            or (
                isinstance(proposal, GoalMemoryProposal)
                and isinstance(existing, GoalCandidate)
            )
        )

    @staticmethod
    def _safe_kind(proposal: object) -> MemoryKind | None:
        kind = getattr(proposal, "memory_kind", None)
        return kind if isinstance(kind, MemoryKind) else None

    @staticmethod
    def _valid_identity(identity: object) -> bool:
        return (
            isinstance(identity, ExtractorIdentity)
            and CognitionEngine._valid_token(identity.model_identifier)
            and CognitionEngine._valid_token(identity.prompt_version)
        )

    @staticmethod
    def _valid_token(value: object) -> bool:
        return type(value) is str and _TOKEN.fullmatch(value) is not None

    @staticmethod
    def _valid_text(value: object) -> bool:
        if type(value) is not str or not value.strip():
            return False
        try:
            return len(value.encode("utf-8")) <= MAX_STATE_TEXT_BYTES
        except UnicodeEncodeError:
            return False

    @staticmethod
    def _valid_text_tuple(values: object) -> bool:
        return type(values) is tuple and all(
            CognitionEngine._valid_text(value) for value in values
        )

    @staticmethod
    def _valid_time(value: object) -> bool:
        return CognitionEngine._canonical_time(value) is not None

    @staticmethod
    def _canonical_time(value: object) -> datetime | None:
        if not isinstance(value, datetime) or value.tzinfo is None:
            return None
        try:
            plain = datetime(
                value.year,
                value.month,
                value.day,
                value.hour,
                value.minute,
                value.second,
                value.microsecond,
                tzinfo=value.tzinfo,
                fold=value.fold,
            )
            return plain.astimezone(UTC)
        except Exception:
            return None

    @staticmethod
    def _rejected(
        reason: CognitionReason, memory_kind: MemoryKind | None = None
    ) -> CognitionOutcome:
        return CognitionOutcome(CognitionOutcomeStatus.REJECTED, reason, memory_kind)
