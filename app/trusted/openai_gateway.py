"""Trusted, bounded gateway from secretless model requests to untrusted proposals."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import json
import math
import time

from app.core.agent_proposals import ProposalValidation, parse_agent_proposal
from app.cognition.worker_extraction import (
    ExtractionModelRequest,
    ExtractionProposal,
    parse_extraction_proposal,
)
from app.cognition.worker_evolution import (
    EvolutionModelRequest,
    EvolutionProposal,
    parse_evolution_proposal,
)
from app.cognition.worker_typed import (
    TypedModelRequest,
    TypedProposal,
    parse_typed_proposal,
)
from app.core.model_contract import ModelRequest
from app.core.provider_contract import (
    PROVIDER_NAME,
    DeploymentStage,
    OpenAIProviderConfig,
    PricingConfig,
    ProviderUsage,
    build_openai_request,
    build_worker_context_request,
    build_worker_extraction_request,
    build_worker_evolution_request,
    build_worker_typed_request,
    estimate_cost,
    require_identifier,
    validate_pricing_config,
)
from app.core.provider_spend import (
    AttemptReservationStatus,
    CONFIRMED_SPEND_LIMIT_CODES,
    ProviderAttemptRecord,
    ProviderPersistenceError,
    ProviderStateStore,
)
from .openai_transport import (
    OpenAITransport,
    OpenAITransportError,
    ProviderResponse,
)


MAX_PROVIDER_OUTPUT_UTF8_BYTES = 8 * 1024
MAX_OPERATION_ID_LENGTH = 126

_GENERAL_FAILURE_RU = "Сервис модели временно недоступен. Попробуйте позже."
_SPEND_FAILURE_RU = "Запросы к модели приостановлены из-за лимита расходов."
_STOP_FAILURE_RU = "Новые запросы к модели временно остановлены."


class GatewayOutcome(StrEnum):
    COMPLETED = "completed"
    DISABLED = "disabled"
    EMERGENCY_STOP = "emergency_stop"
    SPEND_SUSPENDED = "spend_suspended"
    DUPLICATE_OPERATION = "duplicate_operation"
    REFUSED = "refused"
    INCOMPLETE = "incomplete"
    MISSING_OUTPUT = "missing_output"
    OVERSIZED_OUTPUT = "oversized_output"
    UNSUPPORTED_OUTPUT = "unsupported_output"
    INVALID_RESPONSE = "invalid_response"
    INVALID_PROPOSAL = "invalid_proposal"
    RATE_LIMIT = "rate_limit"
    SPEND_LIMIT_CONFIRMED = "spend_limit_confirmed"
    BILLING_QUOTA = "billing_quota"
    AMBIGUOUS_QUOTA = "ambiguous_quota"
    AUTHENTICATION_ERROR = "authentication_error"
    INVALID_REQUEST = "invalid_request"
    PERMISSION_ERROR = "permission_error"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    UNKNOWN_ERROR = "unknown_error"
    PERSISTENCE_FAILURE = "persistence_failure"


@dataclass(frozen=True, slots=True)
class GatewayResult:
    outcome: GatewayOutcome
    response_id: str | None
    attempts: int
    safe_russian_message: str | None
    validation: ProposalValidation | None = field(repr=False)
    extraction: ExtractionProposal | None = field(default=None, repr=False)
    evolution: EvolutionProposal | None = field(default=None, repr=False)
    typed: TypedProposal | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _ParsedResponse:
    outcome: GatewayOutcome
    validation: ProposalValidation | None
    extraction: ExtractionProposal | None = None
    evolution: EvolutionProposal | None = None
    typed: TypedProposal | None = None


class TrustedOpenAIGateway:
    """Enforce operational policy without executing the returned proposal."""

    def __init__(
        self,
        *,
        store: ProviderStateStore,
        transport: OpenAITransport,
        config: OpenAIProviderConfig,
        pricing: PricingConfig | None = None,
        emergency_stop: Callable[[], bool] = lambda: False,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[int, float], float] = lambda _attempt, _maximum: 0.0,
    ):
        if type(store) is not ProviderStateStore:
            raise TypeError("store must be a ProviderStateStore")
        if pricing is not None:
            validate_pricing_config(pricing)
        self._store = store
        self._transport = transport
        self._config = config
        self._pricing = pricing
        self._emergency_stop = emergency_stop
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._sleeper = sleeper
        self._jitter = jitter
        self._persistence_failed = False

    def submit(
        self,
        request: ModelRequest,
        *,
        correlation_id: str,
        operation_id: str,
    ) -> GatewayResult:
        if type(request) is not ModelRequest:
            raise TypeError("request must be a ModelRequest")
        return self._submit(
            request, correlation_id=correlation_id, operation_id=operation_id
        )

    def submit_context(
        self, request, *, correlation_id: str, operation_id: str
    ) -> GatewayResult:
        from app.cognition.conversation_context import MemoryModelRequest
        from app.cognition.shared_context import SharedModelRequest

        if type(request) not in {MemoryModelRequest, SharedModelRequest}:
            raise TypeError("request must be a typed context request")
        return self._submit(
            request, correlation_id=correlation_id, operation_id=operation_id
        )

    def submit_extraction(
        self, request: ExtractionModelRequest, *, correlation_id: str, operation_id: str
    ) -> GatewayResult:
        if type(request) is not ExtractionModelRequest:
            raise TypeError("request must be an extraction request")
        return self._submit(
            request, correlation_id=correlation_id, operation_id=operation_id
        )

    def submit_evolution(
        self, request: EvolutionModelRequest, *, correlation_id: str, operation_id: str
    ) -> GatewayResult:
        if type(request) is not EvolutionModelRequest:
            raise TypeError("request must be an evolution request")
        return self._submit(
            request, correlation_id=correlation_id, operation_id=operation_id
        )

    def submit_typed(
        self, request: TypedModelRequest, *, correlation_id: str, operation_id: str
    ) -> GatewayResult:
        if type(request) is not TypedModelRequest:
            raise TypeError("request must be a typed memory request")
        return self._submit(
            request, correlation_id=correlation_id, operation_id=operation_id
        )

    def _submit(
        self, request, *, correlation_id: str, operation_id: str
    ) -> GatewayResult:
        require_identifier("correlation id", correlation_id)
        require_identifier("operation id", operation_id)
        if len(operation_id) > MAX_OPERATION_ID_LENGTH:
            raise ValueError("invalid operation id")
        provider_request = (
            build_openai_request(request, self._config)
            if type(request) is ModelRequest
            else build_worker_extraction_request(request, self._config)
            if type(request) is ExtractionModelRequest
            else build_worker_evolution_request(request, self._config)
            if type(request) is EvolutionModelRequest
            else build_worker_typed_request(request, self._config)
            if type(request) is TypedModelRequest
            else build_worker_context_request(request, self._config)
        )
        if (
            self._config.deployment_stage is not DeploymentStage.STAGE_1
            or not self._config.paid_calls_enabled
        ):
            return self._result(GatewayOutcome.DISABLED, attempts=0)
        if self._persistence_failed:
            return self._result(GatewayOutcome.PERSISTENCE_FAILURE, attempts=0)

        overall_start = self._checked_monotonic()
        deadline = overall_start + self._config.overall_timeout_seconds
        completed_attempts = 0
        attempt_number = 1
        while True:
            attempt_id = f"{operation_id}:{attempt_number}"
            started_monotonic = self._checked_monotonic()
            remaining = deadline - started_monotonic
            if remaining <= 0:
                return self._result(GatewayOutcome.TIMEOUT, attempts=completed_attempts)
            started_at = self._utcnow()
            preflight = self._preflight(attempt_id, correlation_id, started_at)
            if preflight is not None:
                return self._result(preflight, attempts=completed_attempts)
            ready_monotonic = self._checked_monotonic()
            if self._emergency_stop():
                return self._finish_reserved_without_call(
                    request=request,
                    correlation_id=correlation_id,
                    attempt_id=attempt_id,
                    attempt_number=attempt_number,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    ended_monotonic=ready_monotonic,
                    outcome=GatewayOutcome.EMERGENCY_STOP,
                    completed_attempts=completed_attempts,
                )
            remaining = deadline - ready_monotonic
            if remaining <= 0:
                return self._finish_reserved_without_call(
                    request=request,
                    correlation_id=correlation_id,
                    attempt_id=attempt_id,
                    attempt_number=attempt_number,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    ended_monotonic=ready_monotonic,
                    outcome=GatewayOutcome.TIMEOUT,
                    completed_attempts=completed_attempts,
                )
            timeout = min(self._config.timeout_seconds, remaining)
            try:
                response = self._transport.call(
                    provider_request, timeout_seconds=timeout
                )
            except OpenAITransportError as error:
                completed_attempts = attempt_number
                ended = self._checked_monotonic()
                outcome = _classify_error(error)
                if (
                    ended >= deadline
                    and outcome is not GatewayOutcome.SPEND_LIMIT_CONFIRMED
                ):
                    outcome = GatewayOutcome.TIMEOUT
                persisted = self._record(
                    request=request,
                    correlation_id=correlation_id,
                    attempt_id=attempt_id,
                    attempt_number=attempt_number,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    ended_monotonic=ended,
                    outcome=outcome,
                    response_id=None,
                    usage=None,
                    error_code=error.error_code,
                    suspend=(
                        error.error_code
                        if outcome is GatewayOutcome.SPEND_LIMIT_CONFIRMED
                        else None
                    ),
                )
                if not persisted:
                    return self._result(
                        GatewayOutcome.PERSISTENCE_FAILURE,
                        attempts=completed_attempts,
                    )
                if outcome is GatewayOutcome.SPEND_LIMIT_CONFIRMED:
                    return self._result(outcome, attempts=completed_attempts)
                if outcome not in _RETRYABLE_OUTCOMES:
                    return self._result(outcome, attempts=completed_attempts)
                if attempt_number == self._config.max_attempts:
                    return self._result(outcome, attempts=completed_attempts)
                delay = self._retry_delay(attempt_number, error)
                if delay is None or ended + delay >= deadline:
                    return self._result(outcome, attempts=completed_attempts)
                self._sleeper(delay)
                attempt_number += 1
                continue

            completed_attempts = attempt_number
            ended = self._checked_monotonic()
            parsed = (
                _ParsedResponse(GatewayOutcome.TIMEOUT, None)
                if ended >= deadline
                else _parse_response(
                    response,
                    extraction=type(request) is ExtractionModelRequest,
                    evolution=type(request) is EvolutionModelRequest,
                    typed=type(request) is TypedModelRequest,
                )
            )
            persisted = self._record(
                request=request,
                correlation_id=correlation_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                started_at=started_at,
                started_monotonic=started_monotonic,
                ended_monotonic=ended,
                outcome=parsed.outcome,
                response_id=response.response_id,
                usage=response.usage,
                error_code=None,
                suspend=None,
            )
            if not persisted:
                return self._result(
                    GatewayOutcome.PERSISTENCE_FAILURE,
                    attempts=completed_attempts,
                )
            return self._result(
                parsed.outcome,
                attempts=completed_attempts,
                response_id=response.response_id,
                validation=parsed.validation,
                extraction=parsed.extraction,
                evolution=parsed.evolution,
                typed=parsed.typed,
            )

    def _preflight(
        self, attempt_id: str, correlation_id: str, started_at: datetime
    ) -> GatewayOutcome | None:
        if self._emergency_stop():
            return GatewayOutcome.EMERGENCY_STOP
        try:
            reservation = self._store.reserve_attempt(
                attempt_id, correlation_id, started_at
            )
            if reservation is AttemptReservationStatus.SPEND_SUSPENDED:
                return GatewayOutcome.SPEND_SUSPENDED
            if reservation is AttemptReservationStatus.DUPLICATE:
                return GatewayOutcome.DUPLICATE_OPERATION
        except ProviderPersistenceError:
            self._persistence_failed = True
            return GatewayOutcome.PERSISTENCE_FAILURE
        return None

    def _record(
        self,
        *,
        request: ModelRequest,
        correlation_id: str,
        attempt_id: str,
        attempt_number: int,
        started_at: datetime,
        started_monotonic: float,
        ended_monotonic: float,
        outcome: GatewayOutcome,
        response_id: str | None,
        usage: ProviderUsage | None,
        error_code: str | None,
        suspend: str | None,
    ) -> bool:
        duration = ended_monotonic - started_monotonic
        if not math.isfinite(duration) or duration < 0:
            self._persistence_failed = True
            return False
        estimate = estimate_cost(usage, self._pricing, self._config.model)
        record = ProviderAttemptRecord(
            attempt_id=attempt_id,
            correlation_id=correlation_id,
            provider=PROVIDER_NAME,
            model_identifier=self._config.model,
            provider_config_version=self._config.version,
            prompt_version=(
                "private-evolution-v1"
                if type(request) is EvolutionModelRequest
                else "private-typed-v1"
                if type(request) is TypedModelRequest
                else "private-extraction-v1"
                if type(request) is ExtractionModelRequest
                else self._config.prompt_version
            ),
            core_self_version=(
                request.core_self_version
                if type(request) is ModelRequest
                else "private-evolution-v1"
                if type(request) is EvolutionModelRequest
                else "private-typed-v1"
                if type(request) is TypedModelRequest
                else "private-extraction-v1"
                if type(request) is ExtractionModelRequest
                else request.conversation.core_self_version
            ),
            provider_response_id=response_id,
            started_at=started_at,
            duration_ms=round(duration * 1000),
            input_tokens=usage.input_tokens if usage is not None else None,
            cached_input_tokens=(
                usage.cached_input_tokens if usage is not None else None
            ),
            output_tokens=usage.output_tokens if usage is not None else None,
            total_tokens=usage.total_tokens if usage is not None else None,
            outcome=outcome.value,
            retry_count=attempt_number - 1,
            estimated_cost=(
                format(estimate.amount, "f") if estimate.amount is not None else None
            ),
            pricing_version=estimate.pricing_version,
            currency=estimate.currency,
            cost_estimate_status=estimate.status,
            provider_error_code=error_code,
        )
        try:
            created = self._store.record_attempt(
                record,
                confirmed_spend_limit_code=suspend,
                require_reservation=True,
            )
        except (ProviderPersistenceError, TypeError, ValueError):
            self._persistence_failed = True
            return False
        if not created:
            self._persistence_failed = True
            return False
        return True

    def _finish_reserved_without_call(
        self,
        *,
        request: ModelRequest,
        correlation_id: str,
        attempt_id: str,
        attempt_number: int,
        started_at: datetime,
        started_monotonic: float,
        ended_monotonic: float,
        outcome: GatewayOutcome,
        completed_attempts: int,
    ) -> GatewayResult:
        persisted = self._record(
            request=request,
            correlation_id=correlation_id,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            started_at=started_at,
            started_monotonic=started_monotonic,
            ended_monotonic=ended_monotonic,
            outcome=outcome,
            response_id=None,
            usage=None,
            error_code=None,
            suspend=None,
        )
        if not persisted:
            return self._result(
                GatewayOutcome.PERSISTENCE_FAILURE,
                attempts=completed_attempts,
            )
        return self._result(outcome, attempts=completed_attempts)

    def _retry_delay(
        self, attempt_number: int, error: OpenAITransportError
    ) -> float | None:
        backoff = min(
            self._config.maximum_backoff_seconds,
            self._config.initial_backoff_seconds * (2 ** (attempt_number - 1)),
        )
        if error.retry_after_seconds is not None:
            if error.retry_after_seconds > self._config.maximum_backoff_seconds:
                return None
            backoff = max(backoff, error.retry_after_seconds)
        jitter = self._jitter(attempt_number, self._config.maximum_jitter_seconds)
        if (
            type(jitter) not in {int, float}
            or not math.isfinite(jitter)
            or not 0 <= jitter <= self._config.maximum_jitter_seconds
        ):
            raise ValueError("jitter source returned an invalid value")
        return backoff + jitter

    def _checked_monotonic(self) -> float:
        value = self._monotonic()
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError("clock returned an invalid value")
        return value

    @staticmethod
    def _result(
        outcome: GatewayOutcome,
        *,
        attempts: int,
        response_id: str | None = None,
        validation: ProposalValidation | None = None,
        extraction: ExtractionProposal | None = None,
        evolution: EvolutionProposal | None = None,
        typed: TypedProposal | None = None,
    ) -> GatewayResult:
        if outcome is GatewayOutcome.COMPLETED:
            message = None
        elif outcome in {
            GatewayOutcome.SPEND_SUSPENDED,
            GatewayOutcome.SPEND_LIMIT_CONFIRMED,
        }:
            message = _SPEND_FAILURE_RU
        elif outcome is GatewayOutcome.EMERGENCY_STOP:
            message = _STOP_FAILURE_RU
        else:
            message = _GENERAL_FAILURE_RU
        return GatewayResult(
            outcome,
            response_id,
            attempts,
            message,
            validation,
            extraction,
            evolution,
            typed,
        )


_RETRYABLE_OUTCOMES = frozenset(
    (
        GatewayOutcome.RATE_LIMIT,
        GatewayOutcome.SERVER_ERROR,
        GatewayOutcome.TIMEOUT,
        GatewayOutcome.CONNECTION_ERROR,
    )
)

_BILLING_QUOTA_CODES = frozenset(
    ("credit_balance_exhausted", "organization_usage_limit_exceeded")
)


def _classify_error(error: OpenAITransportError) -> GatewayOutcome:
    if error.timeout:
        return GatewayOutcome.TIMEOUT
    if error.connection:
        return GatewayOutcome.CONNECTION_ERROR
    if error.error_code in CONFIRMED_SPEND_LIMIT_CODES:
        return GatewayOutcome.SPEND_LIMIT_CONFIRMED
    if error.error_code in _BILLING_QUOTA_CODES:
        return GatewayOutcome.BILLING_QUOTA
    if error.status_code == 401:
        return GatewayOutcome.AUTHENTICATION_ERROR
    if error.status_code == 403:
        return GatewayOutcome.PERMISSION_ERROR
    if error.status_code in {400, 422}:
        return GatewayOutcome.INVALID_REQUEST
    if error.status_code == 429:
        if error.error_type == "rate_limit_error" or error.error_code in {
            "rate_limit_exceeded",
            "slow_down",
        }:
            return GatewayOutcome.RATE_LIMIT
        if error.error_type == "insufficient_quota":
            return GatewayOutcome.BILLING_QUOTA
        return GatewayOutcome.AMBIGUOUS_QUOTA
    if error.status_code is not None and 500 <= error.status_code <= 599:
        return GatewayOutcome.SERVER_ERROR
    return GatewayOutcome.UNKNOWN_ERROR


def _parse_response(
    response: ProviderResponse,
    *,
    extraction: bool = False,
    evolution: bool = False,
    typed: bool = False,
) -> _ParsedResponse:
    if type(response) is not ProviderResponse:
        return _ParsedResponse(GatewayOutcome.INVALID_RESPONSE, None)
    if response.status == "incomplete":
        return _ParsedResponse(GatewayOutcome.INCOMPLETE, None)
    if response.status != "completed":
        return _ParsedResponse(GatewayOutcome.INVALID_RESPONSE, None)
    messages = []
    for item in response.output:
        if item.type == "reasoning" and not item.content:
            continue
        if item.type == "message":
            messages.append(item)
            continue
        return _ParsedResponse(GatewayOutcome.UNSUPPORTED_OUTPUT, None)
    if len(messages) != 1:
        return _ParsedResponse(GatewayOutcome.MISSING_OUTPUT, None)
    content = messages[0].content
    if any(item.type == "refusal" for item in content):
        return _ParsedResponse(GatewayOutcome.REFUSED, None)
    if not content:
        return _ParsedResponse(GatewayOutcome.MISSING_OUTPUT, None)
    if len(content) != 1 or content[0].type != "output_text":
        return _ParsedResponse(GatewayOutcome.UNSUPPORTED_OUTPUT, None)
    text = content[0].text
    if not isinstance(text, str) or not text:
        return _ParsedResponse(GatewayOutcome.MISSING_OUTPUT, None)
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return _ParsedResponse(GatewayOutcome.INVALID_RESPONSE, None)
    if size > MAX_PROVIDER_OUTPUT_UTF8_BYTES:
        return _ParsedResponse(GatewayOutcome.OVERSIZED_OUTPUT, None)
    try:
        payload = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return _ParsedResponse(GatewayOutcome.INVALID_RESPONSE, None)
    if extraction:
        try:
            parsed = parse_extraction_proposal(text)
        except ValueError:
            return _ParsedResponse(GatewayOutcome.INVALID_PROPOSAL, None)
        return _ParsedResponse(GatewayOutcome.COMPLETED, None, parsed)
    if evolution:
        try:
            parsed_evolution = parse_evolution_proposal(text)
        except ValueError:
            return _ParsedResponse(GatewayOutcome.INVALID_PROPOSAL, None)
        return _ParsedResponse(
            GatewayOutcome.COMPLETED, None, evolution=parsed_evolution
        )
    if typed:
        try:
            parsed_typed = parse_typed_proposal(text)
        except ValueError:
            return _ParsedResponse(GatewayOutcome.INVALID_PROPOSAL, None)
        return _ParsedResponse(GatewayOutcome.COMPLETED, None, typed=parsed_typed)
    if type(payload) is not dict or set(payload) != {
        "decision",
        "reason_category",
        "content",
        "brief",
        "draft",
    }:
        return _ParsedResponse(GatewayOutcome.INVALID_RESPONSE, None)
    decision = payload["decision"]
    if not isinstance(decision, str):
        return _ParsedResponse(GatewayOutcome.INVALID_RESPONSE, None)
    fields = {
        "no_action": "reason_category",
        "reply": "content",
        "research": "brief",
        "propose_publication": "draft",
    }
    selected = fields.get(decision)
    if selected is None:
        validation = parse_agent_proposal({"decision": decision})
        return _ParsedResponse(GatewayOutcome.INVALID_PROPOSAL, validation)
    if any(
        payload[field] is not None
        for field in ("reason_category", "content", "brief", "draft")
        if field != selected
    ):
        return _ParsedResponse(GatewayOutcome.INVALID_PROPOSAL, None)
    validation = parse_agent_proposal(
        {"decision": decision, selected: payload[selected]}
    )
    if not validation.valid:
        return _ParsedResponse(GatewayOutcome.INVALID_PROPOSAL, validation)
    return _ParsedResponse(GatewayOutcome.COMPLETED, validation)
