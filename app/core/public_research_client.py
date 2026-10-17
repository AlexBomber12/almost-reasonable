"""Secretless, one-shot client for the bounded public research worker."""

from pathlib import Path
import os
import socket
import stat
import time

from app.trusted.openai_worker_protocol import (
    WorkerProtocolError,
    receive_frame,
    send_frame,
)
from app.trusted.openai_worker_server import MAX_LINUX_UID, _peer_uid
from app.trusted.public_research import (
    PublicResearchRequest,
    decode_public_response,
    encode_public_request,
)


class PublicResearchClient:
    """Authenticate the worker before any request byte leaves the application."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        expected_worker_uid: int,
        deadline_seconds: float = 80.0,
        io_timeout_seconds: float = 5.0,
    ):
        path = Path(socket_path)
        if (
            not path.is_absolute()
            or len(os.fsencode(path)) > 107
            or type(expected_worker_uid) is not int
            or not 0 <= expected_worker_uid <= MAX_LINUX_UID
            or type(deadline_seconds) not in (int, float)
            or not 75 <= deadline_seconds <= 90
            or type(io_timeout_seconds) not in (int, float)
            or not 0 < io_timeout_seconds <= 10
        ):
            raise ValueError("invalid public research client configuration")
        self._path = path
        self._expected_worker_uid = expected_worker_uid
        self._deadline_seconds = deadline_seconds
        self._io_timeout_seconds = io_timeout_seconds

    def execute(self, request: PublicResearchRequest) -> dict[str, object]:
        # Validate locally, but delay encoding until the authenticated connection.
        encode_public_request(request)
        parent = self._path.parent.lstat()
        endpoint = self._path.lstat()
        if (
            self._path.parent.resolve(strict=True) != self._path.parent
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_mode & 0o022
            or not stat.S_ISSOCK(endpoint.st_mode)
            or endpoint.st_uid != self._expected_worker_uid
            or endpoint.st_mode & 0o002
        ):
            raise WorkerProtocolError("unsafe public research endpoint")
        deadline = time.monotonic() + self._deadline_seconds
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self._io_timeout_seconds)
            connection.connect(str(self._path))
            if _peer_uid(connection) != self._expected_worker_uid:
                raise WorkerProtocolError("unexpected public research peer")
            send_frame(
                connection,
                encode_public_request(request),
                deadline=deadline,
                io_timeout=self._io_timeout_seconds,
            )
            connection.shutdown(socket.SHUT_WR)
            payload = receive_frame(
                connection,
                deadline=deadline,
                io_timeout=self._io_timeout_seconds,
                wait_for_first_byte=True,
            )
        return decode_public_response(payload)
