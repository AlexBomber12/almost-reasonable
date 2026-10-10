"""Authenticated AF_UNIX server owning the trusted OpenAI gateway."""

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import socket
import stat
import struct
from threading import Thread
from time import monotonic

from app.core.provider_contract import (
    MAX_OVERALL_TIMEOUT_SECONDS,
    default_openai_provider_config,
)
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.trusted.openai_gateway import GatewayResult, TrustedOpenAIGateway
from app.trusted.openai_transport import FakeOpenAITransport
from app.trusted.openai_worker_protocol import (
    IPC_REJECTED,
    WorkerProtocolError,
    WorkerResponse,
    decode_request,
    encode_response,
    receive_frame,
    send_frame,
    validate_timing,
)


SOCKET_MODE = 0o600
MAX_LINUX_UID = 2**32 - 2
_INVALID_REQUEST_RU = "Локальный запрос к модели отклонён."
_TIMEOUT_RU = "Сервис модели временно недоступен. Попробуйте позже."


@dataclass(frozen=True, slots=True)
class WorkerServerConfig:
    socket_path: Path
    permitted_peer_uids: frozenset[int]
    connection_lifetime_seconds: float = 320.0
    io_timeout_seconds: float = 5.0
    accept_timeout_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.socket_path, Path) or not self.socket_path.is_absolute():
            raise ValueError("socket path must be an absolute Path")
        if (
            type(self.permitted_peer_uids) is not frozenset
            or not self.permitted_peer_uids
            or any(
                type(uid) is not int or not 0 <= uid <= MAX_LINUX_UID
                for uid in self.permitted_peer_uids
            )
        ):
            raise ValueError("permitted peer UIDs are invalid")
        validate_timing(self.connection_lifetime_seconds, self.io_timeout_seconds)
        if (
            type(self.accept_timeout_seconds) not in {int, float}
            or not 0 < self.accept_timeout_seconds <= 10.0
        ):
            raise ValueError("accept timeout is invalid")


