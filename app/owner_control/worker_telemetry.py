"""Secretless owner client for the fixed worker status operation."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
import socket
import stat
import struct

from app.worker_telemetry.protocol import MAX_RESPONSE, REQUEST, decode, unavailable


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
        try:
            entry = self.socket_path.lstat()
            if (
                not stat.S_ISSOCK(entry.st_mode)
                or entry.st_uid != self.expected_uid
                or stat.S_IMODE(entry.st_mode) not in (0o600, 0o660)
            ):
                return unavailable()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(1.0)
                connection.connect(str(self.socket_path))
                credentials = connection.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII")
                )
                if struct.unpack("iII", credentials)[1] != self.expected_uid:
                    return unavailable()
                connection.sendall(REQUEST)
                connection.shutdown(socket.SHUT_WR)
                size = struct.unpack("!I", _read_exact(connection, 4))[0]
                if size > MAX_RESPONSE:
                    return unavailable()
                result = decode(_read_exact(connection, size))
                collected = datetime.fromisoformat(result["collected_at"])
                now = datetime.now(UTC)
                if collected.tzinfo is None or not now - timedelta(
                    seconds=5
                ) <= collected <= now + timedelta(seconds=5):
                    return unavailable()
                return result
        except (
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            ValueError,
            struct.error,
            OverflowError,
        ):
            return unavailable()


def _read_exact(connection, size):
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ValueError("telemetry connection closed")
        data.extend(chunk)
    return bytes(data)
