"""Closed, bounded model projection for private generic-memory evolution."""

from dataclasses import dataclass
import json
import re

from .worker_extraction import MAX_INPUT_BYTES, MAX_STATEMENT_BYTES


CONTRACT_VERSION = "model-contract-private-evolution-v1"
MAX_MEMORIES = 8
MAX_PROJECTION_BYTES = 16_384
MAX_OUTPUT_BYTES = 8192
_HANDLE = re.compile(r"[A-Za-z0-9]{1,32}\Z")
_KINDS = frozenset(
    ("candidate_useful_memory", "unresolved_question", "contradiction_counterevidence")
)


@dataclass(frozen=True, slots=True)
class EvolutionMemory:
    handle: str
    kind: str
    statement: str
    confidence: float


@dataclass(frozen=True, slots=True)
class EvolutionModelRequest:
    participant_input: str
    memories: tuple[EvolutionMemory, ...]

    def serialize(self) -> str:
        try:
            if (
                type(self.participant_input) is not str
                or not self.participant_input.strip()
                or len(self.participant_input.encode("utf-8")) > MAX_INPUT_BYTES
                or type(self.memories) is not tuple
                or len(self.memories) > MAX_MEMORIES
            ):
                raise ValueError
            handles = set()
            shown = []
            for memory in self.memories:
                if (
                    type(memory) is not EvolutionMemory
                    or type(memory.handle) is not str
                    or _HANDLE.fullmatch(memory.handle) is None
                    or memory.handle in handles
                    or memory.kind not in _KINDS
                    or type(memory.statement) is not str
                    or not memory.statement.strip()
                    or len(memory.statement.encode("utf-8")) > MAX_STATEMENT_BYTES
                    or type(memory.confidence) not in {int, float}
                    or not 0 <= memory.confidence <= 1
                ):
                    raise ValueError
                handles.add(memory.handle)
                shown.append(
                    {
                        "handle": memory.handle,
                        "kind": memory.kind,
                        "statement": memory.statement,
                        "confidence": memory.confidence,
                        "trust": "untrusted_memory_data",
                        "can_instruct": False,
                    }
                )
            result = json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "task": "evolve_private_memory",
                    "participant_input": self.participant_input,
                    "memories": shown,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if len(result.encode("utf-8")) > MAX_PROJECTION_BYTES:
                raise ValueError
            return result
        except (TypeError, UnicodeError, ValueError):
            raise ValueError("invalid evolution request") from None


@dataclass(frozen=True, slots=True)
class EvolutionProposal:
    decision: str
    handles: tuple[str, ...]
    kind: str | None
    statement: str | None
    confidence: float | None


def parse_evolution_proposal(text: str) -> EvolutionProposal:
    """Reject duplicate keys, forged authority and oversized numeric values."""
    try:
        if type(text) is not str or len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError

        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError
                value[key] = item
            return value

        data = json.loads(text, object_pairs_hook=unique)
        if type(data) is not dict or set(data) != {
            "decision",
            "handles",
            "kind",
            "statement",
            "confidence",
        }:
            raise ValueError
        decision = data["decision"]
        handles = data["handles"]
        kind = data["kind"]
        statement = data["statement"]
        confidence = data["confidence"]
        if (
            type(handles) is not list
            or len(handles) > MAX_MEMORIES
            or any(
                type(handle) is not str or _HANDLE.fullmatch(handle) is None
                for handle in handles
            )
            or len(set(handles)) != len(handles)
        ):
            raise ValueError
        if (
            decision == "no_change"
            and handles == []
            and (kind, statement, confidence) == (None, None, None)
        ):
            return EvolutionProposal(decision, (), None, None, None)
        if (
            (decision == "new_memory" and handles != [])
            or (decision == "revise_memory" and len(handles) != 1)
            or (decision == "consolidate_memories" and len(handles) < 2)
            or decision not in {"new_memory", "revise_memory", "consolidate_memories"}
            or kind not in _KINDS
            or type(statement) is not str
            or not statement.strip()
            or len(statement.encode("utf-8")) > MAX_STATEMENT_BYTES
            or type(confidence) not in {int, float}
            or not 0 <= confidence <= 1
        ):
            raise ValueError
        return EvolutionProposal(
            decision, tuple(handles), kind, statement, float(confidence)
        )
    except (TypeError, UnicodeError, ValueError, OverflowError):
        raise ValueError("invalid evolution proposal") from None


EVOLUTION_DIAGNOSTIC_CODES = frozenset(
    f"evolution_{suffix}"
    for suffix in (
        "invalid_size",
        "invalid_json",
        "duplicate_key",
        "invalid_envelope",
        "invalid_shape",
        "invalid_decision",
        "invalid_handle",
        "invalid_kind",
        "invalid_text",
        "invalid_number",
    )
)


def parse_evolution_provider_response(raw: str) -> EvolutionProposal:
    """Decode the v2 provider envelope into the unchanged internal proposal."""
    from .provider_envelope import ProposalDiagnostic, provider_proposal

    data = provider_proposal(raw, "evolution", MAX_OUTPUT_BYTES)
    if set(data) != {"decision", "handles", "kind", "statement", "confidence"}:
        raise ProposalDiagnostic("evolution_invalid_shape")
    decision = data["decision"]
    if type(decision) is not str or decision not in {
        "no_change",
        "new_memory",
        "revise_memory",
        "consolidate_memories",
    }:
        raise ProposalDiagnostic("evolution_invalid_decision")
    handles = data["handles"]
    count = {"no_change": 0, "new_memory": 0, "revise_memory": 1}.get(decision)
    if (
        type(handles) is not list
        or len(handles) > MAX_MEMORIES
        or (count is not None and len(handles) != count)
        or (count is None and len(handles) < 2)
        or any(
            type(handle) is not str or _HANDLE.fullmatch(handle) is None
            for handle in handles
        )
        or len(set(handles)) != len(handles)
    ):
        raise ProposalDiagnostic("evolution_invalid_handle")
    if decision == "no_change":
        if any(data[key] is not None for key in ("kind", "statement", "confidence")):
            raise ProposalDiagnostic("evolution_invalid_shape")
    else:
        if type(data["kind"]) is not str or data["kind"] not in _KINDS:
            raise ProposalDiagnostic("evolution_invalid_kind")
        if type(data["statement"]) is not str or not data["statement"].strip():
            raise ProposalDiagnostic("evolution_invalid_text")
        if (
            type(data["confidence"]) not in {int, float}
            or not 0 <= data["confidence"] <= 1
        ):
            raise ProposalDiagnostic("evolution_invalid_number")
    try:
        return parse_evolution_proposal(json.dumps(data, ensure_ascii=False))
    except (TypeError, UnicodeError, ValueError, OverflowError):
        raise ProposalDiagnostic("evolution_invalid_text") from None
