"""Explicit, fixed-destination hosted-search adapter for the trusted worker."""

import math
import re
import threading

from app.research.provider import (
    HostedSearchRequest,
    HostedSearchResponse,
    HostedSearchTransportError,
)
from app.research.provenance import MAX_PROVIDER_RESPONSE_UTF8_BYTES
from app.trusted.openai_credential_files import build_worker_openai_transport
from app.trusted.openai_gateway import GatewayOutcome, _classify_error
from app.trusted.openai_transport import OpenAITransportError, _sanitized_sdk_error


_RECORDABLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def recordable_response_id(value: object) -> bool:
    return type(value) is str and _RECORDABLE_ID.fullmatch(value) is not None


class PublicResearchTransport:
    """A worker-owned SDK client; callers supply only a sealed search request."""

    def __init__(self, client: object):
        if client is None or not hasattr(client, "responses"):
            raise TypeError("OpenAI Responses client required")
        self._client = client
        self._attempt = threading.local()

    @property
    def last_usage(self) -> dict[str, int] | None:
        return getattr(self._attempt, "usage", None)

    @last_usage.setter
    def last_usage(self, value: dict[str, int] | None) -> None:
        self._attempt.usage = value

    @property
    def last_response_id(self) -> str | None:
        return getattr(self._attempt, "response_id", None)

    @last_response_id.setter
    def last_response_id(self, value: str | None) -> None:
        self._attempt.response_id = value

    @classmethod
    def from_protected_file(cls, *, project_id: str, organization_id: str):
        transport = build_worker_openai_transport(
            project_id=project_id, organization_id=organization_id
        )
        return cls(transport._client)

    def call(self, request: HostedSearchRequest, *, timeout_seconds: float):
        self.last_usage = None
        self.last_response_id = None
        if type(request) is not HostedSearchRequest:
            raise TypeError("sealed hosted-search request required")
        kwargs = request.to_sdk_kwargs()
        if (
            type(timeout_seconds) not in (float, int)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > 120
        ):
            raise ValueError("invalid research timeout")
        try:
            raw = self._client.responses.create(**kwargs, timeout=timeout_seconds)
        except Exception as error:
            sanitized = _sanitized_sdk_error(error)
            raise HostedSearchTransportError(
                _error_code(sanitized),
                retry_after_seconds=sanitized.retry_after_seconds,
            ) from None
        try:
            if hasattr(raw, "model_dump"):
                raw = raw.model_dump(mode="json")
            if type(raw) is not dict:
                raise ValueError("invalid response")
            self.last_response_id = (
                raw.get("id") if recordable_response_id(raw.get("id")) else None
            )
            import json

            usage = raw.get("usage")
            if usage is not None:
                if (
                    type(usage) is not dict
                    or any(
                        type(usage.get(key)) is not int
                        or not 0 <= usage[key] <= 10_000_000
                        for key in ("input_tokens", "output_tokens", "total_tokens")
                    )
                    or usage["total_tokens"]
                    != usage["input_tokens"] + usage["output_tokens"]
                ):
                    raise ValueError("invalid usage")
                self.last_usage = {
                    key: usage[key]
                    for key in ("input_tokens", "output_tokens", "total_tokens")
                }
            encoded = json.dumps(raw, ensure_ascii=False).encode("utf-8")
            if len(encoded) > MAX_PROVIDER_RESPONSE_UTF8_BYTES:
                raise ValueError("oversized response")
            output = raw["output"]
            if type(output) is not list:
                raise ValueError("invalid output")
            return HostedSearchResponse(raw["id"], raw["status"], tuple(output))
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise HostedSearchTransportError("invalid_response") from None


def _error_code(error: OpenAITransportError) -> str:
    outcome = _classify_error(error)
    if outcome is GatewayOutcome.SPEND_LIMIT_CONFIRMED:
        return error.error_code
    return (
        "provider_error" if outcome is GatewayOutcome.UNKNOWN_ERROR else outcome.value
    )
