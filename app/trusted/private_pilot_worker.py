"""Explicit credential-holding worker for the bounded INT-22 private pilot."""

import argparse
from collections.abc import Callable
import json
from pathlib import Path
import signal

from app.cognition.conversation_context import MemoryModelRequest
from app.cognition.worker_extraction import ExtractionModelRequest
from app.cognition.worker_evolution import EvolutionModelRequest
from app.cognition.shared_context import SharedModelRequest
from app.core.provider_contract import (
    DeploymentStage,
    OpenAIProviderConfig,
    PROMPT_VERSION,
)
from app.core.provider_spend import ProviderPersistenceError, ProviderStateStore
from app.core.store import EventStore
from app.trusted.openai_credential_files import (
    TrustedTransportConfigurationError,
    build_worker_openai_transport,
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
    protected_state_identity,
    read_pilot_control,
)


ACTIVATION_PATH = Path("/run/almost-reasonable-private-pilot/control/activation.json")
STOP_PATH = Path("/run/almost-reasonable-private-pilot/control/emergency-stop.json")
STOP_VERSION = "private-pilot-emergency-stop-v1"
_STOP_RU = "Новые запросы к модели временно остановлены."
_REJECT_RU = "Локальный запрос не соответствует разрешённой проверке."
EXTRACTION_PERMISSION_VERSION = "private-memory-extraction-permission-v1"
EVOLUTION_PERMISSION_VERSION = "private-memory-evolution-permission-v1"


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
                read_pilot_control(self.path, maximum_bytes=1024),
                object_pairs_hook=_unique,
            )
            return not (
                type(value) is dict
                and set(value) == {"version", "stop"}
                and value["version"] == STOP_VERSION
                and value["stop"] is False
            )
        except (
            PilotAuthorityError,
            TrustedTransportConfigurationError,
            UnicodeDecodeError,
            ValueError,
        ):
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
        provider_state_current: Callable[[], bool] | None = None,
        provider_spend_suspended: Callable[[], bool] | None = None,
        extraction_permission_path: Path | None = None,
        evolution_permission_path: Path | None = None,
    ):
        self.activation = activation
        self.allowance = allowance
        self.activation_path = activation_path
        self.stop = stop
        self.provider_state_current = provider_state_current
        self.provider_spend_suspended = provider_spend_suspended
        self.extraction_permission_path = extraction_permission_path
        self.evolution_permission_path = evolution_permission_path

    def extraction_limit(self) -> int | None:
        return self._memory_limit(
            self.extraction_permission_path, EXTRACTION_PERMISSION_VERSION
        )

    def evolution_limit(self) -> int | None:
        return self._memory_limit(
            self.evolution_permission_path, EVOLUTION_PERMISSION_VERSION
        )

    def _memory_limit(self, path: Path | None, version: str) -> int | None:
        if path is None:
            return None
        try:
            value = json.loads(
                read_pilot_control(path, maximum_bytes=1024),
                object_pairs_hook=_unique,
            )
            if (
                type(value) is dict
                and set(value) == {"version", "pilot_id", "max_operations"}
                and value["version"] == version
                and value["pilot_id"] == self.activation.pilot_id
                and type(value["max_operations"]) is int
                and 1 <= value["max_operations"] <= self.activation.max_operations
            ):
                return value["max_operations"]
        except (
            PilotAuthorityError,
            TrustedTransportConfigurationError,
            UnicodeError,
            ValueError,
            TypeError,
        ):
            pass
        return None

    def current(self) -> bool:
        try:
            return load_activation(self.activation_path) == self.activation
        except PilotAuthorityError:
            return False

    def _spend_blocked(self) -> bool:
        if self.provider_spend_suspended is None:
            return False
        try:
            return self.provider_spend_suspended()
        except ProviderPersistenceError:
            return True

    def authorize(self, request: InferenceRequest) -> WorkerResponse | None:
        if self.stop.active():
            return WorkerResponse("emergency_stop", 0, _STOP_RU, None)
        extraction = type(request.model_request) is ExtractionModelRequest
        evolution = type(request.model_request) is EvolutionModelRequest
        limit = (
            self.extraction_limit()
            if extraction
            else self.evolution_limit()
            if evolution
            else None
        )
        if (
            (
                limit is None
                if extraction or evolution
                else type(request.model_request)
                not in {MemoryModelRequest, SharedModelRequest}
            )
            or not self.current()
            or (
                self.provider_state_current is not None
                and not self.provider_state_current()
            )
            or self._spend_blocked()
        ):
            return WorkerResponse("authorization_rejected", 0, _REJECT_RU, None)
        try:
            reserved = self.allowance.reserve(
                request.operation_id, extraction_limit=limit
            )
        except PilotAuthorityError:
            reserved = False
        if not reserved:
            return WorkerResponse("authorization_rejected", 0, _REJECT_RU, None)
        return None

    def paid_transport_blocked(self) -> bool:
        return (
            self.stop.active()
            or not self.current()
            or (
                self.provider_state_current is not None
                and not self.provider_state_current()
            )
            or self._spend_blocked()
        )

    def extraction_commit_allowed(self) -> bool:
        return not self.paid_transport_blocked() and self.extraction_limit() is not None

    def evolution_commit_allowed(self) -> bool:
        return not self.paid_transport_blocked() and self.evolution_limit() is not None


