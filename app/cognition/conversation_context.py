"""Bounded private memory and transcript context with protected dependencies.

Legacy v1 and v2 contracts remain strict. The v4 transcript projection crosses
the authenticated worker boundary while protected dependency IDs stay private.
"""

from dataclasses import dataclass, replace
import hashlib
import json
import math

from app.core.context import (
    ContextLimits,
    ContextRole,
    ContextTrust,
    ConversationSegment,
    PrivateContextAssembler,
)
from app.core.model_contract import (
    MAX_MODEL_REQUEST_UTF8_BYTES,
    ModelRequest,
    build_model_request,
    TRANSCRIPT_MODEL_CONTRACT_VERSION,
)
from app.core.agent_output_store import AgentOutputStore, ResultState
from app.core.self_state_store import SelfStateStore
from app.privacy.application import PrivacyReadBlocked

from .contracts import MemoryRecord, RetrievalPurpose, RetrievalRequest
from .read_store import CognitionReadStore
from .generic_store import MAX_GENERIC_SOURCES, GenericMemoryStore
from .retrieval import MemoryContextSelector


MEMORY_MODEL_CONTRACT_VERSION = "model-contract-private-memory-v2"
TRANSCRIPT_CONTEXT_CONTRACT_VERSION = "model-contract-conversation-transcript-v4"
MAX_MEMORY_RECORDS = 16


@dataclass(frozen=True, slots=True)
class ModelMemory:
    """Closed non-authoritative memory value, with no protected routing fields."""

    kind: str
    text: str
    status: str
    confidence: float | None
    stance: str | None
    priority: int | None
    revision_reason: str
    supporting_evidence_count: int
    counterevidence_count: int
    reason_for_interest: str | None
    alternatives: tuple[str, ...]
    rationale: str | None
    constraints: tuple[str, ...]
    review_condition: str | None
    verification: str
    provenance: str

    def __post_init__(self):
        closed = (
            (
                self.kind,
                {
                    "durable_interest",
                    "belief_or_hypothesis",
                    "goal_or_intention",
                    "unresolved_question",
                    "candidate_useful_memory",
                    "contradiction_counterevidence",
                },
            ),
            (
                self.status,
                {"active", "suspended"},
            ),
            (self.stance, {None, "supports", "opposes", "uncertain"}),
            (self.verification, {"unverified"}),
            (self.provenance, {"model_inference", "participant_assertion"}),
        )
        for value, allowed in closed:
            if type(value) not in {str, type(None)} or value not in allowed:
                raise ValueError("invalid model memory classification")
        for value in (self.text, self.revision_reason):
            _text(value)
        for value in (self.reason_for_interest, self.rationale, self.review_condition):
            if value is not None:
                _text(value)
        for values in (self.alternatives, self.constraints):
            if type(values) is not tuple or len(values) > 32:
                raise ValueError("invalid model memory collection")
            for value in values:
                _text(value)
        if self.confidence is not None:
            if (
                type(self.confidence) is not float
                or not math.isfinite(self.confidence)
                or not 0 <= self.confidence <= 1
            ):
                raise ValueError("invalid model memory confidence")
        if self.priority is not None:
            if type(self.priority) is not int or not 0 <= self.priority <= 100:
                raise ValueError("invalid model memory priority")
        bound = (
            MAX_GENERIC_SOURCES
            if self.kind
            in {
                "unresolved_question",
                "candidate_useful_memory",
                "contradiction_counterevidence",
            }
            else 50
        )
        for value in (self.supporting_evidence_count, self.counterevidence_count):
            if type(value) is not int or not 0 <= value <= bound:
                raise ValueError("invalid model memory evidence count")

    @classmethod
    def from_record(cls, record: MemoryRecord):
        return cls(
            record.memory_kind.value,
            record.text,
            record.status,
            record.confidence,
            None if record.stance is None else record.stance.value,
            record.priority,
            record.revision_reason,
            record.supporting_evidence_count,
            record.counterevidence_count,
            record.reason_for_interest,
            record.alternatives,
            record.rationale,
            record.constraints,
            record.review_condition,
            record.verification.value,
            record.provenance.value,
        )

    def structured(self):
        return {
            "kind": self.kind,
            "text": self.text,
            "status": self.status,
            "confidence": self.confidence,
            "stance": self.stance,
            "priority": self.priority,
            "revision_reason": self.revision_reason,
            "supporting_evidence_count": self.supporting_evidence_count,
            "counterevidence_count": self.counterevidence_count,
            "reason_for_interest": self.reason_for_interest,
            "alternatives": list(self.alternatives),
            "rationale": self.rationale,
            "constraints": list(self.constraints),
            "review_condition": self.review_condition,
            "verification": self.verification,
            "provenance": self.provenance,
            "trust": "untrusted_memory_data",
            "can_instruct": False,
        }


