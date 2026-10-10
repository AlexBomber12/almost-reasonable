"""Privacy-first deterministic ranking of whole, untrusted memory records."""

from datetime import datetime
import re
import unicodedata

from app.core.events import ProvenanceType, TrustedPrincipalContext, VerificationStatus
from app.core.self_state import (
    BeliefCandidate,
    InterestCandidate,
    PrivateCandidate,
    StateType,
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


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_WORD = re.compile(r"\w+", re.UNICODE)
_TERMINAL_STATUSES = {"completed", "abandoned"}


class MemoryContextSelector:
    """Selects authorized private state before any relevance ranking."""

    def __init__(self, event_store: EventStore, state_store: SelfStateStore):
        self._state = state_store
        self._read = CognitionReadStore(event_store, state_store)

    def select(
        self, context: TrustedPrincipalContext, request: RetrievalRequest
    ) -> MemorySelection:
        self._validate_request(request)
        limits = MemoryRetrievalLimits(request.total_utf8_bytes, request.max_records)

        # This store call is the privacy boundary. Only its authorized result is
        # transformed or ranked below.
        authorized = self._read.retrieve_all_private_candidates(context)
        invalidated = set(request.invalidated_event_ids)
        ranked: list[tuple[tuple[object, ...], MemoryRecord]] = []
        query_words = set(self._words(request.relevant_topic))
        omitted = False
        for candidate in authorized:
            if (
                not request.include_terminal
                and candidate.status.value in _TERMINAL_STATUSES
            ):
                omitted = True
                continue
            state_type = self._state_type(candidate)
            evidence_ids = self._read.retrieve_private_candidate_evidence_ids(
                context, candidate.item_id
            )
            if invalidated & set(evidence_ids):
                omitted = True
                continue
            history = self._state.retrieve_private_revision_history(
                context, state_type, candidate.item_id
            )
            recency = history[-1].decided_at
            record = self._record(candidate, recency)
            relevance = len(query_words & set(self._words(record.text)))
            strength = (
                record.confidence
                if record.confidence is not None
                else (record.priority or 0) / 100
            )
            rank = (
                relevance,
                record.status == "active",
                strength,
                record.recency,
                record.item_id,
            )
            ranked.append((rank, record))

        ranked.sort(key=lambda item: item[0], reverse=True)
        selected: list[MemoryRecord] = []
        used = 0
        for _, record in ranked:
            if len(selected) == limits.max_records:
                omitted = True
                break
            if used + record.utf8_bytes > limits.max_utf8_bytes:
                omitted = True
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
    def _record(candidate: PrivateCandidate, recency: datetime) -> MemoryRecord:
        if isinstance(candidate, InterestCandidate):
            kind = MemoryKind.DURABLE_INTEREST
            text = candidate.topic
            confidence = None
            priority = candidate.strength
            verification = VerificationStatus.UNVERIFIED
        elif isinstance(candidate, BeliefCandidate):
            kind = MemoryKind.BELIEF_OR_HYPOTHESIS
            text = candidate.proposition
            confidence = candidate.confidence
            priority = None
            verification = candidate.verification
        else:
            kind = MemoryKind.GOAL_OR_INTENTION
            text = candidate.description
            confidence = None
            priority = candidate.priority
            verification = VerificationStatus.UNVERIFIED
        values = (
            candidate.item_id,
            kind.value,
            text,
            candidate.status.value,
            candidate.scope.value,
            "" if confidence is None else str(confidence),
            "" if priority is None else str(priority),
            verification.value,
            ProvenanceType.PARTICIPANT_ASSERTION.value,
            recency.isoformat(),
            MemoryTrust.UNTRUSTED_MEMORY_DATA.value,
            "false",
        )
        size = sum(len(value.encode("utf-8")) for value in values)
        return MemoryRecord(
            item_id=candidate.item_id,
            memory_kind=kind,
            text=text,
            status=candidate.status.value,
            scope=candidate.scope.value,
            confidence=confidence,
            priority=priority,
            verification=verification,
            provenance=ProvenanceType.PARTICIPANT_ASSERTION,
            recency=recency,
            trust=MemoryTrust.UNTRUSTED_MEMORY_DATA,
            can_instruct=False,
            utf8_bytes=size,
        )

    @staticmethod
    def _state_type(candidate: PrivateCandidate) -> StateType:
        if isinstance(candidate, InterestCandidate):
            return StateType.INTEREST
        if isinstance(candidate, BeliefCandidate):
            return StateType.BELIEF
        return StateType.GOAL

    @staticmethod
    def _words(value: str) -> tuple[str, ...]:
        return tuple(_WORD.findall(unicodedata.normalize("NFKC", value).casefold()))

    @staticmethod
    def _validate_request(request: object) -> None:
        if not isinstance(request, RetrievalRequest):
            raise ValueError("invalid retrieval request")
        try:
            MemoryRetrievalLimits(request.total_utf8_bytes, request.max_records)
            request.relevant_topic.encode("utf-8")
        except (AttributeError, UnicodeEncodeError, ValueError):
            raise ValueError("invalid retrieval request") from None
        if (
            not isinstance(request.purpose, RetrievalPurpose)
            or type(request.relevant_topic) is not str
            or len(request.relevant_topic.encode("utf-8")) > 4096
            or type(request.include_terminal) is not bool
            or (
                request.include_terminal
                and request.purpose is not RetrievalPurpose.REVISION
            )
            or type(request.invalidated_event_ids) is not tuple
            or len(request.invalidated_event_ids) > 100
            or len(set(request.invalidated_event_ids))
            != len(request.invalidated_event_ids)
            or any(
                not isinstance(item, str) or _TOKEN.fullmatch(item) is None
                for item in request.invalidated_event_ids
            )
        ):
            raise ValueError("invalid retrieval request")
