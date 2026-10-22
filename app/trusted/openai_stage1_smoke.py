"""Default-deny worker-owned authorization for one Stage 1 OpenAI smoke."""

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import signal

from app.core.openai_stage1_contract import (
    STAGE1_CORRELATION_ID,
    STAGE1_MODEL,
    STAGE1_OPERATION_ID,
    STAGE1_PROVIDER_CONFIG_VERSION,
    build_stage1_synthetic_request,
    stage1_model_request_digest,
)
from app.core.provider_contract import (
    PROMPT_VERSION,
    DeploymentStage,
    OpenAIProviderConfig,
    PricingConfig,
    ProviderUsage,
)
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.trusted.openai_credential_files import (
    TrustedTransportConfigurationError,
    build_worker_openai_transport,
    read_protected_worker_file,
)
from app.trusted.openai_gateway import TrustedOpenAIGateway
from app.trusted.openai_transport import (
    FakeOpenAITransport,
    ProviderContent,
    ProviderOutput,
    ProviderResponse,
)
from app.trusted.openai_worker_protocol import InferenceRequest, WorkerResponse
from app.trusted.openai_worker_server import OpenAIWorkerServer, WorkerServerConfig


ACTIVATION_PATH = Path("/run/almost-reasonable-stage1/control/activation.json")
EMERGENCY_STOP_PATH = Path("/run/almost-reasonable-stage1/control/emergency-stop.json")
ACTIVATION_VERSION = "openai-stage1-activation-v1"
STOP_VERSION = "openai-stage1-emergency-stop-v1"
MAX_CONTROL_FILE_BYTES = 4096
MAX_ACTIVATION_SECONDS = 15 * 60
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_STOP_RU = "Новые запросы к модели временно остановлены."
_AUTHORIZATION_RU = "Локальный запрос не соответствует разрешённой проверке."


@dataclass(frozen=True, slots=True, repr=False)
class Stage1Activation:
    version: str
    session_id: str
    operation_id: str
    correlation_id: str
    model_request_sha256: str
    provider_config_version: str
    model: str
    project_id: str
    organization_id: str
    not_before: datetime
    expires_at: datetime


class WorkerEmergencyStop:
    """Protected operator stop source that defaults to active on every error."""

    def __init__(self, path: Path = EMERGENCY_STOP_PATH):
        self._path = path

    def active(self) -> bool:
        try:
            value = _read_control_json(self._path)
            return not (
                type(value) is dict
                and set(value) == {"version", "stop"}
                and value["version"] == STOP_VERSION
                and value["stop"] is False
            )
        except TrustedTransportConfigurationError:
            return True


class Stage1RequestGate:
    """Bind IPC input to one unchanged, short-lived operator activation."""

    def __init__(
        self,
        activation: Stage1Activation,
        *,
        stop: WorkerEmergencyStop,
        activation_path: Path = ACTIVATION_PATH,
        utcnow=lambda: datetime.now(UTC),
    ):
        self.activation = activation
        self._stop = stop
        self._activation_path = activation_path
        self._utcnow = utcnow

    def authorize(self, request: InferenceRequest) -> WorkerResponse | None:
        if self._stop.active():
            return WorkerResponse("emergency_stop", 0, _STOP_RU, None)
        try:
            current = load_stage1_activation(self._activation_path, now=self._utcnow())
            digest = stage1_model_request_digest(request.model_request)
        except (TrustedTransportConfigurationError, TypeError, ValueError):
            return WorkerResponse("authorization_rejected", 0, _AUTHORIZATION_RU, None)
        if (
            current != self.activation
            or request.operation_id != current.operation_id
            or request.correlation_id != current.correlation_id
            or digest != current.model_request_sha256
        ):
            return WorkerResponse("authorization_rejected", 0, _AUTHORIZATION_RU, None)
        return None

    def paid_transport_blocked(self) -> bool:
        if self._stop.active():
            return True
        try:
            return (
                load_stage1_activation(self._activation_path, now=self._utcnow())
                != self.activation
            )
        except TrustedTransportConfigurationError:
            return True


