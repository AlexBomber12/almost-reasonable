"""Closed, secretless public behavior worker exchange.

Only trusted application code constructs these fields. Public text is context, never
an authority source; the worker owns its model, tools, credentials and retry policy.
"""

from dataclasses import dataclass
import hashlib
import json
import math

from app.threads.domain import require_reference

VERSION = "public-behavior-v1"
MAX_FRAME = 32_768
MAX_CONTEXT = 2_048


@dataclass(frozen=True, slots=True)
class PublicBehaviorRequest:
    operation_id: str
    correlation_id: str
    trigger: str
    context: tuple[str, ...]
    mission: tuple[str, ...]
    deadline_seconds: float

    def encode(self) -> bytes:
        require_reference("operation ID", self.operation_id)
        require_reference("correlation ID", self.correlation_id)
        if self.trigger not in ("autonomous_wakeup", "public_message"):
            raise ValueError("invalid public trigger")
        if (
            type(self.context) is not tuple
            or len(self.context) > 1
            or type(self.mission) is not tuple
            or len(self.mission) > 1
            or any(
                type(v) is not str or not v or len(v.encode()) > MAX_CONTEXT
                for v in (*self.context, *self.mission)
            )
        ):
            raise ValueError("invalid public context")
        if (
            type(self.deadline_seconds) not in (float, int)
            or not math.isfinite(self.deadline_seconds)
            or not 0 < self.deadline_seconds <= 10
        ):
            raise ValueError("invalid inference deadline")
        value = {
            "version": VERSION,
            "operation_id": self.operation_id,
            "correlation_id": self.correlation_id,
            "trigger": self.trigger,
            "context": self.context,
            "mission": self.mission,
            "deadline_seconds": self.deadline_seconds,
        }
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        if len(data) > MAX_FRAME:
            raise ValueError("public request too large")
        return data


def decode_public_request(data: bytes) -> PublicBehaviorRequest:
    if type(data) is not bytes or len(data) > MAX_FRAME:
        raise ValueError("invalid public request")
    try:
        value = json.loads(data, object_pairs_hook=_unique_fields)
        if (
            type(value) is not dict
            or set(value)
            != {
                "version",
                "operation_id",
                "correlation_id",
                "trigger",
                "context",
                "mission",
                "deadline_seconds",
            }
            or value["version"] != VERSION
        ):
            raise ValueError("invalid public request")
        if type(value["context"]) is not list or type(value["mission"]) is not list:
            raise ValueError("invalid public request")
        request = PublicBehaviorRequest(
            value["operation_id"],
            value["correlation_id"],
            value["trigger"],
            tuple(value["context"]),
            tuple(value["mission"]),
            value["deadline_seconds"],
        )
        request.encode()
        return request
    except (TypeError, KeyError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid public request") from error


def _unique_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate public request field")
        value[key] = item
    return value


def public_context_digest(context: tuple[str, ...]) -> str:
    """Bind the exact public context without retaining its text in the journal."""

    encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
