"""Credential-holding OpenAI SDK transport with a narrow secretless input."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
import os
import re
from typing import Protocol

from app.core.provider_contract import (
    MAX_PROVIDER_TOKEN_COUNT,
    OpenAIRequest,
    ProviderUsage,
)


MAX_RETRY_AFTER_SECONDS = 120.0
OPENAI_API_BASE_URL = "https://api.openai.com/v1"
_PROVIDER_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class TrustedTransportConfigurationError(RuntimeError):
    """The explicitly invoked credential boundary is not safely configured."""


class OpenAITransportError(RuntimeError):
    """Sanitized structured failure; provider messages are intentionally discarded."""

    def __init__(
        self,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        error_code: str | None = None,
        retry_after_seconds: float | None = None,
        timeout: bool = False,
        connection: bool = False,
    ):
        super().__init__("provider transport failed")
        self.status_code = status_code
        self.error_type = error_type
        self.error_code = error_code
        self.retry_after_seconds = retry_after_seconds
        self.timeout = timeout
        self.connection = connection

    def __repr__(self) -> str:
        return "OpenAITransportError('provider transport failed')"


@dataclass(frozen=True, slots=True, repr=False)
class ProviderContent:
    type: str
    text: str | None


@dataclass(frozen=True, slots=True, repr=False)
class ProviderOutput:
    type: str
    content: tuple[ProviderContent, ...]


@dataclass(frozen=True, slots=True, repr=False)
class ProviderResponse:
    response_id: str | None
    status: str
    output: tuple[ProviderOutput, ...]
    usage: ProviderUsage | None


@dataclass(frozen=True, slots=True, repr=False)
class TransportCall:
    request: OpenAIRequest
    timeout_seconds: float


class OpenAITransport(Protocol):
    def call(
        self, request: OpenAIRequest, *, timeout_seconds: float
    ) -> ProviderResponse: ...


class OpenAIResponsesTransport:
    """Official-SDK Responses adapter; construction never occurs implicitly."""

    def __init__(self, client: object):
        if client is None or not hasattr(client, "responses"):
            raise TypeError("an OpenAI SDK client is required")
        self._client = client

    def __repr__(self) -> str:
        return "OpenAIResponsesTransport()"

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "OpenAIResponsesTransport":
        """Explicitly cross the credential boundary; never called at import/startup."""

        environment = os.environ if environ is None else environ
        credential = environment.get("OPENAI_API_KEY")
        if not isinstance(credential, str) or not credential:
            raise TrustedTransportConfigurationError(
                "OpenAI transport credential is unavailable"
            )
        try:
            from openai import OpenAI
        except ImportError:
            raise TrustedTransportConfigurationError(
                "OpenAI SDK is unavailable"
            ) from None
        return cls(
            OpenAI(
                api_key=credential,
                base_url=OPENAI_API_BASE_URL,
                max_retries=0,
            )
        )

    def call(
        self, request: OpenAIRequest, *, timeout_seconds: float
    ) -> ProviderResponse:
        if type(request) is not OpenAIRequest:
            raise TypeError("request must be an OpenAIRequest")
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout must be positive and finite")
        try:
            raw = self._client.responses.create(
                **request.to_sdk_kwargs(), timeout=timeout_seconds
            )
        except Exception as error:
            if isinstance(error, OpenAITransportError):
                raise
            raise _sanitized_sdk_error(error) from None
        try:
            return _normalize_response(raw)
        except (AttributeError, KeyError, TypeError, ValueError):
            raise OpenAITransportError(
                error_code="malformed_provider_response"
            ) from None


class FakeOpenAITransport:
    """Deterministic offline transport used by unit and integration tests."""

    def __init__(self, outcomes: Sequence[ProviderResponse | OpenAITransportError]):
        self._outcomes = list(outcomes)
        self.calls: list[TransportCall] = []

    def call(
        self, request: OpenAIRequest, *, timeout_seconds: float
    ) -> ProviderResponse:
        self.calls.append(TransportCall(request, timeout_seconds))
        if not self._outcomes:
            raise OpenAITransportError(error_code="fake_schedule_exhausted")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, OpenAITransportError):
            raise outcome
        if type(outcome) is not ProviderResponse:
            raise OpenAITransportError(error_code="malformed_fake_response")
        return outcome


def _normalize_response(raw: object) -> ProviderResponse:
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(mode="json")
    if type(raw) is not dict:
        raise ValueError("malformed response")
    response_id = raw.get("id")
    if response_id is not None:
        response_id = _optional_provider_token(response_id)
        if response_id is None:
            raise ValueError("malformed response id")
    status = _provider_token(raw.get("status"))
    raw_output = raw.get("output")
    if type(raw_output) is not list:
        raise ValueError("malformed output")
    output = tuple(_normalize_output_item(item) for item in raw_output)
    usage = _normalize_usage(raw.get("usage"))
    return ProviderResponse(response_id, status, output, usage)


def _normalize_output_item(raw: object) -> ProviderOutput:
    if type(raw) is not dict:
        raise ValueError("malformed output item")
    output_type = _provider_token(raw.get("type"))
    raw_content = raw.get("content", [])
    if output_type == "reasoning" and raw_content is None:
        raw_content = []
    if type(raw_content) is not list:
        raise ValueError("malformed content")
    content = tuple(_normalize_content(item) for item in raw_content)
    return ProviderOutput(output_type, content)


def _normalize_content(raw: object) -> ProviderContent:
    if type(raw) is not dict:
        raise ValueError("malformed response content")
    content_type = _provider_token(raw.get("type"))
    if content_type == "output_text":
        text = raw.get("text")
        if not isinstance(text, str):
            text = None
    else:
        text = None
    return ProviderContent(content_type, text)


def _normalize_usage(raw: object) -> ProviderUsage | None:
    if raw is None:
        return None
    if type(raw) is not dict:
        raise ValueError("malformed usage")
    details = raw.get("input_tokens_details")
    if details is not None and type(details) is not dict:
        raise ValueError("malformed usage details")
    usage = ProviderUsage(
        _optional_nonnegative_integer(raw.get("input_tokens")),
        _optional_nonnegative_integer(
            details.get("cached_tokens") if details is not None else None
        ),
        _optional_nonnegative_integer(raw.get("output_tokens")),
        _optional_nonnegative_integer(raw.get("total_tokens")),
    )
    if (
        usage.input_tokens is not None
        and usage.cached_input_tokens is not None
        and usage.cached_input_tokens > usage.input_tokens
    ):
        raise ValueError("malformed usage")
    if (
        usage.input_tokens is not None
        and usage.output_tokens is not None
        and usage.total_tokens is not None
        and usage.total_tokens != usage.input_tokens + usage.output_tokens
    ):
        raise ValueError("malformed usage")
    return usage


def _optional_nonnegative_integer(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_PROVIDER_TOKEN_COUNT:
        raise ValueError("malformed usage value")
    return value


def _sanitized_sdk_error(error: Exception) -> OpenAITransportError:
    status_code = getattr(error, "status_code", None)
    if type(status_code) is not int:
        status_code = None
    body = getattr(error, "body", None)
    if type(body) is dict and type(body.get("error")) is dict:
        body = body["error"]
    if type(body) is not dict:
        body = {}
    error_type = _optional_provider_token(body.get("type"))
    error_code = _optional_provider_token(body.get("code"))
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {})
    retry_after = None
    if isinstance(headers, Mapping):
        try:
            candidate = float(headers.get("retry-after"))
        except (TypeError, ValueError, OverflowError):
            candidate = None
        if (
            candidate is not None
            and math.isfinite(candidate)
            and 0 < candidate <= MAX_RETRY_AFTER_SECONDS
        ):
            retry_after = candidate
    name = type(error).__name__
    return OpenAITransportError(
        status_code=status_code,
        error_type=error_type,
        error_code=error_code,
        retry_after_seconds=retry_after,
        timeout=name == "APITimeoutError",
        connection=name == "APIConnectionError",
    )


def _provider_token(value: object) -> str:
    token = _optional_provider_token(value)
    if token is None:
        raise ValueError("malformed provider token")
    return token


def _optional_provider_token(value: object) -> str | None:
    if not isinstance(value, str) or _PROVIDER_TOKEN.fullmatch(value) is None:
        return None
    return value
