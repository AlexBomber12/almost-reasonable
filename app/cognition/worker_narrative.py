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

    def serialize(self) -> str:
        if (
            type(self.conversation) is not tuple
            or not 1 <= len(self.conversation) <= 99
            or type(self.episodes) is not tuple
            or len(self.episodes) > 16
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


def parse_narrative_proposal(raw: str) -> NarrativeProposal:
    if type(raw) is not str or len(raw.encode("utf-8")) > 8192:
        raise ValueError("invalid narrative proposal")
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError):
        raise ValueError("invalid narrative proposal") from None
    if type(value) is not dict or set(value) != {"decision", "handle", "episode"}:
        raise ValueError("invalid narrative proposal")
    decision, handle, episode = value["decision"], value["handle"], value["episode"]
    if decision == "no_change" and handle is None and episode is None:
        return NarrativeProposal(decision, None, None)
    if (
        decision not in {"create", "revise"}
        or (decision == "create" and handle is not None)
        or (
            decision == "revise"
            and (type(handle) is not str or _HANDLE.fullmatch(handle) is None)
        )
        or type(episode) is not str
        or not episode.strip()
        or len(episode.encode("utf-8")) > 4096
    ):
        raise ValueError("invalid narrative proposal")
    return NarrativeProposal(decision, handle, episode)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate narrative proposal field")
        result[key] = value
    return result