def stage1_provider_config() -> OpenAIProviderConfig:
    return OpenAIProviderConfig(
        version=STAGE1_PROVIDER_CONFIG_VERSION,
        model=STAGE1_MODEL,
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


def stage1_pricing() -> PricingConfig:
    return PricingConfig(
        version="openai-pricing-2026-10-10-standard-short-v1",
        model=STAGE1_MODEL,
        currency="USD",
        input_per_million=Decimal("0.10"),
        cached_input_per_million=Decimal("0.01"),
        output_per_million=Decimal("0.50"),
    )


def load_stage1_activation(
    path: Path = ACTIVATION_PATH, *, now: datetime | None = None
) -> Stage1Activation:
    value = _read_control_json(path)
    fields = {
        "version",
        "session_id",
        "operation_id",
        "correlation_id",
        "model_request_sha256",
        "provider_config_version",
        "model",
        "project_id",
        "organization_id",
        "not_before",
        "expires_at",
    }
    if type(value) is not dict or set(value) != fields:
        raise TrustedTransportConfigurationError("Stage 1 activation is malformed")
    try:
        not_before = _parse_utc_second(value["not_before"])
        expires_at = _parse_utc_second(value["expires_at"])
        current = datetime.now(UTC) if now is None else now
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise ValueError
        request = build_stage1_synthetic_request()
        activation = Stage1Activation(
            version=value["version"],
            session_id=value["session_id"],
            operation_id=value["operation_id"],
            correlation_id=value["correlation_id"],
            model_request_sha256=value["model_request_sha256"],
            provider_config_version=value["provider_config_version"],
            model=value["model"],
            project_id=value["project_id"],
            organization_id=value["organization_id"],
            not_before=not_before,
            expires_at=expires_at,
        )
        fixed = (
            activation.version == ACTIVATION_VERSION
            and _identifier(activation.session_id)
            and activation.operation_id == STAGE1_OPERATION_ID
            and activation.correlation_id == STAGE1_CORRELATION_ID
            and activation.model_request_sha256 == stage1_model_request_digest(request)
            and _DIGEST.fullmatch(activation.model_request_sha256) is not None
            and activation.provider_config_version == STAGE1_PROVIDER_CONFIG_VERSION
            and activation.model == STAGE1_MODEL
            and _identifier(activation.project_id)
            and activation.project_id.startswith("proj_")
            and _identifier(activation.organization_id)
            and activation.organization_id.startswith("org-")
        )
        lifetime = (expires_at - not_before).total_seconds()
        if (
            not fixed
            or not 0 < lifetime <= MAX_ACTIVATION_SECONDS
            or not not_before <= current.astimezone(UTC) <= expires_at
        ):
            raise ValueError
        return activation
    except (KeyError, TypeError, ValueError):
        raise TrustedTransportConfigurationError(
            "Stage 1 activation is invalid or stale"
        ) from None


def _read_control_json(path: Path) -> object:
    payload = read_protected_worker_file(path, maximum_bytes=MAX_CONTROL_FILE_BYTES)
    try:
        return json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_control_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TrustedTransportConfigurationError("control file is malformed") from None


def _unique_control_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise json.JSONDecodeError("duplicate field", key, 0)
        value[key] = item
    return value


def _parse_utc_second(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
    ):
        raise ValueError
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is not None
    )


def _fake_response() -> ProviderResponse:
    return ProviderResponse(
        response_id="resp_stage1_dry_run",
        status="completed",
        output=(
            ProviderOutput(
                "message",
                (
                    ProviderContent(
                        "output_text",
                        '{"proposal":{"decision":"no_action",'
                        '"reason_category":"not_warranted"}}',
                    ),
                ),
            ),
        ),
        usage=ProviderUsage(100, 0, 20, 120),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the one-shot Stage 1 worker")
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--allow-uid", required=True, type=int)
    parser.add_argument("--socket-group-gid", required=True, type=int)
    parser.add_argument("--log-peer-uids", action="store_true")
    parser.add_argument("--fake-transport", action="store_true")
    arguments = parser.parse_args(argv)
    activation = load_stage1_activation()
    stop = WorkerEmergencyStop()
    gate = Stage1RequestGate(activation, stop=stop)
    transport = (
        FakeOpenAITransport([_fake_response()])
        if arguments.fake_transport
        else build_worker_openai_transport(
            project_id=activation.project_id,
            organization_id=activation.organization_id,
        )
    )
    gateway = TrustedOpenAIGateway(
        store=ProviderStateStore(EventStore(arguments.database)),
        transport=transport,
        config=stage1_provider_config(),
        pricing=stage1_pricing(),
        emergency_stop=gate.paid_transport_blocked,
    )
    server = OpenAIWorkerServer(
        WorkerServerConfig(
            arguments.socket,
            frozenset((arguments.allow_uid,)),
            trusted_ipc_gid=arguments.socket_group_gid,
            log_peer_uids=arguments.log_peer_uids,
        ),
        gateway,
        request_guard=gate.authorize,
    )
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    try:
        server.start()
        server.serve(max_connections=2, should_stop=lambda: stopping)
    finally:
        server.close()
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
