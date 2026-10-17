"""Explicit credential-holding worker for the bounded INT-22 private pilot."""

import argparse
import json
import os
from pathlib import Path
import signal
import stat

from app.cognition.conversation_context import MemoryModelRequest
from app.cognition.shared_context import SharedModelRequest
from app.core.provider_contract import (
    DeploymentStage,
    OpenAIProviderConfig,
    PROMPT_VERSION,
)
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.trusted.openai_credential_files import (
    TrustedTransportConfigurationError,
    build_worker_openai_transport,
    read_protected_worker_file,
)
from app.trusted.openai_gateway import TrustedOpenAIGateway
from app.trusted.openai_worker_protocol import InferenceRequest, WorkerResponse
from app.trusted.openai_worker_server import OpenAIWorkerServer, WorkerServerConfig
from app.trusted.private_pilot import (
    MODEL,
    PROVIDER_CONFIG_VERSION,
    PilotActivation,
    PilotAllowance,
    PilotAuthorityError,
    _unique,
    load_activation,
)


ACTIVATION_PATH = Path("/run/almost-reasonable-private-pilot/control/activation.json")
STOP_PATH = Path("/run/almost-reasonable-private-pilot/control/emergency-stop.json")
STOP_VERSION = "private-pilot-emergency-stop-v1"
_STOP_RU = "Новые запросы к модели временно остановлены."
_REJECT_RU = "Локальный запрос не соответствует разрешённой проверке."


def provider_config() -> OpenAIProviderConfig:
    return OpenAIProviderConfig(
        version=PROVIDER_CONFIG_VERSION,
        model=MODEL,
        prompt_version=PROMPT_VERSION,
        deployment_stage=DeploymentStage.STAGE_1,
        paid_calls_enabled=True,
        max_output_tokens=512,
        timeout_seconds=30.0,
        overall_timeout_seconds=40.0,
        max_attempts=1,
        initial_backoff_seconds=0.0,
        maximum_backoff_seconds=0.1,
        maximum_jitter_seconds=0.0,
    )


class PilotStop:
    """An absent, unsafe, malformed or set worker stop blocks provider use."""

    def __init__(self, path: Path = STOP_PATH):
        self.path = path

    def active(self) -> bool:
        try:
            value = json.loads(
                read_protected_worker_file(self.path, maximum_bytes=1024),
                object_pairs_hook=_unique,
            )
            return not (
                type(value) is dict
                and set(value) == {"version", "stop"}
                and value["version"] == STOP_VERSION
                and value["stop"] is False
            )
        except (TrustedTransportConfigurationError, UnicodeDecodeError, ValueError):
            return True


class PilotRequestGate:
    """Recheck live authority and commit allowance before any gateway attempt."""

    def __init__(
        self,
        activation: PilotActivation,
        allowance: PilotAllowance,
        *,
        activation_path: Path,
        stop: PilotStop,
    ):
        self.activation = activation
        self.allowance = allowance
        self.activation_path = activation_path
        self.stop = stop

    def current(self) -> bool:
        try:
            return load_activation(self.activation_path) == self.activation
        except PilotAuthorityError:
            return False

    def authorize(self, request: InferenceRequest) -> WorkerResponse | None:
        if self.stop.active():
            return WorkerResponse("emergency_stop", 0, _STOP_RU, None)
        if (
            type(request.model_request) not in {MemoryModelRequest, SharedModelRequest}
            or not self.current()
        ):
            return WorkerResponse("authorization_rejected", 0, _REJECT_RU, None)
        try:
            reserved = self.allowance.reserve(request.operation_id)
        except PilotAuthorityError:
            reserved = False
        if not reserved:
            return WorkerResponse("authorization_rejected", 0, _REJECT_RU, None)
        return None

    def paid_transport_blocked(self) -> bool:
        return self.stop.active() or not self.current()


def build_worker(
    *,
    socket_path: Path,
    provider_database: Path,
    allowance_database: Path,
    activation_path: Path = ACTIVATION_PATH,
    stop_path: Path = STOP_PATH,
    socket_group_gid: int | None = None,
) -> OpenAIWorkerServer:
    """Live entry point; only the protected credential-file factory creates SDK."""

    activation = load_activation(activation_path)
    allowance = PilotAllowance(allowance_database, activation)
    _require_provider_database(provider_database)
    gate = PilotRequestGate(
        activation,
        allowance,
        activation_path=activation_path,
        stop=PilotStop(stop_path),
    )
    transport = build_worker_openai_transport(
        project_id=activation.project_id,
        organization_id=activation.organization_id,
    )
    gateway = TrustedOpenAIGateway(
        store=ProviderStateStore(EventStore(provider_database)),
        transport=transport,
        config=provider_config(),
        emergency_stop=gate.paid_transport_blocked,
    )
    return OpenAIWorkerServer(
        WorkerServerConfig(
            socket_path,
            frozenset({activation.application_peer_uid}),
            trusted_ipc_gid=socket_group_gid,
        ),
        gateway,
        request_guard=gate.authorize,
    )


def _require_provider_database(path: Path) -> None:
    """A missing worker spend ledger cannot silently restart unsuspended."""

    try:
        parent = path.parent.lstat()
        entry = path.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
            or not stat.S_ISREG(entry.st_mode)
            or entry.st_uid != os.geteuid()
            or stat.S_IMODE(entry.st_mode) != 0o600
            or entry.st_nlink != 1
        ):
            raise PilotAuthorityError("unsafe provider state")
    except OSError:
        raise PilotAuthorityError("provider state unavailable") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the bounded private pilot worker")
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--provider-database", required=True, type=Path)
    parser.add_argument("--allowance-database", required=True, type=Path)
    parser.add_argument("--socket-group-gid", required=True, type=int)
    args = parser.parse_args(argv)
    worker = build_worker(
        socket_path=args.socket,
        provider_database=args.provider_database,
        allowance_database=args.allowance_database,
        socket_group_gid=args.socket_group_gid,
    )
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    try:
        worker.start()
        worker.serve(should_stop=lambda: stopping)
    finally:
        worker.close()
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