def _text(value):
    if type(value) is not str or not value.strip():
        raise ValueError("invalid model memory text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("invalid model memory text") from None
    if size > 4096:
        raise ValueError("invalid model memory text")


@dataclass(frozen=True, slots=True)
class MemoryModelRequest:
    """Assembler-owned v2 input; bounded as one complete serialized request."""

    conversation: ModelRequest
    memories: tuple[ModelMemory, ...]
    memories_omitted: bool
    generic_operation_ids: tuple[str, ...] = ()
    inherited_shared_operation_ids: tuple[str, ...] = ()

    def serialize(self) -> str:
        serialized = self._serialized()
        if len(serialized.encode("utf-8")) > MAX_MODEL_REQUEST_UTF8_BYTES:
            raise ValueError("memory request exceeds serialized byte bound")
        return serialized

    def _serialized(self) -> str:
        if (
            type(self.conversation) is not ModelRequest
            or type(self.memories) is not tuple
            or len(self.memories) > MAX_MEMORY_RECORDS
            or any(type(memory) is not ModelMemory for memory in self.memories)
            or type(self.memories_omitted) is not bool
        ):
            raise ValueError("invalid memory request")
        data = self.conversation.to_structured_data()
        data["contract_version"] = (
            TRANSCRIPT_CONTEXT_CONTRACT_VERSION
            if self.conversation.contract_version == TRANSCRIPT_MODEL_CONTRACT_VERSION
            else MEMORY_MODEL_CONTRACT_VERSION
        )
        data["memories"] = [memory.structured() for memory in self.memories]
        data["memories_omitted"] = self.memories_omitted
        return json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


class _Events:
    def __init__(self, events):
        self._events = events

    def retrieve_private_events(self, _context):
        return self._events


class MemoryConversationAssembler:
    """Authorize before ranking, using the caller's consistent SQLite snapshot.

    Dependencies are the complete revision-chain union, retained separately from
    the model values. A released reply is untrusted transcript data.
    """

    def __init__(self, store, limits: ContextLimits):
        self._store = store
        self._limits = limits
        self._state = SelfStateStore(store)
        self._read = CognitionReadStore(store, self._state)
        self._selector = MemoryContextSelector(store, self._state)
        self._generic = GenericMemoryStore(store)

    def build(self, principal, event_id, db, *, reply_permitted=lambda _reply: True):
        events = self._store.retrieve_private_events_in_commit_order(
            principal,
            event_id,
            _connection=db,
        )
        if not events or events[-1].event_id != event_id:
            raise PrivacyReadBlocked("original_input_outside_context_window")
        # Relevance words use a bounded prefix; content itself is never truncated.
        topic = events[-1].content[:1024]
        authorized = self._read.retrieve_all_private_candidate_reads(
            principal,
            _connection=db,
        )
        dependencies = {
            (
                self._selector._record(
                    read.candidate,
                    read.decided_at,
                    read.decision_origin,
                    read.revision_reason,
                    read.core_self_version,
                    read.model_identifier,
                    read.prompt_version,
                ).memory_kind,
                read.candidate.item_id,
                read.candidate.revision,
            ): read.evidence_event_ids
            for read in authorized
        }
        dependencies.update(
            {
                (
                    read.view.memory_kind,
                    read.view.item_id,
                    read.view.revision,
                ): read.source_event_ids
                for read in self._generic.current(principal, _connection=db)
            }
        )
        generic_operations = {
            (
                read.view.memory_kind,
                read.view.item_id,
                read.view.revision,
            ): read.operation_id
            for read in self._generic.current(principal, _connection=db)
        }
        selection = self._selector.select(
            principal,
            RetrievalRequest(
                RetrievalPurpose.CONVERSATION,
                topic,
                self._limits.max_utf8_bytes,
                MAX_MEMORY_RECORDS,
            ),
            _connection=db,
        )
        # Keep current input first, then ranked memories, then recent history.
        base = build_model_request(
            PrivateContextAssembler(
                _Events(events[-1:]),
                self._limits,
            ).build(principal)
        )
        base = replace(base, contract_version=TRANSCRIPT_MODEL_CONTRACT_VERSION)
        request = MemoryModelRequest(base, (), selection.records_omitted)
        sources = {event_id}
        if (
            not base.conversation
            or len(request.serialize().encode("utf-8")) > self._limits.max_utf8_bytes
        ):
            raise PrivacyReadBlocked("current_input_exceeds_combined_budget")
        for record in selection.records:
            evidence = dependencies[
                (record.memory_kind, record.item_id, record.revision)
            ]
            proposed = replace(
                request, memories=(*request.memories, ModelMemory.from_record(record))
            )
            if not evidence:
                raise PrivacyReadBlocked("memory_dependencies_unavailable")
            if (
                len(sources | set(evidence)) > 100
                or len(proposed._serialized().encode("utf-8"))
                > self._limits.max_utf8_bytes
            ):
                request = replace(request, memories_omitted=True)
                continue
            sources.update(evidence)
            generic_operation = generic_operations.get(
                (record.memory_kind, record.item_id, record.revision)
            )
            request = (
                proposed
                if generic_operation is None
                else replace(
                    proposed,
                    generic_operation_ids=(
                        *proposed.generic_operation_ids,
                        generic_operation,
                    ),
                )
            )
        history_omitted = False
        outputs = AgentOutputStore(self._store)
        selected_history = ()
        inherited_shared = set()
        inherited_generic = set(request.generic_operation_ids)
        shared_sources = set()

        def source_budget_exceeded(extra_private, extra_shared):
            return len(sources | shared_sources | extra_private | extra_shared) > 100

        for event in reversed(events[:-1]):
            history = build_model_request(
                PrivateContextAssembler(
                    _Events((event,)),
                    self._limits,
                ).build(principal)
            ).conversation
            if not history:
                history_omitted = True
                continue
            operation_id = (
                "reply:" + hashlib.sha256(event.event_id.encode()).hexdigest()
            )
            reply = outputs._read(db, principal, operation_id)
            resolution = (
                db.execute(
                    "SELECT outcome, release_id FROM agent_reply_resolutions WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if reply is not None
                else None
            )
            released = (
                reply is not None
                and reply.result_state is ResultState.PERSISTED
                and resolution == ("completed", f"release:{operation_id}")
                and reply_permitted(reply)
            )
            agent = (
                ()
                if not released
                else (
                    ConversationSegment(
                        ContextRole.AGENT,
                        ContextTrust.UNTRUSTED_CONVERSATIONAL_DATA,
                        reply.content,
                    ),
                )
            )
            addition = (*history, *agent)
            extra_sources = set(reply.operation.source_event_ids) if released else set()
            extra_shared = (
                set(reply.operation.shared_operation_ids) if released else set()
            )
            extra_generic = (
                set(reply.operation.generic_operation_ids) if released else set()
            )
            extra_shared_sources = {
                row[0]
                for shared_id in extra_shared
                for row in db.execute(
                    "SELECT event_id FROM protected_shared_sources WHERE operation_id=?",
                    (shared_id,),
                )
            }
            # Prefer the complete released exchange. If its dependency union cannot
            # fit, keep the participant input only when that also fits.
            proposed = replace(
                request,
                conversation=replace(
                    request.conversation,
                    conversation=(*addition, *selected_history, *base.conversation),
                ),
            )
            if (
                len(proposed.conversation.conversation)
                > self._limits.max_history_events
                or source_budget_exceeded(
                    {event.event_id} | extra_sources, extra_shared_sources
                )
                or len(inherited_shared | extra_shared) > 16
                or len(inherited_generic | extra_generic) > 16
                or len(proposed._serialized().encode("utf-8"))
                > self._limits.max_utf8_bytes
            ):
                history_omitted = True
                agent = ()
                extra_sources = set()
                extra_shared = set()
                extra_generic = set()
                extra_shared_sources = set()
                proposed = replace(
                    request,
                    conversation=replace(
                        request.conversation,
                        conversation=(*history, *selected_history, *base.conversation),
                    ),
                )
                if (
                    len(proposed.conversation.conversation)
                    > self._limits.max_history_events
                    or source_budget_exceeded({event.event_id}, set())
                    or len(proposed._serialized().encode("utf-8"))
                    > self._limits.max_utf8_bytes
                ):
                    continue
            selected_history = (*history, *agent, *selected_history)
            sources.update({event.event_id} | extra_sources)
            inherited_shared.update(extra_shared)
            inherited_generic.update(extra_generic)
            shared_sources.update(extra_shared_sources)
            request = proposed
        request = replace(
            request,
            conversation=replace(
                request.conversation,
                history_omitted=history_omitted or len(events) == 100,
            ),
        )
        return replace(
            request,
            generic_operation_ids=tuple(sorted(inherited_generic)),
            inherited_shared_operation_ids=tuple(sorted(inherited_shared)),
        ), tuple(sorted(sources))
