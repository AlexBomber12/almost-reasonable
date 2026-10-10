"""Fixed secretless contract for the single Stage 1 synthetic smoke request."""

from hashlib import sha256

from .context import (
    MAX_CONTEXT_UTF8_BYTES,
    ContextRole,
    ContextTrust,
    ConversationSegment,
    PrivateConversationContext,
    context_utf8_size,
)
from .core_self import initial_core_self
from .model_contract import ModelRequest, build_model_request, serialize_model_request


STAGE1_OPERATION_ID = "stage1-openai-smoke-v1"
STAGE1_CORRELATION_ID = "stage1-openai-smoke-correlation-v1"
STAGE1_MODEL = "gpt-6-luna"
STAGE1_PROVIDER_CONFIG_VERSION = "openai-stage1-smoke-config-v1"
STAGE1_SYNTHETIC_CONTENT = (
    "Синтетическая проверка совместимости Stage 1. "
    "Выбери одно безопасное структурированное решение."
)


def build_stage1_synthetic_request() -> ModelRequest:
    """Build the sole request eligible for the Stage 1 smoke capability."""

    core_self = initial_core_self()
    history = (
        ConversationSegment(
            ContextRole.PARTICIPANT,
            ContextTrust.UNTRUSTED_CONVERSATIONAL_DATA,
            STAGE1_SYNTHETIC_CONTENT,
        ),
    )
    return build_model_request(
        PrivateConversationContext(
            core_self=core_self,
            history=history,
            history_omitted=False,
            used_utf8_bytes=context_utf8_size(core_self, history),
            max_utf8_bytes=MAX_CONTEXT_UTF8_BYTES,
        )
    )


def stage1_model_request_digest(request: ModelRequest) -> str:
    """Return the canonical lowercase SHA-256 digest used by activation."""

    return sha256(serialize_model_request(request).encode("utf-8")).hexdigest()
