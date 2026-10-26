"""Closed, secretless public behavior worker exchange.

Only trusted application code constructs these fields. Public text is context, never
an authority source; the worker owns its model, tools, credentials and retry policy.
"""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import re

from app.research.assessment import MAX_PUBLIC_EVIDENCE_BYTES
from app.threads.domain import require_aware_time, require_reference
from app.behavior.deliberation import (
    BehaviorProposalValidation,
    parse_behavior_proposal,
)
from app.core.agent_proposals import AgentDecision, ProposalInvalidReason
from app.behavior.mission import MissionPurpose

VERSION = "public-behavior-v1"
MAX_FRAME = 65_536
MAX_CONTEXT = 2_048
_TOPIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,120}\Z")


@dataclass(frozen=True, slots=True)
class PublicBehaviorRequest:
    operation_id: str
    correlation_id: str
    trigger: str
    context: tuple[str, ...]
    mission: tuple[str, ...]
    deadline_seconds: float
    reservation: dict | None = None
    catalog: tuple[tuple[str, str], ...] = ()
    evidence: tuple[str, ...] = ()
    mission_evolution: tuple[str, ...] = ()
    deadline_at: datetime | None = None

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
            type(self.catalog) is not tuple
            or len(self.catalog) > 16
            or any(
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or _TOPIC_ID.fullmatch(item[0]) is None
                or type(item[1]) is not str
                or not item[1]
                or len(item[1].encode()) > 512
                for item in self.catalog
            )
            or len({item[0] for item in self.catalog}) != len(self.catalog)
            or type(self.evidence) is not tuple
            or len(self.evidence) > 1
            or any(
                type(item) is not str or len(item.encode()) > MAX_PUBLIC_EVIDENCE_BYTES
                for item in self.evidence
            )
        ):
            raise ValueError("invalid public research context")
        if self.mission_evolution not in ((), ("enabled",)) and not (
            type(self.mission_evolution) is tuple
            and len(self.mission_evolution) == 2
            and self.mission_evolution[0] == "pending"
            and self.mission_evolution[1] in MissionPurpose._value2member_map_
        ):
            raise ValueError("invalid mission evolution context")
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
            "catalog": self.catalog,
            "evidence": self.evidence,
        }
        if self.reservation is not None:
            if type(self.reservation) is not dict or set(self.reservation) != {
                "account",
                "kind",
                "event",
                "resumed",
                "runtime_version",
                "phase",
                "context_digest",
                "mission",
                "timeout",
            }:
                raise ValueError("invalid application reservation evidence")
            value["reservation"] = self.reservation
        if self.mission_evolution:
            value["mission_evolution"] = self.mission_evolution
        if self.deadline_at is not None:
            require_aware_time("public inference deadline", self.deadline_at)
            value["deadline_at"] = self.deadline_at.isoformat()
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
            or set(value) - {"mission_evolution", "deadline_at"}
            not in (
                {
                    "version",
                    "operation_id",
                    "correlation_id",
                    "trigger",
                    "context",
                    "mission",
                    "deadline_seconds",
                    "catalog",
                    "evidence",
                },
                {
                    "version",
                    "operation_id",
                    "correlation_id",
                    "trigger",
                    "context",
                    "mission",
                    "deadline_seconds",
                    "catalog",
                    "evidence",
                    "reservation",
                },
            )
            or value["version"] != VERSION
        ):
            raise ValueError("invalid public request")
        if any(
            type(value[key]) is not list
            for key in ("context", "mission", "catalog", "evidence")
        ):
            raise ValueError("invalid public request")
        request = PublicBehaviorRequest(
            value["operation_id"],
            value["correlation_id"],
            value["trigger"],
            tuple(value["context"]),
            tuple(value["mission"]),
            value["deadline_seconds"],
            value.get("reservation"),
            tuple(
                tuple(item) if type(item) is list else item for item in value["catalog"]
            ),
            tuple(value["evidence"]),
            tuple(value.get("mission_evolution", ())),
            datetime.fromisoformat(value["deadline_at"])
            if "deadline_at" in value
            else None,
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


@dataclass(frozen=True, slots=True)
class PublicMissionChoice:
    action: str
    purpose: MissionPurpose | None
    decision: AgentDecision = AgentDecision.REVISE_MISSION


def parse_public_proposal(value: object, *, evolution: bool = False):
    """Public-only closed mission choice, without model-selected authority fields."""
    if type(value) is dict and value.get("decision") == "mission":
        if not evolution or set(value) != {"decision", "action", "purpose"}:
            return BehaviorProposalValidation(
                None, ProposalInvalidReason.UNKNOWN_FIELDS
            )
        action, raw_purpose = value["action"], value["purpose"]
        if (
            action not in ("propose", "confirm", "reject", "abandon")
            or (
                action == "propose"
                and (
                    type(raw_purpose) is not str
                    or raw_purpose not in MissionPurpose._value2member_map_
                )
            )
            or (action != "propose" and raw_purpose is not None)
        ):
            return BehaviorProposalValidation(None, ProposalInvalidReason.INVALID_FIELD)
        return BehaviorProposalValidation(
            PublicMissionChoice(
                action, MissionPurpose(raw_purpose) if action == "propose" else None
            ),
            None,
        )
    return parse_behavior_proposal(value)
