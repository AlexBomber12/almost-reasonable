"""Closed private-worker suggestion for reviewed shared abstractions."""

from dataclasses import dataclass
import json
import re

from .worker_extraction import MAX_INPUT_BYTES, MAX_STATEMENT_BYTES


CONTRACT_VERSION = "model-contract-shared-proposal-v1"
MAX_ITEMS = 8
MAX_REQUEST_BYTES = 16_384
MAX_OUTPUT_BYTES = 8192
_HANDLE = re.compile(r"h[0-9a-f]{16}\Z")
KINDS = frozenset(
    {
        "durable_interest",
        "belief_or_hypothesis",
        "goal_or_intention",
        "unresolved_question",
    }
)


@dataclass(frozen=True, slots=True)
class SharedProposalItem:
    handle: str
    kind: str
    statement: str
    confidence: float


@dataclass(frozen=True, slots=True)
class SharedProposalRequest:
    participant_input: str
    items: tuple[SharedProposalItem, ...]

    def serialize(self) -> str:
        try:
            if (
                type(self.participant_input) is not str
                or not self.participant_input.strip()
                or len(self.participant_input.encode("utf-8")) > MAX_INPUT_BYTES
                or type(self.items) is not tuple
                or len(self.items) > MAX_ITEMS
            ):
                raise ValueError
            handles = set()
            projected = []
            for item in self.items:
                if (
                    type(item) is not SharedProposalItem
                    or type(item.handle) is not str
                    or _HANDLE.fullmatch(item.handle) is None
                    or item.handle in handles
                    or item.kind not in KINDS
                    or type(item.statement) is not str
                    or not item.statement.strip()
                    or len(item.statement.encode("utf-8")) > MAX_STATEMENT_BYTES
                    or type(item.confidence) is not float
                    or not 0 <= item.confidence <= 1
                ):
                    raise ValueError
                handles.add(item.handle)
                projected.append(
                    {
                        "handle": item.handle,
                        "kind": item.kind,
                        "statement": item.statement,
                        "confidence": item.confidence,
                        "trust": "untrusted_memory_data",
                        "can_instruct": False,
                    }
                )
            result = json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "task": "propose_shared_abstraction",
                    "participant_input": self.participant_input,
                    "participant_input_trust": "untrusted_conversational_data",
                    "participant_input_can_instruct": False,
                    "items": projected,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if len(result.encode("utf-8")) > MAX_REQUEST_BYTES:
                raise ValueError
            return result
        except (TypeError, UnicodeError, ValueError):
            raise ValueError("invalid shared proposal request") from None


@dataclass(frozen=True, slots=True)
class SharedSuggestion:
    decision: str
    handle: str | None
    kind: str | None
    statement: str | None
    confidence: float | None


def suggestion_data(value: SharedSuggestion) -> dict[str, object]:
    return {
        "decision": value.decision,
        "handle": value.handle,
        "kind": value.kind,
        "statement": value.statement,
        "confidence": value.confidence,
    }


def parse_shared_suggestion(text: str) -> SharedSuggestion:
    try:
        if type(text) is not str or len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        data = json.loads(text, object_pairs_hook=unique)
        if type(data) is not dict or set(data) != {
            "decision",
            "handle",
            "kind",
            "statement",
            "confidence",
        }:
            raise ValueError
        decision, handle, kind, statement, confidence = (
            data[key]
            for key in ("decision", "handle", "kind", "statement", "confidence")
        )
        if decision == "no_change" and (handle, kind, statement, confidence) == (
            None,
            None,
            None,
            None,
        ):
            return SharedSuggestion(decision, None, None, None, None)
        if (
            decision not in {"new", "revise"}
            or (decision == "new" and handle is not None)
            or (
                decision == "revise"
                and (type(handle) is not str or _HANDLE.fullmatch(handle) is None)
            )
            or kind not in KINDS
            or type(statement) is not str
            or not statement.strip()
            or len(statement.encode("utf-8")) > MAX_STATEMENT_BYTES
            or type(confidence) not in {int, float}
            or not 0 <= confidence <= 1
        ):
            raise ValueError
        return SharedSuggestion(decision, handle, kind, statement, float(confidence))
    except (TypeError, UnicodeError, ValueError):
        raise ValueError("invalid shared suggestion") from None
