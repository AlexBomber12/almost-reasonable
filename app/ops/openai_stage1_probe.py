"""Secretless one-shot planner probe for the fixed Stage 1 smoke request."""

import argparse
from pathlib import Path
import sys

from app.core.openai_stage1_contract import (
    STAGE1_CORRELATION_ID,
    STAGE1_OPERATION_ID,
    build_stage1_synthetic_request,
)
from app.core.openai_worker_client import OpenAIWorkerClient


def run_probe(socket_path: Path) -> bool:
    result = OpenAIWorkerClient(socket_path).infer(
        build_stage1_synthetic_request(),
        operation_id=STAGE1_OPERATION_ID,
        correlation_id=STAGE1_CORRELATION_ID,
    )
    print(
        "Stage 1 sanitized outcome: "
        f"outcome={result.outcome} attempts={result.attempts}"
    )
    return (
        result.outcome == "completed"
        and result.attempts == 1
        and result.validation is not None
        and result.validation.valid
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the fixed Stage 1 probe")
    parser.add_argument("--socket", required=True, type=Path)
    arguments = parser.parse_args(argv)
    if run_probe(arguments.socket):
        return 0
    print("Stage 1 probe did not reach a paid-attempt outcome.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