def build_worker(
    *,
    socket_path: Path,
    provider_database: Path,
    allowance_database: Path,
    activation_path: Path = ACTIVATION_PATH,
    stop_path: Path = STOP_PATH,
    socket_group_gid: int | None = None,
    extraction_permission_path: Path | None = None,
    evolution_permission_path: Path | None = None,
) -> OpenAIWorkerServer:
    """Live entry point; only the protected credential-file factory creates SDK."""

    activation = load_activation(activation_path)
    allowance = PilotAllowance(allowance_database, activation)
    provider_identity = _require_provider_database(provider_database)

    def provider_state_current() -> bool:
        try:
            return _require_provider_database(provider_database) == provider_identity
        except PilotAuthorityError:
            return False

    provider_store = ProviderStateStore(EventStore(provider_database))
    gate = PilotRequestGate(
        activation,
        allowance,
        activation_path=activation_path,
        stop=PilotStop(stop_path),
        provider_state_current=provider_state_current,
        provider_spend_suspended=provider_store.is_spend_suspended,
        extraction_permission_path=extraction_permission_path,
        evolution_permission_path=evolution_permission_path,
    )
    transport = build_worker_openai_transport(
        project_id=activation.project_id,
        organization_id=activation.organization_id,
    )
    gateway = TrustedOpenAIGateway(
        store=provider_store,
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
        extraction_enabled=extraction_permission_path is not None,
        extraction_commit_guard=gate.extraction_commit_allowed,
        evolution_enabled=evolution_permission_path is not None,
        evolution_commit_guard=gate.evolution_commit_allowed,
    )


def _require_provider_database(
    path: Path,
) -> tuple[tuple[tuple[int, int], ...], int, int]:
    """A missing worker spend ledger cannot silently restart unsuspended."""

    return protected_state_identity(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the bounded private pilot worker")
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--provider-database", required=True, type=Path)
    parser.add_argument("--allowance-database", required=True, type=Path)
    parser.add_argument("--socket-group-gid", required=True, type=int)
    parser.add_argument("--memory-extraction-permission", type=Path)
    parser.add_argument("--memory-evolution-permission", type=Path)
    args = parser.parse_args(argv)
    worker = build_worker(
        socket_path=args.socket,
        provider_database=args.provider_database,
        allowance_database=args.allowance_database,
        socket_group_gid=args.socket_group_gid,
        extraction_permission_path=args.memory_extraction_permission,
        evolution_permission_path=args.memory_evolution_permission,
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
