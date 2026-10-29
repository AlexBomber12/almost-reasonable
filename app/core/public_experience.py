"""Closed, bounded public data projection. No authority or protected origins."""

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json

from app.threads.domain import require_aware_time, require_reference

EXPERIENCE_VERSION = "public-experience-v1"
MAX_RECORDS = 6
MAX_RECORD_BYTES = 1536
MAX_EXPERIENCE_BYTES = 8192


def serialized(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


@dataclass(frozen=True, slots=True)
class PublicExperienceRecord:
    kind: str
    occurred_at: str
    text: str
    message_id: str | None
    parent_message_id: str | None
    conversation_id: str | None

    def value(self):
        if self.kind not in (
            "external_assertion",
            "confirmed_publication",
            "no_action",
        ):
            raise ValueError("invalid public experience kind")
        require_aware_time("experience time", datetime.fromisoformat(self.occurred_at))
        if type(self.text) is not str or not self.text:
            raise ValueError("invalid public experience text")
        for reference in (
            self.message_id,
            self.parent_message_id,
            self.conversation_id,
        ):
            if reference is not None:
                require_reference("public message reference", reference)
        if (self.message_id is None) != (self.kind == "no_action"):
            raise ValueError("invalid public experience identity")
        value = asdict(self)
        if len(serialized(value)) > MAX_RECORD_BYTES:
            raise ValueError("public experience record too large")
        return value


@dataclass(frozen=True, slots=True)
class PublicExperience:
    records: tuple[PublicExperienceRecord, ...] = ()

    def value(self):
        if (
            type(self.records) is not tuple
            or len(self.records) > MAX_RECORDS
            or any(type(r) is not PublicExperienceRecord for r in self.records)
        ):
            raise ValueError("invalid public experience records")
        value = {
            "version": EXPERIENCE_VERSION,
            "records": [r.value() for r in self.records],
        }
        if len(serialized(value)) > MAX_EXPERIENCE_BYTES:
            raise ValueError("public experience too large")
        return value

    @classmethod
    def parse(cls, value):
        if (
            type(value) is not dict
            or set(value) != {"version", "records"}
            or value["version"] != EXPERIENCE_VERSION
            or type(value["records"]) is not list
            or any(type(r) is not dict for r in value["records"])
        ):
            raise ValueError("invalid public experience")
        try:
            result = cls(tuple(PublicExperienceRecord(**r) for r in value["records"]))
            result.value()
            return result
        except TypeError as error:
            raise ValueError("invalid public experience") from error

    @property
    def digest(self):
        return hashlib.sha256(serialized(self.value())).hexdigest()


def inference_context_digest(context, experience):
    """A new domain; the legacy research/mission context digest stays unchanged."""
    return hashlib.sha256(
        serialized(
            {
                "version": EXPERIENCE_VERSION,
                "context": context,
                "experience": experience.value(),
            }
        )
    ).hexdigest()
