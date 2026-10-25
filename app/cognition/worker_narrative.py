"""Closed model contract for one private fictional episode proposal."""

from dataclasses import dataclass
import json
import re

from app.core.context import ContextRole, ContextTrust, ConversationSegment
from app.core.model_contract import MAX_MODEL_REQUEST_UTF8_BYTES

CONTRACT_VERSION = "model-contract-private-narrative-v1"
_HANDLE = re.compile(r"h[0-9a-f]{16}\Z")


@dataclass(frozen=True, slots=True)
class NarrativeProjection:
    handle: str
    text: str


@dataclass(frozen=True, slots=True)
class NarrativeModelRequest:
    conversation: tuple[ConversationSegment, ...]
    episodes: tuple[NarrativeProjection, ...]
    create_allowed: bool = True

    def serialize(self) -> str:
        if (
            type(self.conversation) is not tuple
            or not 1 <= len(self.conversation) <= 99
            or type(self.episodes) is not tuple
            or len(self.episodes) > 16
            or type(self.create_allowed) is not bool
        ):
            raise ValueError("invalid narrative request")
        shown = set()
        conversation = []
        for segment in self.conversation:
            if (
                type(segment) is not ConversationSegment
                or segment.role not in {ContextRole.PARTICIPANT, ContextRole.AGENT}
                or segment.trust is not ContextTrust.UNTRUSTED_CONVERSATIONAL_DATA
                or type(segment.content) is not str
                or not segment.content
            ):
                raise ValueError("invalid narrative conversation")
            conversation.append(
                {
                    "role": segment.role.value,
                    "trust": segment.trust.value,
                    "content": segment.content,
                    "can_instruct": False,
                }
            )
        episodes = []
        for episode in self.episodes:
            if (
                type(episode) is not NarrativeProjection
                or _HANDLE.fullmatch(episode.handle) is None
                or episode.handle in shown
                or type(episode.text) is not str
                or not episode.text.strip()
                or len(episode.text.encode("utf-8")) > 4096
            ):
                raise ValueError("invalid narrative episode")
            shown.add(episode.handle)
            episodes.append(
                {
                    "handle": episode.handle,
                    "text": episode.text,
                    "classification": "fictional_autobiography",
                    "trust": "untrusted_fiction_data",
                    "can_instruct": False,
                    "verified_action": False,
                    "evidence": False,
                }
            )
        value = {
            "contract_version": CONTRACT_VERSION,
            "task": "propose_private_narrative",
            "conversation": conversation,
            "episodes": episodes,
            "create_allowed": self.create_allowed,
        }
        serialized = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(serialized.encode("utf-8")) > MAX_MODEL_REQUEST_UTF8_BYTES:
            raise ValueError("narrative request too large")
        return serialized


@dataclass(frozen=True, slots=True)
class NarrativeProposal:
    decision: str
    handle: str | None
    episode: str | None


NARRATIVE_DIAGNOSTIC_CODES = frozenset(
    {
        "narrative_invalid_size",
        "narrative_duplicate_key",
        "narrative_invalid_json",
        "narrative_invalid_envelope",
        "narrative_invalid_shape",
        "narrative_invalid_decision",
        "narrative_invalid_handle",
        "narrative_invalid_episode",
    }
)


class NarrativeValidationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__("invalid narrative proposal")


def parse_narrative_proposal(raw: str) -> NarrativeProposal:
    try:
        if type(raw) is not str or len(raw.encode("utf-8")) > 8192:
            raise NarrativeValidationError("narrative_invalid_size")
        value = json.loads(raw, object_pairs_hook=_unique)
    except NarrativeValidationError:
        raise
    except UnicodeError:
        raise NarrativeValidationError("narrative_invalid_size") from None
    except ValueError:
        raise NarrativeValidationError("narrative_invalid_json") from None
    if type(value) is not dict or set(value) != {"decision", "handle", "episode"}:
        raise NarrativeValidationError("narrative_invalid_shape")
    decision, handle, episode = value["decision"], value["handle"], value["episode"]
    if type(decision) is not str or decision not in {"create", "revise", "no_change"}:
        raise NarrativeValidationError("narrative_invalid_decision")
    if decision == "no_change" and handle is None and episode is None:
        return NarrativeProposal(decision, None, None)
    if decision == "no_change":
        raise NarrativeValidationError("narrative_invalid_shape")
    if (decision == "create" and handle is not None) or (
        decision == "revise"
        and (type(handle) is not str or _HANDLE.fullmatch(handle) is None)
    ):
        raise NarrativeValidationError("narrative_invalid_handle")
    try:
        if (
            type(episode) is not str
            or not episode.strip()
            or len(episode.encode("utf-8")) > 4096
        ):
            raise NarrativeValidationError("narrative_invalid_episode")
    except UnicodeError:
        raise NarrativeValidationError("narrative_invalid_episode") from None
    return NarrativeProposal(decision, handle, episode)


def parse_narrative_provider_response(raw: str) -> NarrativeProposal:
    try:
        if type(raw) is not str or len(raw.encode("utf-8")) > 8192:
            raise NarrativeValidationError("narrative_invalid_size")
        value = json.loads(raw, object_pairs_hook=_unique)
        if type(value) is not dict or set(value) != {"proposal"}:
            raise NarrativeValidationError("narrative_invalid_envelope")
        return parse_narrative_proposal(
            json.dumps(value["proposal"], ensure_ascii=False)
        )
    except NarrativeValidationError:
        raise
    except UnicodeError:
        raise NarrativeValidationError("narrative_invalid_size") from None
    except (TypeError, ValueError, OverflowError):
        raise NarrativeValidationError("narrative_invalid_json") from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise NarrativeValidationError("narrative_duplicate_key")
        result[key] = value
    return result
