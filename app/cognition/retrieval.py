"""Privacy-first deterministic ranking of whole, untrusted memory records."""

from datetime import datetime
import re
import sqlite3
import unicodedata

from app.core.events import ProvenanceType, TrustedPrincipalContext, VerificationStatus
from app.core.self_state import (
    BeliefCandidate,
    DecisionOrigin,
    InterestCandidate,
    PrivateCandidate,
)
from app.core.self_state_store import SelfStateStore
from app.core.store import EventStore

from .contracts import (
    MemoryKind,
    MemoryRecord,
    MemoryRetrievalLimits,
    MemorySelection,
    MemoryTrust,
    RetrievalPurpose,
    RetrievalRequest,
)
from .read_store import CognitionReadStore
from .generic_store import GenericMemoryRead, GenericMemoryStore


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_WORD = re.compile(r"\w+", re.UNICODE)
_TERMINAL_STATUSES = {"completed", "abandoned"}
_MAX_INVALIDATION_HINT_BYTES = 64 * 1024


class MemoryContextSelector:
    """Selects authorized private state before any relevance ranking."""

    def __init__(self, event_store: EventStore, state_store: SelfStateStore):
        self._read = CognitionReadStore(event_store, state_store)
        self._generic = GenericMemoryStore(event_store)

    def select(
        self,
        context: TrustedPrincipalContext,
        request: RetrievalRequest,
        *,
        _connection: sqlite3.Connection | None = None,
    ) -> MemorySelection:
        self._validate_request(request)
        limits = MemoryRetrievalLimits(request.total_utf8_bytes, request.max_records)

        # This store call is the privacy boundary. Only its authorized result is
        # transformed or ranked below.
        authorized = self._read.retrieve_all_private_candidate_reads(
            context,
            include_belief_history=request.purpose is RetrievalPurpose.REVISION,
            _connection=_connection,
        )
        invalidated = set(request.invalidated_event_ids)
        ranked: list[tuple[tuple[object, ...], MemoryRecord]] = []
        query_words = set(self._words(request.relevant_topic))
        latest_by_item: dict[str, PrivateCandidate] = {}
        for candidate_read in authorized:
            candidate = candidate_read.candidate
            latest_by_item[candidate.item_id] = max(
                candidate,
                latest_by_item.get(candidate.item_id, candidate),
                key=lambda item: item.revision,
            )
        terminal_items = {
            item_id
            for item_id, candidate in latest_by_item.items()
            if candidate.status.value in _TERMINAL_STATUSES
        }
        omitted = False
        for candidate_read in authorized:
            candidate = candidate_read.candidate
            if not request.include_terminal and candidate.item_id in terminal_items:
                omitted = True
                continue
            if candidate_read.has_invalidated_evidence or invalidated & set(
                candidate_read.evidence_event_ids
            ):
                omitted = True
                continue
            record = self._record(
                candidate,
                candidate_read.decided_at,
                candidate_read.decision_origin,
                candidate_read.revision_reason,
                candidate_read.core_self_version,
                candidate_read.model_identifier,
                candidate_read.prompt_version,
            )
            searchable_values = (
                record.text,
                record.revision_reason,
                record.reason_for_interest or "",
                *record.alternatives,
                record.rationale or "",
                *record.constraints,
                record.review_condition or "",
            )
            searchable_words = {
                word for value in searchable_values for word in self._words(value)
            }
            relevance = len(query_words & searchable_words)
            strength = (
                record.confidence
                if record.confidence is not None
                else (record.priority or 0) / 100
            )
            current_revision = (
                request.purpose is not RetrievalPurpose.REVISION
                or candidate.revision == latest_by_item[candidate.item_id].revision
            )
            rank = (
                current_revision,
                relevance,
                record.status == "active",
                strength,
                record.recency,
                record.item_id,
                record.revision,
            )
            ranked.append((rank, record))

        for generic in self._generic.current(context, _connection=_connection):
            if invalidated & set(generic.source_event_ids):
                omitted = True
                continue
            record = self._generic_record(generic)
            relevance = len(
                query_words
                & set(self._words(record.text + " " + record.revision_reason))
            )
            ranked.append(
                (
                    (
                        True,
                        relevance,
                        True,
                        record.confidence,
                        record.recency,
                        record.item_id,
                        record.revision,
                    ),
                    record,
                )
            )

        ranked.sort(key=lambda item: item[0], reverse=True)
        selected: list[MemoryRecord] = []
        omitted_revision_heads: set[str] = set()
        used = 0
        for _, record in ranked:
            if len(selected) == limits.max_records:
                omitted = True
                break
            if (
                record.memory_kind is MemoryKind.BELIEF_OR_HYPOTHESIS
                and record.item_id in omitted_revision_heads
            ):
                omitted = True
                continue
            if used + record.utf8_bytes > limits.max_utf8_bytes:
                omitted = True
                if (
                    request.purpose is RetrievalPurpose.REVISION
                    and record.memory_kind is MemoryKind.BELIEF_OR_HYPOTHESIS
                    and record.revision == latest_by_item[record.item_id].revision
                ):
                    omitted_revision_heads.add(record.item_id)
                continue
            selected.append(record)
            used += record.utf8_bytes
        return MemorySelection(
            request.purpose,
            tuple(selected),
            used,
            limits.max_utf8_bytes,
            omitted,
        )

    @staticmethod
    def _generic_record(read: GenericMemoryRead) -> MemoryRecord:
        view = read.view
        values = (
            view.item_id,
            view.memory_kind.value,
            str(view.revision),
            read.reason,
            view.statement,
            "active",
            "private_candidate",
            str(view.confidence),
            str(view.supporting_count),
            str(view.counterevidence_count),
            read.core_self_version,
            read.model_identifier,
            read.prompt_version,
            read.decided_at.isoformat(),
            read.review_at.isoformat(),
            MemoryTrust.UNTRUSTED_MEMORY_DATA.value,
        )
        return MemoryRecord(
            item_id=view.item_id,
            memory_kind=view.memory_kind,
            revision=view.revision,
            revision_reason=read.reason,
            text=view.statement,
            status="active",
            scope="private_candidate",
            confidence=view.confidence,
            priority=None,
            stance=None,
            supporting_evidence_count=view.supporting_count,
            counterevidence_count=view.counterevidence_count,
            reason_for_interest=None,
            alternatives=(),
            rationale=None,
            constraints=(),
            review_condition=None,
            verification=view.verification,
            provenance=ProvenanceType.MODEL_INFERENCE,
            core_self_version=read.core_self_version,
            model_identifier=read.model_identifier,
            prompt_version=read.prompt_version,
            recency=read.decided_at,
            review_at=read.review_at,
            trust=MemoryTrust.UNTRUSTED_MEMORY_DATA,
            can_instruct=False,
            utf8_bytes=sum(len(value.encode("utf-8")) for value in values),
        )

    @staticmethod
    def _record(
        candidate: PrivateCandidate,
        recency: datetime,
        decision_origin: DecisionOrigin,
        revision_reason: str,
        core_self_version: str,
        model_identifier: str | None,
        prompt_version: str | None,
    ) -> MemoryRecord:
        if isinstance(candidate, InterestCandidate):
            kind = MemoryKind.DURABLE_INTEREST
            text = candidate.topic
            confidence = None
            priority = candidate.strength
            stance = None
            counterevidence_count = 0
            reason_for_interest = candidate.reason_for_interest
            alternatives = ()
            rationale = None
            constraints = ()
            review_condition = None
            verification = VerificationStatus.UNVERIFIED
        elif isinstance(candidate, BeliefCandidate):
            kind = MemoryKind.BELIEF_OR_HYPOTHESIS
            text = candidate.proposition
            confidence = candidate.confidence
            priority = None
            stance = candidate.stance
            counterevidence_count = candidate.conflicting_evidence_count
            reason_for_interest = None
            alternatives = candidate.alternatives
            rationale = None
            constraints = ()
            review_condition = None
            verification = candidate.verification
        else:
            kind = MemoryKind.GOAL_OR_INTENTION
            text = candidate.description
            confidence = None
            priority = candidate.priority
            stance = None
            counterevidence_count = 0
            reason_for_interest = None
            alternatives = ()
            rationale = candidate.rationale
            constraints = candidate.constraints
            review_condition = candidate.review_condition
            verification = VerificationStatus.UNVERIFIED
        provenance = {
            DecisionOrigin.MODEL: ProvenanceType.MODEL_INFERENCE,
            DecisionOrigin.DETERMINISTIC: ProvenanceType.PARTICIPANT_ASSERTION,
        }[decision_origin]
        values = (
            candidate.item_id,
            kind.value,
            str(candidate.revision),
            revision_reason,
            text,
            candidate.status.value,
            candidate.scope.value,
            "" if confidence is None else str(confidence),
            "" if priority is None else str(priority),
            "" if stance is None else stance.value,
            str(candidate.supporting_evidence_count),
            str(counterevidence_count),
            "" if reason_for_interest is None else reason_for_interest,
            *alternatives,
            "" if rationale is None else rationale,
            *constraints,
            "" if review_condition is None else review_condition,
            verification.value,
            provenance.value,
            core_self_version,
            "" if model_identifier is None else model_identifier,
            "" if prompt_version is None else prompt_version,
            recency.isoformat(),
            candidate.review_at.isoformat(),
            MemoryTrust.UNTRUSTED_MEMORY_DATA.value,
            "false",
        )
        size = sum(len(value.encode("utf-8")) for value in values)
        return MemoryRecord(
            item_id=candidate.item_id,
            memory_kind=kind,
            revision=candidate.revision,
            revision_reason=revision_reason,
            text=text,
            status=candidate.status.value,
            scope=candidate.scope.value,
            confidence=confidence,
            priority=priority,
            stance=stance,
            supporting_evidence_count=candidate.supporting_evidence_count,
            counterevidence_count=counterevidence_count,
            reason_for_interest=reason_for_interest,
            alternatives=alternatives,
            rationale=rationale,
            constraints=constraints,
            review_condition=review_condition,
            verification=verification,
            provenance=provenance,
            core_self_version=core_self_version,
            model_identifier=model_identifier,
            prompt_version=prompt_version,
            recency=recency,
            review_at=candidate.review_at,
            trust=MemoryTrust.UNTRUSTED_MEMORY_DATA,
            can_instruct=False,
            utf8_bytes=size,
        )

    @staticmethod
    def _words(value: str) -> tuple[str, ...]:
        return tuple(_WORD.findall(unicodedata.normalize("NFKC", value).casefold()))

    @staticmethod
    def _validate_request(request: object) -> None:
        if type(request) is not RetrievalRequest:
            raise ValueError("invalid retrieval request")
        if type(request.relevant_topic) is not str:
            raise ValueError("invalid retrieval request")
        try:
            MemoryRetrievalLimits(request.total_utf8_bytes, request.max_records)
            request.relevant_topic.encode("utf-8")
        except (AttributeError, UnicodeEncodeError, ValueError):
            raise ValueError("invalid retrieval request") from None
        if (
            not isinstance(request.purpose, RetrievalPurpose)
            or len(request.relevant_topic.encode("utf-8")) > 4096
            or type(request.include_terminal) is not bool
            or (
                request.include_terminal
                and request.purpose is not RetrievalPurpose.REVISION
            )
            or type(request.invalidated_event_ids) is not tuple
            or any(
                type(item) is not str or _TOKEN.fullmatch(item) is None
                for item in request.invalidated_event_ids
            )
            or sum(len(item.encode("utf-8")) for item in request.invalidated_event_ids)
            > _MAX_INVALIDATION_HINT_BYTES
            or len(set(request.invalidated_event_ids))
            != len(request.invalidated_event_ids)
        ):
            raise ValueError("invalid retrieval request")
