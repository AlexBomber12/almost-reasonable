"""Opt-in synthetic acceptance worker: record actual reply provider projections."""

import json
import os
from pathlib import Path
import stat

from app.trusted import private_pilot_worker
from app.trusted.openai_transport import OpenAIResponsesTransport


def main(argv=None):
    capture = Path(os.environ["PILOT_MEMORY_CAPTURE"])
    directory = capture.parent.stat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid():
        raise ValueError("unsafe synthetic capture directory")
    original = OpenAIResponsesTransport.call

    def record(self, request, *, timeout_seconds):
        if request.purpose in {
            "agent",
            "memory_evolution",
            "typed_memory_evolution",
            "narrative_evolution",
            "narrative_reply",
        }:
            payload = (
                json.dumps(
                    {"purpose": request.purpose, "input": request.input_text},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            fd = os.open(
                capture,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            try:
                entry = os.fstat(fd)
                if (
                    not stat.S_ISREG(entry.st_mode)
                    or entry.st_uid != os.geteuid()
                    or stat.S_IMODE(entry.st_mode) != 0o600
                    or entry.st_nlink != 1
                ):
                    raise ValueError("unsafe synthetic capture file")
                pending = memoryview(payload)
                while pending:
                    pending = pending[os.write(fd, pending) :]
                os.fsync(fd)
            finally:
                os.close(fd)
        return original(self, request, timeout_seconds=timeout_seconds)

    OpenAIResponsesTransport.call = record
    try:
        return private_pilot_worker.main(argv)
    finally:
        OpenAIResponsesTransport.call = original


if __name__ == "__main__":
    raise SystemExit(main())
