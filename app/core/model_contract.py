"""Explicitly allowlisted, bounded, secretless model-facing request data."""

from dataclasses import dataclass
from enum import StrEnum
import json

from .agent_proposals import AgentDecision
from .context import (
    MAX_CONTEXT_HISTORY_EVENTS,
    MAX_CONTEXT_UTF8_BYTES,
    ContextRole,
    ContextTrust,
    ConversationSegment,
    PrivateConversationContext,
    context_utf8_size,
)
from .core_self import CoreSelf, initial_core_self


MODEL_CONTRACT_VERSION = "model-contract-v1"
MAX_MODEL_REQUEST_UTF8_BYTES = MAX_CONTEXT_UTF8_BYTES + 32 * 1024


class ModelTask(StrEnum):
    PRIVATE_CONVERSATION = "private_conversation"


MODEL_ALLOWED_DECISIONS = (
    AgentDecision.NO_ACTION,
    AgentDecision.REPLY,
    AgentDecision.RESEARCH,
    AgentDecision.PROPOSE_PUBLICATION,
)


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """Purpose-specific model input with no trusted operational fields."""

    contract_version: str
    task: ModelTask
    core_self_version: str
    core_self_identity: str
    core_self_principles: tuple[str, ...]
    conversation: tuple[ConversationSegment, ...]
    history_omitted: bool
    allowed_decisions: tuple[AgentDecision, ...]

    def to_structured_data(self) -> dict[str, object]:
        """Build the model payload through field-by-field allowlisting."""

        _validate_request(self)
        return {
            "contract_version": self.contract_version,
            "task": self.task.value,
            "core_self": {
                "version": self.core_self_version,
                "identity": self.core_self_identity,
                "principles": list(self.core_self_principles),
            },
            "conversation": [
                {
                    "role": segment.role.value,
                    "trust": segment.trust.value,
                    "content": segment.content,
                }
                for segment in self.conversation
            ],
            "history_omitted": self.history_omitted,
            "allowed_decisions": [item.value for item in self.allowed_decisions],
        }


def build_model_request(context: PrivateConversationContext) -> ModelRequest:
    """Convert the existing privacy-scoped context without copying trusted data."""

    _validate_context(context)
    core_self = context.core_self
    selected: tuple[ConversationSegment, ...] = ()
    history_omitted = context.history_omitted
    for segment in reversed(context.history):
        proposed = (segment, *selected)
        candidate = _new_request(core_self, proposed, history_omitted)
        if _serialized_utf8_size(candidate) <= MAX_MODEL_REQUEST_UTF8_BYTES:
            selected = proposed
        else:
            history_omitted = True
    return _new_request(core_self, selected, history_omitted)


def _new_request(
    core_self: CoreSelf,
    conversation: tuple[ConversationSegment, ...],
    history_omitted: bool,
) -> ModelRequest:
    return ModelRequest(
        contract_version=MODEL_CONTRACT_VERSION,
        task=ModelTask.PRIVATE_CONVERSATION,
        core_self_version=core_self.version,
        core_self_identity=core_self.identity,
        core_self_principles=core_self.principles,
        conversation=conversation,
        history_omitted=history_omitted,
        allowed_decisions=MODEL_ALLOWED_DECISIONS,
    )


def serialize_model_request(request: ModelRequest) -> str:
    """Serialize deterministically without reflecting over application objects."""

    if type(request) is not ModelRequest:
        raise TypeError("request must be a ModelRequest")
    serialized = _serialize_unchecked(request)
    if len(serialized.encode("utf-8")) > MAX_MODEL_REQUEST_UTF8_BYTES:
        raise ValueError("serialized model request exceeds its byte bound")
    return serialized


def _serialize_unchecked(request: ModelRequest) -> str:
    return json.dumps(
        request.to_structured_data(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _serialized_utf8_size(request: ModelRequest) -> int:
    return len(_serialize_unchecked(request).encode("utf-8"))


def _validate_request(request: ModelRequest) -> None:
    core_self = initial_core_self()
    fixed_fields = (
        request.contract_version,
        request.task,
        request.core_self_version,
        request.core_self_identity,
        request.core_self_principles,
        request.allowed_decisions,
    )
    approved_fields = (
        MODEL_CONTRACT_VERSION,
        ModelTask.PRIVATE_CONVERSATION,
        core_self.version,
        core_self.identity,
        core_self.principles,
        MODEL_ALLOWED_DECISIONS,
    )
    if fixed_fields != approved_fields:
        raise ValueError("model request contains non-approved fixed fields")
    if (
        type(request.conversation) is not tuple
        or len(request.conversation) > MAX_CONTEXT_HISTORY_EVENTS
    ):
        raise ValueError("model request conversation is malformed")
    for segment in request.conversation:
        if type(segment) is not ConversationSegment:
            raise ValueError("model request conversation is malformed")
        if (
            segment.role is not ContextRole.PARTICIPANT
            or segment.trust is not ContextTrust.UNTRUSTED_CONVERSATIONAL_DATA
            or not isinstance(segment.content, str)
            or not segment.content
        ):
            raise ValueError("model request segment is malformed")
        try:
            segment.content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("model request segment is not valid UTF-8") from error
    if type(request.history_omitted) is not bool:
        raise ValueError("model request omission flag is malformed")
    if context_utf8_size(core_self, request.conversation) > MAX_CONTEXT_UTF8_BYTES:
        raise ValueError("model request conversation exceeds its raw byte bound")


def _validate_context(context: PrivateConversationContext) -> None:
    if not isinstance(context, PrivateConversationContext):
        raise TypeError("context must be a PrivateConversationContext")
    if context.core_self != initial_core_self():
        raise ValueError("context must use the approved Core Self")
    if (
        type(context.history) is not tuple
        or len(context.history) > MAX_CONTEXT_HISTORY_EVENTS
    ):
        raise ValueError("context history is malformed")
    for segment in context.history:
        if not isinstance(segment, ConversationSegment):
            raise ValueError("context history is malformed")
        if (
            segment.role is not ContextRole.PARTICIPANT
            or segment.trust is not ContextTrust.UNTRUSTED_CONVERSATIONAL_DATA
            or not isinstance(segment.content, str)
            or not segment.content
        ):
            raise ValueError("context segment is malformed")
        try:
            segment.content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("context segment is not valid UTF-8") from error
    if type(context.history_omitted) is not bool:
        raise ValueError("history_omitted must be a boolean")
    if (
        type(context.max_utf8_bytes) is not int
        or not 1 <= context.max_utf8_bytes <= MAX_CONTEXT_UTF8_BYTES
    ):
        raise ValueError("context byte bound is malformed")
    expected_size = context_utf8_size(context.core_self, context.history)
    if (
        type(context.used_utf8_bytes) is not int
        or context.used_utf8_bytes != expected_size
        or expected_size > context.max_utf8_bytes
    ):
        raise ValueError("context byte accounting is inconsistent")
