"""Bounded disabled-by-default orchestration for the fake hosted-search boundary."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import math
import time
from typing import Protocol

from .brief import ResearchBrief
from .provenance import ResearchEvidence, parse_hosted_search_response
from .provider import (
    HostedSearchConfig,
    HostedSearchRequest,
    HostedSearchResponse,
    HostedSearchTransportError,
    build_hosted_search_request,
)


_GENERAL_FAILURE_RU = (
    "Исследование сейчас недоступно; проверенные выводы не сформированы."
)
_STOP_FAILURE_RU = "Новые исследовательские запросы временно остановлены."
_SPEND_FAILURE_RU = "Исследовательские запросы приостановлены из-за лимита расходов."


class ResearchTransport(Protocol):
    def call(
        self, request: HostedSearchRequest, *, timeout_seconds: float
    ) -> HostedSearchResponse: ...


class ResearchOutcome(StrEnum):
    COMPLETED = "completed"
    DISABLED = "disabled"
    EMERGENCY_STOP = "emergency_stop"
    SPEND_SUSPENDED = "spend_suspended"
    SPEND_LIMIT_CONFIRMED = "spend_limit_confirmed"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    AUTHENTICATION_ERROR = "authentication_error"
    INVALID_REQUEST = "invalid_request"
    PERMISSION_ERROR = "permission_error"
    BILLING_QUOTA = "billing_quota"
    PROVIDER_ERROR = "provider_error"
    INVALID_RESPONSE = "invalid_response"


@dataclass(frozen=True, slots=True)
class ResearchResult:
    outcome: ResearchOutcome
    attempts: int
    evidence: ResearchEvidence | None
    safe_russian_message: str | None
    can_modify_policy: bool = False
    can_update_beliefs: bool = False


class ResearchService:
    """Use only an injected narrow transport; this class has no SDK or credentials."""

    def __init__(
        self,
        *,
        config: HostedSearchConfig,
        transport: ResearchTransport,
        emergency_stop: Callable[[], bool] = lambda: False,
        spend_suspended: Callable[[], bool] = lambda: False,
        mark_spend_suspended: Callable[[str], None] = lambda _code: None,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._config = config
        self._transport = transport
        self._emergency_stop = emergency_stop
        self._spend_suspended = spend_suspended
        self._mark_spend_suspended = mark_spend_suspended
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._sleeper = sleeper

    def run(self, brief: ResearchBrief) -> ResearchResult:
        if type(brief) is not ResearchBrief:
            raise TypeError("brief must be a ResearchBrief")
        request = build_hosted_search_request(brief, self._config)
        if not self._config.enabled:
            return self._result(ResearchOutcome.DISABLED, 0)
        if self._emergency_stop():
            return self._result(ResearchOutcome.EMERGENCY_STOP, 0)
        if self._spend_suspended():
            return self._result(ResearchOutcome.SPEND_SUSPENDED, 0)

        started = self._checked_monotonic()
        deadline = started + self._config.overall_timeout_seconds
        attempts = 0
        while True:
            if attempts:
                if self._emergency_stop():
                    return self._result(ResearchOutcome.EMERGENCY_STOP, attempts)
                if self._spend_suspended():
                    return self._result(ResearchOutcome.SPEND_SUSPENDED, attempts)
            now = self._checked_monotonic()
            remaining = deadline - now
            if remaining <= 0:
                return self._result(ResearchOutcome.TIMEOUT, attempts)
            attempts += 1
            try:
                response = self._transport.call(
                    request,
                    timeout_seconds=min(self._config.timeout_seconds, remaining),
                )
            except HostedSearchTransportError as error:
                ended = self._checked_monotonic()
                outcome = _classify_error(error.code)
                if outcome is ResearchOutcome.SPEND_LIMIT_CONFIRMED:
                    self._mark_spend_suspended(error.code)
                    return self._result(outcome, attempts)
                if ended >= deadline:
                    return self._result(ResearchOutcome.TIMEOUT, attempts)
                if outcome not in _RETRYABLE or attempts >= self._config.max_attempts:
                    return self._result(outcome, attempts)
                delay = min(
                    _retry_delay(self._config.initial_backoff_seconds, attempts),
                    self._config.maximum_backoff_seconds,
                )
                if error.retry_after_seconds is not None:
                    delay = min(
                        max(0.0, error.retry_after_seconds),
                        self._config.maximum_backoff_seconds,
                    )
                if ended + delay >= deadline:
                    return self._result(outcome, attempts)
                self._sleeper(delay)
                continue
            if self._checked_monotonic() >= deadline:
                return self._result(ResearchOutcome.TIMEOUT, attempts)
            try:
                evidence = parse_hosted_search_response(
                    response,
                    retrieved_at=self._utcnow(),
                    max_search_calls=self._config.max_search_calls,
                    max_sources=brief.result_budget,
                )
            except (TypeError, ValueError):
                return self._result(ResearchOutcome.INVALID_RESPONSE, attempts)
            return ResearchResult(
                ResearchOutcome.COMPLETED,
                attempts,
                evidence,
                None,
            )

    def _checked_monotonic(self) -> float:
        value = self._monotonic()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise RuntimeError("monotonic clock returned an invalid value")
        return float(value)

    @staticmethod
    def _result(outcome: ResearchOutcome, attempts: int) -> ResearchResult:
        if outcome is ResearchOutcome.EMERGENCY_STOP:
            message = _STOP_FAILURE_RU
        elif outcome in {
            ResearchOutcome.SPEND_SUSPENDED,
            ResearchOutcome.SPEND_LIMIT_CONFIRMED,
        }:
            message = _SPEND_FAILURE_RU
        else:
            message = _GENERAL_FAILURE_RU
        return ResearchResult(outcome, attempts, None, message)


_RETRYABLE = {
    ResearchOutcome.RATE_LIMIT,
    ResearchOutcome.SERVER_ERROR,
    ResearchOutcome.TIMEOUT,
    ResearchOutcome.CONNECTION_ERROR,
}

_ERROR_OUTCOMES = {
    "rate_limit": ResearchOutcome.RATE_LIMIT,
    "server_error": ResearchOutcome.SERVER_ERROR,
    "timeout": ResearchOutcome.TIMEOUT,
    "connection_error": ResearchOutcome.CONNECTION_ERROR,
    "spend_limit_confirmed": ResearchOutcome.SPEND_LIMIT_CONFIRMED,
    "authentication_error": ResearchOutcome.AUTHENTICATION_ERROR,
    "invalid_request": ResearchOutcome.INVALID_REQUEST,
    "permission_error": ResearchOutcome.PERMISSION_ERROR,
    "billing_quota": ResearchOutcome.BILLING_QUOTA,
}


def _classify_error(code: object) -> ResearchOutcome:
    if not isinstance(code, str):
        return ResearchOutcome.PROVIDER_ERROR
    return _ERROR_OUTCOMES.get(code, ResearchOutcome.PROVIDER_ERROR)


def _retry_delay(initial: float, attempts: int) -> float:
    return initial * (2 ** (attempts - 1))
