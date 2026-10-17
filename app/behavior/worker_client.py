"""Secretless one-call public behavior model client."""

import json
from pathlib import Path
import socket
import struct
import time

from app.behavior.deliberation import parse_behavior_proposal
from app.core.public_behavior import PublicBehaviorRequest, _unique_fields
from app.trusted.openai_worker_protocol import receive_frame, send_frame


class PublicBehaviorClient:
    def __init__(self, socket_path: Path, *, expected_uid: int):
        if (
            not socket_path.is_absolute()
            or len(str(socket_path).encode()) > 107
            or type(expected_uid) is not int
            or not 0 <= expected_uid <= 2**32 - 2
        ):
            raise ValueError("invalid public worker binding")
        self.socket_path, self.expected_uid = socket_path, expected_uid

    def complete(self, request, timeout_seconds):
        operation = request["correlation_id"]
        public = PublicBehaviorRequest(
            operation,
            operation,
            request["trigger"],
            tuple(request["authorized_context"]),
            tuple(request["permitted_mutable_state"]),
            timeout_seconds,
        )
        payload = public.encode()
        deadline = time.monotonic() + timeout_seconds
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_seconds)
            connection.connect(str(self.socket_path))
            peer = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if struct.unpack("iII", peer)[1] != self.expected_uid:
                raise ValueError("public worker peer mismatch")
            send_frame(
                connection, payload, deadline=deadline, io_timeout=timeout_seconds
            )
            connection.shutdown(socket.SHUT_WR)
            raw = receive_frame(
                connection, deadline=deadline, io_timeout=timeout_seconds
            )
        try:
            value = json.loads(raw, object_pairs_hook=_unique_fields)
        except (UnicodeError, ValueError) as error:
            raise ValueError("invalid public worker result") from error
        if (
            type(value) is not dict
            or set(value) != {"operation_id", "proposal"}
            or value["operation_id"] != operation
        ):
            raise ValueError("invalid public worker result")
        if not parse_behavior_proposal(value["proposal"]).valid:
            raise ValueError("invalid public worker proposal")
        return value["proposal"]
