"""Bounded owner client for the worker's fixed Stop assertion."""

import os
from pathlib import Path
import socket
import stat
import struct
from time import monotonic

from app.worker_stop_protocol import ACK, REQUEST


class WorkerStopClient:
    def __init__(self, socket_path: Path, expected_uid: int):
        if (
            not isinstance(socket_path, Path)
            or not socket_path.is_absolute()
            or len(os.fsencode(socket_path)) > 107
            or type(expected_uid) is not int
            or not 0 <= expected_uid <= 2**32 - 2
            or not hasattr(socket, "SO_PEERCRED")
        ):
            raise ValueError("invalid worker stop endpoint")
        self.socket_path, self.expected_uid = socket_path, expected_uid

    def assert_stop(self):
        """Only an intact acknowledgement is confirmation; failures remain uncertain."""
        sent = False
        try:
            entry = self.socket_path.lstat()
            if (
                not stat.S_ISSOCK(entry.st_mode)
                or entry.st_uid != self.expected_uid
                or stat.S_IMODE(entry.st_mode) not in (0o600, 0o660)
            ):
                return "unavailable"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                deadline = monotonic() + 1.0
                connection.settimeout(1.0)
                connection.connect(str(self.socket_path))
                credentials = connection.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII")
                )
                if struct.unpack("iII", credentials)[1] != self.expected_uid:
                    return "unavailable"
                connection.settimeout(max(0.001, deadline - monotonic()))
                sent = True
                connection.sendall(REQUEST)
                connection.shutdown(socket.SHUT_WR)
                result = bytearray()
                while len(result) < len(ACK) + 1:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return "uncertain"
                    connection.settimeout(remaining)
                    chunk = connection.recv(len(ACK) + 1 - len(result))
                    if not chunk:
                        break
                    result.extend(chunk)
                return "confirmed" if bytes(result) == ACK else "uncertain"
        except (OSError, TimeoutError, ValueError, struct.error):
            return "uncertain" if sent else "unavailable"
