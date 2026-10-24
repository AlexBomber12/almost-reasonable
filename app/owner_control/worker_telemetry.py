"""Secretless owner client for the fixed worker status operation."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
import socket
import stat
import struct
from time import monotonic

from app.worker_telemetry.protocol import (
    MAX_RESPONSE,
    REQUEST,
    decode,
    unavailable,
    DIAGNOSTICS_REQUEST,
    MAX_DIAGNOSTICS_RESPONSE,
    decode_diagnostics,
    diagnostics_unavailable,
)


class WorkerTelemetryClient:
    def __init__(self, socket_path: Path, expected_uid: int):
        if (
            not isinstance(socket_path, Path)
            or not socket_path.is_absolute()
            or len(str(socket_path).encode()) > 107
            or type(expected_uid) is not int
            or not 0 <= expected_uid <= 2**32 - 2
            or not hasattr(socket, "SO_PEERCRED")
        ):
            raise ValueError("invalid worker telemetry endpoint")
        self.socket_path = socket_path
        self.expected_uid = expected_uid

    def read(self):
        return self._read(REQUEST, MAX_RESPONSE, decode, unavailable)

    def read_diagnostics(self):
        return self._read(
            DIAGNOSTICS_REQUEST,
            MAX_DIAGNOSTICS_RESPONSE,
            decode_diagnostics,
            diagnostics_unavailable,
        )

    def _read(self, request, maximum, decoder, fallback):
        try:
            entry = self.socket_path.lstat()
            if (
                not stat.S_ISSOCK(entry.st_mode)
                or entry.st_uid != self.expected_uid
                or stat.S_IMODE(entry.st_mode) not in (0o600, 0o660)
            ):
                return fallback()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(1.0)
                connection.connect(str(self.socket_path))
                credentials = connection.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII")
                )
                if struct.unpack("iII", credentials)[1] != self.expected_uid:
                    return fallback()
                connection.sendall(request)
                connection.shutdown(socket.SHUT_WR)
                deadline = monotonic() + 1.0
                size = struct.unpack("!I", _read_exact(connection, 4, deadline))[0]
                if size > maximum:
                    return fallback()
                result = decoder(_read_exact(connection, size, deadline))
                if request == REQUEST:
                    collected = datetime.fromisoformat(result["collected_at"])
                    now = datetime.now(UTC)
                    if collected.tzinfo is None or not now - timedelta(
                        seconds=5
                    ) <= collected <= now + timedelta(seconds=5):
                        return fallback()
                return result
        except (
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            ValueError,
            struct.error,
            OverflowError,
        ):
            return fallback()


def _read_exact(connection, size, deadline):
    data = bytearray()
    while len(data) < size:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("telemetry read deadline exceeded")
        connection.settimeout(remaining)
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ValueError("telemetry connection closed")
        data.extend(chunk)
    return bytes(data)
