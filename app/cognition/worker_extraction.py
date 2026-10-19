"""Bounded model projection and untrusted private memory extraction result."""

from dataclasses import dataclass
import json


CONTRACT_VERSION = "model-contract-private-extraction-v1"
MAX_INPUT_BYTES = 8192
MAX_STATEMENT_BYTES = 4096
_KINDS = frozenset(
    ("candidate_useful_memory", "unresolved_question", "contradiction_counterevidence")
)


@dataclass(frozen=True, slots=True)
class ExtractionModelRequest:
    participant_input: str

    def serialize(self) -> str:
        value = self.participant_input
        if type(value) is not str or not value.strip():
            raise ValueError("invalid extraction input")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            raise ValueError("invalid extraction input") from None
        if size > MAX_INPUT_BYTES:
            raise ValueError("extraction input too large")
        return json.dumps(
            {
                "contract_version": CONTRACT_VERSION,
                "task": "extract_private_memory",
                "participant_input": value,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class ExtractionProposal:
    decision: str
    kind: str | None
    statement: str | None
    confidence: float | None


def parse_extraction_proposal(text: str) -> ExtractionProposal:
    """Reject extra keys, duplicate keys, authority fields and invalid content."""
    try:
        if type(text) is not str or len(text.encode("utf-8")) > 8192:
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
            "kind",
            "statement",
            "confidence",
        }:
            raise ValueError
        decision = data["decision"]
        kind = data["kind"]
        statement = data["statement"]
        confidence = data["confidence"]
        if decision == "no_change" and (kind, statement, confidence) == (
            None,
            None,
            None,
        ):
            return ExtractionProposal(decision, None, None, None)
        if (
            decision != "new_memory"
            or kind not in _KINDS
            or type(statement) is not str
            or not statement.strip()
            or len(statement.encode("utf-8")) > MAX_STATEMENT_BYTES
            or type(confidence) not in {int, float}
            or not 0 <= confidence <= 1
        ):
            raise ValueError
        return ExtractionProposal(decision, kind, statement, float(confidence))
    except (TypeError, UnicodeError, ValueError):
        raise ValueError("invalid extraction proposal") from None
