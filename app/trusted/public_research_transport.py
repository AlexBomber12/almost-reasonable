"""Explicit, fixed-destination hosted-search adapter for the trusted worker."""

import math

from app.research.provider import (
    HostedSearchRequest,
    HostedSearchResponse,
    HostedSearchTransportError,
)
from app.research.provenance import MAX_PROVIDER_RESPONSE_UTF8_BYTES
from app.trusted.openai_credential_files import build_worker_openai_transport
from app.trusted.openai_transport import OpenAITransportError, _sanitized_sdk_error
from app.core.provider_spend import CONFIRMED_SPEND_LIMIT_CODES


class PublicResearchTransport:
    """A worker-owned SDK client; callers supply only a sealed search request."""

    def __init__(self, client: object):
        if client is None or not hasattr(client, "responses"):
            raise TypeError("OpenAI Responses client required")
        self._client = client
        self.last_usage: dict[str, int] | None = None

    @classmethod
    def from_protected_file(cls, *, project_id: str, organization_id: str):
        transport = build_worker_openai_transport(
            project_id=project_id, organization_id=organization_id
        )
        return cls(transport._client)

    def call(self, request: HostedSearchRequest, *, timeout_seconds: float):
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
        self.last_usage = None
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
            import json

            encoded = json.dumps(raw, ensure_ascii=False).encode("utf-8")
            if len(encoded) > MAX_PROVIDER_RESPONSE_UTF8_BYTES:
                raise ValueError("oversized response")
            usage = raw.get("usage")
            if usage is not None:
                if type(usage) is not dict or any(
                    type(usage.get(key)) is not int or not 0 <= usage[key] <= 10_000_000
                    for key in ("input_tokens", "output_tokens", "total_tokens")
                ):
                    raise ValueError("invalid usage")
                self.last_usage = {
                    key: usage[key]
                    for key in ("input_tokens", "output_tokens", "total_tokens")
                }
            output = raw["output"]
            if type(output) is not list:
                raise ValueError("invalid output")
            return HostedSearchResponse(raw["id"], raw["status"], tuple(output))
        except (KeyError, TypeError, ValueError, UnicodeError):
            self.last_usage = None
            raise HostedSearchTransportError("invalid_response") from None


def _error_code(error: OpenAITransportError) -> str:
    if error.error_code in CONFIRMED_SPEND_LIMIT_CODES:
        return error.error_code
    if error.timeout:
        return "timeout"
    if error.connection:
        return "connection_error"
    return {
        400: "invalid_request",
        401: "authentication_error",
        403: "permission_error",
        429: "rate_limit",
        500: "server_error",
        502: "server_error",
        503: "server_error",
        504: "server_error",
    }.get(error.status_code, "provider_error")