class OpenAIWorkerServer:
    """Serve a single closed operation after OS-authenticating each peer."""

    def __init__(self, config: WorkerServerConfig, gateway: TrustedOpenAIGateway):
        if type(config) is not WorkerServerConfig:
            raise TypeError("config must be a WorkerServerConfig")
        if type(gateway) is not TrustedOpenAIGateway:
            raise TypeError("gateway must be a TrustedOpenAIGateway")
        if not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("Unix peer credentials are unavailable")
        self._config = config
        self._gateway = gateway
        self._listener: socket.socket | None = None
        self._socket_identity: tuple[int, int] | None = None

    def start(self) -> None:
        if self._listener is not None:
            raise RuntimeError("worker server is already started")
        _validate_socket_location(self._config.socket_path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous_umask = os.umask(0o177)
        try:
            listener.bind(str(self._config.socket_path))
        except OSError:
            listener.close()
            raise RuntimeError("worker socket creation failed") from None
        finally:
            os.umask(previous_umask)
        try:
            socket_stat = self._config.socket_path.lstat()
            if not stat.S_ISSOCK(socket_stat.st_mode):
                raise RuntimeError("worker socket creation failed")
            self._socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
            os.chmod(self._config.socket_path, SOCKET_MODE, follow_symlinks=False)
            listener.listen(8)
            listener.settimeout(self._config.accept_timeout_seconds)
            self._listener = listener
        except (OSError, RuntimeError):
            listener.close()
            self._remove_owned_socket()
            raise RuntimeError("worker socket creation failed") from None

    def serve_once(self) -> bool:
        connection = self._accept_connection()
        if connection is None:
            return False
        self._handle_connection(connection)
        return True

    def _accept_connection(self) -> socket.socket | None:
        if self._listener is None:
            raise RuntimeError("worker server is not started")
        try:
            connection, _address = self._listener.accept()
        except TimeoutError:
            return None
        return connection

    def _handle_connection(self, connection: socket.socket) -> None:
        with connection:
            try:
                peer_uid = _peer_uid(connection)
            except WorkerProtocolError:
                return
            if peer_uid not in self._config.permitted_peer_uids:
                return
            deadline = monotonic() + self._config.connection_lifetime_seconds
            try:
                request = decode_request(
                    receive_frame(
                        connection,
                        deadline=deadline,
                        io_timeout=self._config.io_timeout_seconds,
                    )
                )
            except (TypeError, ValueError):
                response = WorkerResponse(IPC_REJECTED, 0, _INVALID_REQUEST_RU, None)
            else:
                gateway_allowance = (
                    MAX_OVERALL_TIMEOUT_SECONDS + self._config.io_timeout_seconds
                )
                if deadline - monotonic() < gateway_allowance:
                    response = WorkerResponse("timeout", 0, _TIMEOUT_RU, None)
                else:
                    try:
                        result = self._gateway.submit(
                            request.model_request,
                            correlation_id=request.correlation_id,
                            operation_id=request.operation_id,
                        )
                    except (TypeError, ValueError):
                        return
                    response = _gateway_response(result)
            try:
                send_frame(
                    connection,
                    encode_response(response),
                    deadline=deadline,
                    io_timeout=self._config.io_timeout_seconds,
                )
                connection.shutdown(socket.SHUT_WR)
            except (OSError, WorkerProtocolError):
                pass

    def serve(
        self,
        *,
        max_connections: int | None = None,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        if max_connections is not None and (
            type(max_connections) is not int or max_connections < 1
        ):
            raise ValueError("max_connections is invalid")
        if not callable(should_stop):
            raise TypeError("should_stop must be callable")
        accepted = 0
        active: Thread | None = None
        try:
            while not should_stop() and (
                max_connections is None or accepted < max_connections
            ):
                if active is not None and not active.is_alive():
                    active.join()
                    active = None
                connection = self._accept_connection()
                if connection is None:
                    continue
                accepted += 1
                if active is not None and not active.is_alive():
                    active.join()
                    active = None
                if active is not None:
                    connection.close()
                    continue
                active = Thread(target=self._handle_connection, args=(connection,))
                active.start()
        finally:
            if active is not None:
                active.join()

    def close(self) -> None:
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        self._remove_owned_socket()

    def _remove_owned_socket(self) -> None:
        identity = self._socket_identity
        self._socket_identity = None
        if identity is None:
            return
        try:
            current = self._config.socket_path.lstat()
            if (
                stat.S_ISSOCK(current.st_mode)
                and (current.st_dev, current.st_ino) == identity
            ):
                self._config.socket_path.unlink()
        except FileNotFoundError:
            pass


def build_offline_worker(
    socket_path: Path, database_path: Path, permitted_peer_uids: frozenset[int]
) -> OpenAIWorkerServer:
    """Build the executable Stage 0 harness without loading provider credentials."""

    store = ProviderStateStore(EventStore(database_path))
    gateway = TrustedOpenAIGateway(
        store=store,
        transport=FakeOpenAITransport([]),
        config=default_openai_provider_config(),
    )
    return OpenAIWorkerServer(
        WorkerServerConfig(socket_path, permitted_peer_uids), gateway
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the offline OpenAI worker")
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--allow-uid", action="append", required=True, type=int)
    parser.add_argument("--max-connections", type=int)
    arguments = parser.parse_args(argv)
    server = build_offline_worker(
        arguments.socket, arguments.database, frozenset(arguments.allow_uid)
    )
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    try:
        server.start()
        server.serve(
            max_connections=arguments.max_connections,
            should_stop=lambda: stopping,
        )
    finally:
        server.close()
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return 0


def _gateway_response(result: GatewayResult) -> WorkerResponse:
    validation = (
        result.validation
        if result.validation is not None and result.validation.valid
        else None
    )
    return WorkerResponse(
        result.outcome.value,
        result.attempts,
        result.safe_russian_message,
        validation,
    )


def _peer_uid(connection: socket.socket) -> int:
    try:
        credentials = connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII")
        )
        _pid, uid, _gid = struct.unpack("iII", credentials)
    except (OSError, struct.error):
        raise WorkerProtocolError("peer authentication failed") from None
    return uid


def _validate_socket_location(path: Path) -> None:
    if len(str(path).encode("utf-8")) > 107:
        raise ValueError("socket path is too long")
    parent = path.parent
    try:
        parent_stat = parent.lstat()
        if parent.resolve(strict=True) != parent:
            raise ValueError("socket directory uses a symbolic link")
    except OSError:
        raise ValueError("socket directory is invalid") from None
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or parent_stat.st_mode & 0o022
    ):
        raise ValueError("socket directory is unsafe")
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise ValueError("socket path already exists")


if __name__ == "__main__":
    raise SystemExit(main())
