"""Closed model data contract for one participant-private typed-state change."""

from dataclasses import dataclass
import json
import math
import re

from app.core.store import MAX_STATE_TEXT_BYTES

from .worker_evolution import MAX_MEMORIES, MAX_PROJECTION_BYTES, MAX_OUTPUT_BYTES
from .worker_extraction import MAX_INPUT_BYTES


CONTRACT_VERSION = "model-contract-private-typed-v1"
_HANDLE = re.compile(r"h[0-9a-f]{16}\Z")
_FIELDS = {
    "durable_interest": ("topic", "strength", "status", "reason_for_interest"),
    "belief_or_hypothesis": (
        "proposition",
        "stance",
        "confidence",
        "status",
        "alternatives",
    ),
    "goal_or_intention": (
        "description",
        "rationale",
        "priority",
        "status",
        "constraints",
        "review_condition",
    ),
}
_STATUSES = {
    "durable_interest": {"active", "suspended", "abandoned"},
    "belief_or_hypothesis": {"active", "suspended", "abandoned"},
    "goal_or_intention": {"active", "suspended", "completed", "abandoned"},
}


def _text(value: object) -> bool:
    return (
        type(value) is str
        and bool(value.strip())
        and len(value.encode("utf-8")) <= MAX_STATE_TEXT_BYTES
    )


def _fields(kind: object, fields: object) -> None:
    if (
        kind not in _FIELDS
        or type(fields) is not dict
        or set(fields) != set(_FIELDS[kind])
    ):
        raise ValueError("invalid typed fields")
    if any(
        not _text(fields[key])
        for key in _FIELDS[kind]
        if key
        not in {"strength", "priority", "confidence", "alternatives", "constraints"}
    ):
        raise ValueError("invalid typed fields")
    if fields["status"] not in _STATUSES[kind]:
        raise ValueError("invalid typed status")
    if kind == "durable_interest":
        if type(fields["strength"]) is not int or not 0 <= fields["strength"] <= 100:
            raise ValueError("invalid interest strength")
    elif kind == "belief_or_hypothesis":
        confidence = fields["confidence"]
        if (
            type(confidence) not in {int, float}
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
            or fields["stance"] not in {"supports", "opposes", "uncertain"}
        ):
            raise ValueError("invalid belief confidence or stance")
        _string_list(fields["alternatives"])
    else:
        if type(fields["priority"]) is not int or not 0 <= fields["priority"] <= 100:
            raise ValueError("invalid goal priority")
        _string_list(fields["constraints"])


def _string_list(value: object) -> None:
    if (
        type(value) is not list
        or len(value) > 20
        or any(not _text(item) for item in value)
    ):
        raise ValueError("invalid typed list")


@dataclass(frozen=True, slots=True)
class TypedMemory:
    handle: str
    kind: str
    fields: dict[str, object]


@dataclass(frozen=True, slots=True)
class TypedModelRequest:
    participant_input: str
    memories: tuple[TypedMemory, ...]

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
            visible = []
            for memory in self.memories:
                if (
                    type(memory) is not TypedMemory
                    or type(memory.handle) is not str
                    or _HANDLE.fullmatch(memory.handle) is None
                    or memory.handle in handles
                ):
                    raise ValueError
                _fields(memory.kind, memory.fields)
                handles.add(memory.handle)
                visible.append(
                    {
                        "handle": memory.handle,
                        "kind": memory.kind,
                        "fields": memory.fields,
                        "verification": "unverified",
                        "trust": "untrusted_memory_data",
                        "can_instruct": False,
                    }
                )
            output = json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "task": "evolve_private_typed_memory",
                    "participant_input": self.participant_input,
                    "participant_input_trust": "untrusted_conversational_data",
                    "participant_input_can_instruct": False,
                    "memories": visible,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
            if len(output.encode("utf-8")) > MAX_PROJECTION_BYTES:
                raise ValueError
            return output
        except (OverflowError, TypeError, UnicodeError, ValueError):
            raise ValueError("invalid typed request") from None


@dataclass(frozen=True, slots=True)
class TypedProposal:
    decision: str
    handle: str | None
    kind: str | None
    fields: dict[str, object] | None
    source_role: str | None


def parse_typed_proposal(text: str) -> TypedProposal:
    """Parse an untrusted single operation without accepting authority fields."""
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

        data = json.loads(
            text,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if type(data) is not dict or set(data) != {
            "decision",
            "handle",
            "kind",
            "fields",
            "source_role",
        }:
            raise ValueError
        decision, handle, kind, fields, role = (
            data[key] for key in ("decision", "handle", "kind", "fields", "source_role")
        )
        if decision == "no_change" and (handle, kind, fields, role) == (None,) * 4:
            return TypedProposal(decision, None, None, None, None)
        if (
            decision not in {"new_memory", "revise_memory"}
            or (
                handle is not None
                if decision == "new_memory"
                else type(handle) is not str or _HANDLE.fullmatch(handle) is None
            )
            or role not in {"supporting", "counterevidence"}
            or (role == "counterevidence" and kind != "belief_or_hypothesis")
        ):
            raise ValueError
        _fields(kind, fields)
        return TypedProposal(decision, handle, kind, fields, role)
    except (TypeError, UnicodeError, ValueError, OverflowError):
        raise ValueError("invalid typed proposal") from None
