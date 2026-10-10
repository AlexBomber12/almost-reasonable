"""Minimal secretless application client for the local OpenAI worker."""

from dataclasses import dataclass, field
from pathlib import Path
import socket
import time

from app.core.agent_proposals import ProposalValidation
from app.core.model_contract import ModelRequest
from app.core.provider_contract import require_identifier
from app.trusted.openai_worker_protocol import (
    InferenceRequest,
    WorkerProtocolError,
    decode_response,
    encode_request,
    receive_frame,
    send_frame,
    validate_timing,
)


CLIENT_COMMUNICATION_FAILURE = "communication_failure"
CLIENT_OUTCOME_UNCERTAIN = "outcome_uncertain"
MAX_UNIX_SOCKET_PATH_UTF8_BYTES = 107


@dataclass(frozen=True, slots=True)
class ClientResult:
    operation_id: str
    correlation_id: str
    outcome: str
    attempts: int
    safe_russian_message: str | None
    validation: ProposalValidation | None = field(default=None, repr=False)


class OpenAIWorkerClient:
    """Perform one non-retrying inference exchange over an AF_UNIX socket."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        connection_lifetime_seconds: float = 320.0,
        io_timeout_seconds: float = 5.0,
    ):
        self._socket_path = _validate_socket_path(socket_path)
        validate_timing(connection_lifetime_seconds, io_timeout_seconds)
        self._connection_lifetime = connection_lifetime_seconds
        self._io_timeout = io_timeout_seconds

    def infer(
        self,
        request: ModelRequest,
        *,
        operation_id: str,
        correlation_id: str,
    ) -> ClientResult:
        require_identifier("operation id", operation_id)
        require_identifier("correlation id", correlation_id)
        payload = encode_request(
            InferenceRequest(operation_id, correlation_id, request)
        )
        deadline = time.monotonic() + self._connection_lifetime
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError:
            return ClientResult(
                operation_id,
                correlation_id,
                CLIENT_COMMUNICATION_FAILURE,
                0,
                None,
            )
        transmitted = False
        try:
            connection.settimeout(min(self._io_timeout, self._connection_lifetime))
            connection.connect(self._socket_path)
            transmitted = True
            send_frame(
                connection,
                payload,
                deadline=deadline,
                io_timeout=self._io_timeout,
            )
            connection.shutdown(socket.SHUT_WR)
            response = decode_response(
                receive_frame(
                    connection,
                    deadline=deadline,
                    io_timeout=self._io_timeout,
                    wait_for_first_byte=True,
                )
            )
        except (OSError, WorkerProtocolError):
            outcome = (
                CLIENT_OUTCOME_UNCERTAIN
                if transmitted
                else CLIENT_COMMUNICATION_FAILURE
            )
            return ClientResult(operation_id, correlation_id, outcome, 0, None)
        finally:
            connection.close()
        return ClientResult(
            operation_id,
            correlation_id,
            response.outcome,
            response.attempts,
            response.safe_russian_message,
            response.validation,
        )


def _validate_socket_path(value: str | Path) -> str:
    if not isinstance(value, (str, Path)):
        raise TypeError("socket_path must be a string or Path")
    path = str(value)
    try:
        size = len(path.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("socket path is invalid") from None
    if not path or "\x00" in path or size > MAX_UNIX_SOCKET_PATH_UTF8_BYTES:
        raise ValueError("socket path is invalid")
    if not Path(path).is_absolute():
        raise ValueError("socket path must be absolute")
    return path
