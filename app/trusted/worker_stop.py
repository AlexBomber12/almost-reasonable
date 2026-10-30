"""Worker owned, one command AF_UNIX emergency Stop service."""

import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
from time import monotonic

from app.trusted.openai_credential_files import TrustedTransportConfigurationError
from app.trusted.private_pilot import (
    PilotAuthorityError,
    protected_ancestry,
    read_pilot_control,
    _unique,
)
from app.trusted.worker_telemetry import _peer_uid, _safe_socket_directory
from app.worker_stop_protocol import ACK, REQUEST, STOP_VERSIONS

STOP_VERSION = "private-pilot-emergency-stop-v1"


def assert_stop(path, *, stop_version=STOP_VERSION):
    """Replace only an existing, valid worker owned Stop file, then sync it."""
    if stop_version not in STOP_VERSIONS:
        raise ValueError("invalid worker Stop version")
    ancestry = protected_ancestry(path)
    original = path.lstat()
    if (
        not stat.S_ISREG(original.st_mode)
        or original.st_uid != os.geteuid()
        or stat.S_IMODE(original.st_mode) != 0o400
        or original.st_nlink != 1
    ):
        raise ValueError("unsafe stop file")
    value = json.loads(
        read_pilot_control(path, maximum_bytes=1024), object_pairs_hook=_unique
    )
    if (
        type(value) is not dict
        or set(value) != {"version", "stop"}
        or value["version"] != stop_version
        or type(value["stop"]) is not bool
    ):
        raise ValueError("invalid stop file")
    if value["stop"]:
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                current = os.fstat(descriptor)
                if (current.st_dev, current.st_ino) != (
                    original.st_dev,
                    original.st_ino,
                ) or protected_ancestry(path) != ancestry:
                    raise ValueError("stop file changed")
                os.fsync(descriptor)
                os.fsync(parent)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)
        return
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".stop-" + secrets.token_hex(16)
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o400,
            dir_fd=parent,
        )
        try:
            os.fchmod(descriptor, 0o400)
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                output.write(
                    json.dumps(
                        {"version": stop_version, "stop": True}, separators=(",", ":")
                    ).encode()
                )
                output.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (
            original.st_dev,
            original.st_ino,
        ) or protected_ancestry(path) != ancestry:
            raise ValueError("stop file changed")
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


class WorkerStop:
    def __init__(self, path, stop_version):
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or stop_version not in STOP_VERSIONS
        ):
            raise ValueError("invalid worker Stop reader")
        self.path, self.stop_version = path, stop_version

    def active(self):
        from app.trusted.worker_telemetry import _stop

        status = _stop(self.path, self.stop_version)
        return not status["valid"] or status["active"]


class WorkerStopServer:
    def __init__(
        self,
        socket_path: Path,
        stop_path: Path,
        owner_uid: int,
        *,
        socket_group_gid=None,
        stop_version=STOP_VERSION,
    ):
        if stop_version not in STOP_VERSIONS:
            raise ValueError("invalid worker Stop version")
        self.stop_version = stop_version
        if (
            not hasattr(socket, "SO_PEERCRED")
            or not isinstance(socket_path, Path)
            or not isinstance(stop_path, Path)
            or not socket_path.is_absolute()
            or not stop_path.is_absolute()
            or type(owner_uid) is not int
            or not 0 <= owner_uid <= 2**32 - 2
        ):
            raise ValueError("invalid worker stop configuration")
        self.socket_path, self.stop_path = socket_path, stop_path
        self.owner_uid, self.socket_group_gid = owner_uid, socket_group_gid
        self.listener = None
        self.identity = None

    def start(self):
        _safe_socket_directory(self.socket_path, self.socket_group_gid)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            listener.bind(str(self.socket_path))
        except OSError:
            listener.close()
            raise RuntimeError("worker stop socket creation failed") from None
        finally:
            os.umask(old)
        try:
            entry = self.socket_path.lstat()
            self.identity = (entry.st_dev, entry.st_ino)
            if self.socket_group_gid is not None:
                os.chown(
                    self.socket_path, -1, self.socket_group_gid, follow_symlinks=False
                )
                os.chmod(self.socket_path, 0o660, follow_symlinks=False)
            listener.listen(8)
            listener.settimeout(0.25)
            self.listener = listener
        except Exception:
            listener.close()
            self.close()
            raise

    def serve_once(self):
        if self.listener is None:
            raise RuntimeError("worker stop server is not started")
        try:
            connection, _ = self.listener.accept()
        except TimeoutError:
            return False
        with connection:
            if _peer_uid(connection) != self.owner_uid:
                return True
            deadline = monotonic() + 0.5
            try:
                request = bytearray()
                while len(request) < len(REQUEST) + 1:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return True
                    connection.settimeout(remaining)
                    chunk = connection.recv(len(REQUEST) + 1 - len(request))
                    if not chunk:
                        break
                    request.extend(chunk)
                if bytes(request) != REQUEST:
                    return True
                assert_stop(self.stop_path, stop_version=self.stop_version)
                connection.settimeout(max(0.001, deadline - monotonic()))
                connection.sendall(ACK)
            except (
                OSError,
                TimeoutError,
                ValueError,
                PilotAuthorityError,
                TrustedTransportConfigurationError,
            ):
                pass
        return True

    def serve(self, *, should_stop):
        while not should_stop():
            self.serve_once()

    def close(self):
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        if self.identity is not None:
            try:
                entry = self.socket_path.lstat()
                if (entry.st_dev, entry.st_ino) == self.identity:
                    self.socket_path.unlink()
            except FileNotFoundError:
                pass
            self.identity = None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Worker emergency Stop control")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--allow-uid", type=int, required=True)
    parser.add_argument("--socket-group-gid", type=int)
    parser.add_argument(
        "--stop-version", choices=sorted(STOP_VERSIONS), default=STOP_VERSION
    )
    args = parser.parse_args(argv)
    server = WorkerStopServer(
        args.socket,
        args.stop_file,
        args.allow_uid,
        socket_group_gid=args.socket_group_gid,
        stop_version=args.stop_version,
    )
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    previous_term = signal.signal(signal.SIGTERM, stop)
    previous_int = signal.signal(signal.SIGINT, stop)
    try:
        server.start()
        server.serve(should_stop=lambda: stopping)
    finally:
        server.close()
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
