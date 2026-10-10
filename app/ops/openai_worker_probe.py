"""One-shot secretless probe for the disabled Stage 0 OpenAI worker."""

import argparse
from pathlib import Path
import sys

from app.core.context import (
    MAX_CONTEXT_UTF8_BYTES,
    ContextRole,
    ContextTrust,
    ConversationSegment,
    PrivateConversationContext,
    context_utf8_size,
)
from app.core.core_self import initial_core_self
from app.core.model_contract import ModelRequest, build_model_request
from app.core.openai_worker_client import OpenAIWorkerClient


PROBE_OPERATION_ID = "stage0-offline-probe-v1"
PROBE_CORRELATION_ID = "stage0-offline-probe-correlation-v1"
_SYNTHETIC_CONTENT = "Синтетическая проверка изоляции Stage 0."
_SUCCESS = "Stage 0 probe succeeded: paid inference is disabled."
_FAILURE = "Stage 0 probe failed: disabled outcome was not verified."


def build_synthetic_request() -> ModelRequest:
    """Build fixed non-participant input through the approved request builder."""

    core_self = initial_core_self()
    history = (
        ConversationSegment(
            ContextRole.PARTICIPANT,
            ContextTrust.UNTRUSTED_CONVERSATIONAL_DATA,
            _SYNTHETIC_CONTENT,
        ),
    )
    context = PrivateConversationContext(
        core_self=core_self,
        history=history,
        history_omitted=False,
        used_utf8_bytes=context_utf8_size(core_self, history),
        max_utf8_bytes=MAX_CONTEXT_UTF8_BYTES,
    )
    return build_model_request(context)


def run_probe(socket_path: Path) -> bool:
    """Return whether one exchange proves the expected inert Stage 0 outcome."""

    result = OpenAIWorkerClient(socket_path).infer(
        build_synthetic_request(),
        operation_id=PROBE_OPERATION_ID,
        correlation_id=PROBE_CORRELATION_ID,
    )
    return (
        result.outcome == "disabled"
        and result.attempts == 0
        and result.validation is None
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe the offline Stage 0 worker")
    parser.add_argument("--socket", required=True, type=Path)
    arguments = parser.parse_args(argv)
    if run_probe(arguments.socket):
        print(_SUCCESS)
        return 0
    print(_FAILURE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
