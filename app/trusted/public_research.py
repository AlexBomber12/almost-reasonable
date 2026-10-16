"""Default-disabled, one-use public research operation in the trusted worker."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import math
import re
import secrets
import socket
import time
import unicodedata

from app.behavior.russian_gate import RussianOutputPolicy
from app.core.provider_contract import (
    CostEstimateStatus,
    default_openai_provider_config,
)
from app.core.provider_spend import (
    AttemptReservationStatus,
    CONFIRMED_SPEND_LIMIT_CODES,
    ProviderAttemptRecord,
    ProviderPersistenceError,
    ProviderStateStore,
)
from app.research.brief import (
    ResearchQuestion,
    ResearchRecordSet,
    ResearchScreeningContext,
    _screen,
    build_safe_research_brief,
)
from app.research.provenance import SourceStatus, parse_hosted_search_response
from app.research.provider import (
    HostedSearchRequest,
    _REQUEST_AUTHORITY,
    _request_integrity,
    _validate_config,
    default_hosted_search_config,
)
from app.research.synthesis import ResearchSynthesisDraft, build_russian_synthesis
from app.trusted.openai_gateway import TrustedOpenAIGateway
from app.trusted.openai_transport import FakeOpenAITransport
from app.trusted.openai_worker_protocol import (
    WorkerProtocolError,
    _decode_json,
    _encode_json,
    _require_exact_dict,
    receive_frame,
    send_frame,
)
from app.trusted.openai_worker_server import (
    MAX_LINUX_UID,
    OpenAIWorkerServer,
    WorkerServerConfig,
    _peer_uid,
)
from app.trusted.public_research_transport import (
    PublicResearchTransport,
    recordable_response_id,
)


PUBLIC_RESEARCH_VERSION = "public-research-v1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,120}\Z")
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_URL_LIKE = re.compile(
    r"(?i)(?<![\w])(?:[\w-]+\.)+[\w-]{2,}(?![\w])|[a-z][a-z0-9+.-]{1,30}:\S"
)
_FAILURE = "Исследование недоступно; проверенные выводы не сформированы."
_UNCERTAINTY = (
    "Ссылки проверены только структурно; фактическая достоверность не оценена."
)
_OUTCOMES = frozenset(
    (
        "completed",
        "authorization_denied",
        "disabled",
        "emergency_stop",
        "spend_suspended",
        "duplicate_operation",
        "timeout",
        "rate_limit",
        "billing_quota",
        "ambiguous_quota",
        "server_error",
        "connection_error",
        "authentication_error",
        "permission_error",
        "invalid_request",
        "spend_limit_confirmed",
        "provider_error",
        "invalid_response",
        "persistence_failure",
        "invalid_ipc_request",
        "output_gate_rejected",
    )
)


@dataclass(frozen=True, slots=True, repr=False)
class PublicResearchRequest:
    operation_id: str
    question: str
    authorization: str


def encode_public_request(request: PublicResearchRequest) -> bytes:
    if type(request) is not PublicResearchRequest:
        raise TypeError("invalid public research request")
    if (
        type(request.operation_id) is not str
        or _IDENTIFIER.fullmatch(request.operation_id) is None
        or type(request.question) is not str
        or not request.question.strip()
        or len(request.question.encode("utf-8")) > 2048
        or _question_has_url(request.question)
        or type(request.authorization) is not str
        or _TOKEN.fullmatch(request.authorization) is None
    ):
        raise WorkerProtocolError("invalid public research request")
    return _encode_json(
        {
            "version": PUBLIC_RESEARCH_VERSION,
            "operation": "public_research",
            "operation_id": request.operation_id,
            "question": request.question,
            "authorization": request.authorization,
        }
    )


def _question_has_url(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        character for character in normalized if unicodedata.category(character) != "Cf"
    )
    return (
        any(character in normalized for character in "/\\%&#")
        or _URL_LIKE.search(normalized) is not None
    )


def decode_public_request(payload: bytes) -> PublicResearchRequest:
    value = _decode_json(payload)
    _require_exact_dict(
        value, {"version", "operation", "operation_id", "question", "authorization"}
    )
    if (
        value["version"] != PUBLIC_RESEARCH_VERSION
        or value["operation"] != "public_research"
    ):
        raise WorkerProtocolError("unsupported public research operation")
    request = PublicResearchRequest(
        value["operation_id"], value["question"], value["authorization"]
    )
    encode_public_request(request)
    return request


def decode_public_response(payload: bytes) -> dict[str, object]:
    value = _decode_json(payload)
    _require_exact_dict(
        value, {"version", "outcome", "attempts", "message", "report", "usage"}
    )
    if (
        len(payload) > 24 * 1024
        or value["version"] != PUBLIC_RESEARCH_VERSION
        or type(value["outcome"]) is not str
        or value["outcome"] not in _OUTCOMES
        or type(value["attempts"]) is not int
        or value["attempts"] not in (0, 1)
    ):
        raise WorkerProtocolError("invalid public research response")
    _encode_json(value)
    usage = value["usage"]
    if usage is not None and (
        type(usage) is not dict
        or set(usage) != {"input_tokens", "output_tokens", "total_tokens"}
        or any(
            type(item) is not int or not 0 <= item <= 10_000_000
            for item in usage.values()
        )
        or usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]
    ):
        raise WorkerProtocolError("invalid public research usage")
    report = value["report"]
    if value["outcome"] != "completed":
        if report is not None or value["message"] != _FAILURE:
            raise WorkerProtocolError("invalid public research failure")
        return value
    if value["message"] is not None:
        raise WorkerProtocolError("invalid public research completion")
    _require_exact_dict(
        report,
        {"main_finding", "uncertainty", "claim_status", "semantic_support", "sources"},
    )
    if (
        type(report["main_finding"]) is not str
        or not report["main_finding"].strip()
        or len(report["main_finding"].encode("utf-8")) > 16 * 1024
        or report["uncertainty"] != _UNCERTAINTY
        or report["claim_status"] != "unverified"
        or report["semantic_support"] != "not_evaluated"
        or type(report["sources"]) is not list
        or not 1 <= len(report["sources"]) <= 3
    ):
        raise WorkerProtocolError("invalid public research report")
    for source in report["sources"]:
        _require_exact_dict(
            source, {"reference", "url", "status", "metadata_validated"}
        )
        if (
            type(source["reference"]) is not str
            or len(source["reference"]) > 1024
            or source["status"] != SourceStatus.RECORDED.value
            or type(source["metadata_validated"]) is not bool
            or source["metadata_validated"] != (source["status"] == "recorded")
            or (
                source["url"] is not None
                and (type(source["url"]) is not str or len(source["url"]) > 4096)
            )
        ):
            raise WorkerProtocolError("invalid public research source")
    return value


def _response(outcome, attempts=0, report=None, usage=None):
    return {
        "version": PUBLIC_RESEARCH_VERSION,
        "outcome": outcome,
        "attempts": attempts,
        "message": None if report is not None else _FAILURE,
        "report": report,
        "usage": usage,
    }


@dataclass(slots=True, repr=False)
class _Grant:
    request: PublicResearchRequest
    peer_uid: int
    brief: object
    digest: str
    config: object
    issued_at: datetime
    expires_at: datetime
    revoked: bool = False


class PublicResearchBoundary:
    """Synthetic installation is a trusted local act; there is no IPC issuer."""

    def __init__(
        self,
        *,
        store,
        transport,
        enabled=False,
        screening=ResearchScreeningContext(),
        emergency_stop=lambda: False,
        utcnow=lambda: datetime.now(UTC),
        monotonic=time.monotonic,
    ):
        if (
            type(store) is not ProviderStateStore
            or type(transport) is not PublicResearchTransport
        ):
            raise TypeError("invalid trusted public research dependencies")
        if type(enabled) is not bool or type(screening) is not ResearchScreeningContext:
            raise TypeError("invalid trusted public research configuration")
        self._store = store
        self._transport = transport
        self._config = replace(
            default_hosted_search_config(), enabled=enabled, max_attempts=1
        )
        _validate_config(self._config)
        self._screening = screening
        self._stop = emergency_stop
        self._utcnow = utcnow
        self._monotonic = monotonic
        self._grants = {}
        self._persistence_failed = False

    def install_synthetic_authorization(
        self,
        *,
        operation_id: str,
        question: str,
        peer_uid: int,
        expires_at: datetime,
    ) -> PublicResearchRequest:
        """Install one exact synthetic brief in a trusted test bootstrap only."""
        issued_at = self._utcnow()
        if (
            type(operation_id) is not str
            or _IDENTIFIER.fullmatch(operation_id) is None
            or type(peer_uid) is not int
            or not 0 <= peer_uid <= MAX_LINUX_UID
            or type(expires_at) is not datetime
            or expires_at.tzinfo is None
            or not issued_at < expires_at <= issued_at + timedelta(minutes=10)
        ):
            raise ValueError("invalid synthetic authorization")
        built = build_safe_research_brief(
            ResearchRecordSet((ResearchQuestion(question),)),
            result_budget=3,
            trusted_config_version="public-research-brief-v1",
            screening=self._screening,
        )
        if built.brief is None:
            raise ValueError("public brief declined")
        request = PublicResearchRequest(
            operation_id, built.brief.question, secrets.token_hex(32)
        )
        encode_public_request(request)
        digest = hashlib.sha256(built.brief.to_json().encode("utf-8")).hexdigest()
        self._grants[operation_id] = _Grant(
            request, peer_uid, built.brief, digest, self._config, issued_at, expires_at
        )
        return request

    def revoke(self, operation_id: str) -> None:
        grant = self._grants[operation_id]
        grant.revoked = True
        self._store.reserve_attempt(operation_id, operation_id, self._utcnow())

    def _guard(self, grant: _Grant) -> str | None:
        if (
            grant.revoked
            or self._grants.get(grant.request.operation_id) is not grant
            or not grant.issued_at <= self._utcnow() < grant.expires_at
            or grant.config != self._config
            or hashlib.sha256(grant.brief.to_json().encode("utf-8")).hexdigest()
            != grant.digest
        ):
            return "authorization_denied"
        if not self._config.enabled:
            return "disabled"
        if self._stop():
            return "emergency_stop"
        if self._store.is_spend_suspended():
            return "spend_suspended"
        return None

    def _screened_response_id(self, value: object) -> str | None:
        if not recordable_response_id(value):
            return None
        return value if _screen((value,), self._screening) is None else None

    def execute(
        self, request: PublicResearchRequest, *, peer_uid: int
    ) -> dict[str, object]:
        encode_public_request(request)
        grant = self._grants.get(request.operation_id)
        if (
            grant is None
            or peer_uid != grant.peer_uid
            or not hmac.compare_digest(
                request.authorization, grant.request.authorization
            )
            or request.question != grant.request.question
        ):
            return _response("authorization_denied")
        if self._persistence_failed:
            return _response("persistence_failure")
        admitted = False
        attempts = 0
        started_at = None
        began = None
        usage = None
        safe_response_id = None
        try:
            blocked = self._guard(grant)
            if blocked is not None:
                return _response(blocked)
            began = self._monotonic()
            started_at = self._utcnow()
            reservation = self._store.reserve_attempt(
                request.operation_id, request.operation_id, started_at
            )
            if reservation is not AttemptReservationStatus.CREATED:
                return _response(
                    "spend_suspended"
                    if reservation is AttemptReservationStatus.SPEND_SUSPENDED
                    else "duplicate_operation"
                )
            admitted = True

            def finish(result, *, response_id=None, code=None):
                return self._record_terminal(
                    request.operation_id,
                    result,
                    started_at,
                    began,
                    response_id=response_id,
                    code=code,
                )

            blocked = self._guard(grant)
            if blocked is not None:
                return finish(_response(blocked))
            provider_request = _build_request(grant.brief, self._config)
            if self._monotonic() - began >= self._config.timeout_seconds:
                return finish(_response("timeout"))
            blocked = self._guard(grant)
            if blocked is not None:
                return finish(_response(blocked))
            try:
                attempts = 1
                provider_result = self._transport.call(
                    provider_request, timeout_seconds=self._config.timeout_seconds
                )
            except Exception as error:
                from app.research.provider import HostedSearchTransportError

                if isinstance(error, HostedSearchTransportError):
                    usage = self._transport.last_usage
                    safe_response_id = self._screened_response_id(
                        self._transport.last_response_id
                    )
                    if error.code in CONFIRMED_SPEND_LIMIT_CODES:
                        return finish(
                            _response("spend_limit_confirmed", 1, usage=usage),
                            response_id=safe_response_id,
                            code=error.code,
                        )
                    return finish(
                        _response(
                            error.code if error.code in _OUTCOMES else "provider_error",
                            1,
                            usage=usage,
                        ),
                        response_id=safe_response_id,
                        code=error.code if error.code in _OUTCOMES else None,
                    )
                return finish(_response("provider_error", 1))
            usage = self._transport.last_usage
            safe_response_id = self._screened_response_id(
                self._transport.last_response_id
            )
            if self._monotonic() - began >= self._config.overall_timeout_seconds:
                return finish(
                    _response("timeout", 1, usage=usage),
                    response_id=safe_response_id,
                )
            blocked = self._guard(grant)
            if blocked is not None:
                return finish(
                    _response(blocked, 1, usage=usage),
                    response_id=safe_response_id,
                )
            evidence = parse_hosted_search_response(
                provider_result,
                retrieved_at=self._utcnow(),
                max_search_calls=self._config.max_search_calls,
                max_sources=3,
            )
            if any(
                source.status is not SourceStatus.RECORDED
                for source in evidence.sources
            ):
                return finish(
                    _response("invalid_response", 1, usage=usage),
                    response_id=safe_response_id,
                )
            values = [evidence.text, evidence.response_id]
            for source in evidence.sources:
                values.extend(
                    value
                    for value in (source.reference, source.url, source.title)
                    if value is not None
                )
            if _screen(values, self._screening) is not None:
                return finish(
                    _response("invalid_response", 1, usage=usage),
                    response_id=safe_response_id,
                )
            synthesis = build_russian_synthesis(
                ResearchSynthesisDraft(
                    evidence.text, (), _UNCERTAINTY, (), (evidence.text,)
                ),
                evidence.sources,
            )
            delivery = RussianOutputPolicy(max_regenerations=0).deliver(
                synthesis.main_finding
            )
            if delivery.used_fallback:
                return finish(
                    _response("output_gate_rejected", 1, usage=usage),
                    response_id=safe_response_id,
                )
            report = {
                "main_finding": delivery.text,
                "uncertainty": synthesis.uncertainty,
                "claim_status": "unverified",
                "semantic_support": "not_evaluated",
                "sources": [
                    {
                        "reference": source.reference,
                        "url": source.url,
                        "status": source.status.value,
                        "metadata_validated": source.is_sourced,
                    }
                    for source in synthesis.source_statuses
                ],
            }
            response = _response("completed", 1, report, usage)
            decode_public_response(_encode_json(response))
            return finish(response, response_id=safe_response_id)
        except ProviderPersistenceError:
            self._persistence_failed = True
            return _response("persistence_failure")
        except (TypeError, ValueError, UnicodeError):
            result = _response("invalid_response", attempts, usage=usage)
            if admitted:
                return self._record_terminal(
                    request.operation_id,
                    result,
                    started_at,
                    began,
                    response_id=safe_response_id,
                )
            return result

    def _record_terminal(
        self,
        operation_id,
        result,
        started_at,
        began,
        *,
        response_id=None,
        code=None,
    ):
        usage = result["usage"]
        try:
            elapsed = self._monotonic() - began
            if not math.isfinite(elapsed) or elapsed < 0:
                raise ValueError("invalid operation duration")
            record = ProviderAttemptRecord(
                operation_id,
                operation_id,
                "openai",
                self._config.model,
                self._config.version,
                PUBLIC_RESEARCH_VERSION,
                "research-no-core-self-v1",
                response_id,
                started_at,
                int(elapsed * 1000),
                usage["input_tokens"] if usage is not None else None,
                None,
                usage["output_tokens"] if usage is not None else None,
                usage["total_tokens"] if usage is not None else None,
                result["outcome"],
                0,
                None,
                None,
                None,
                CostEstimateStatus.PRICING_UNAVAILABLE
                if usage is not None
                else CostEstimateStatus.USAGE_UNAVAILABLE,
                code,
            )
            if not self._store.record_attempt(
                record,
                confirmed_spend_limit_code=code
                if code in CONFIRMED_SPEND_LIMIT_CODES
                else None,
                require_reservation=True,
            ):
                raise ProviderPersistenceError("research evidence persistence failed")
        except (ProviderPersistenceError, TypeError, ValueError):
            self._persistence_failed = True
            return _response("persistence_failure")
        return result


def _build_request(brief, config):
    _validate_config(config)
    request = HostedSearchRequest(
        config.model,
        brief.to_json(),
        config.external_web_access,
        config.search_context_size,
        config.max_output_tokens,
        config.max_search_calls,
        _REQUEST_AUTHORITY,
    )
    object.__setattr__(
        request,
        "_integrity",
        _request_integrity(
            request.model,
            request.input_json,
            request.external_web_access,
            request.search_context_size,
            request.max_output_tokens,
            request.max_search_calls,
        ),
    )
    if len(_encode_json(request.to_sdk_kwargs())) > 16 * 1024:
        raise ValueError("oversized provider request")
    return request


class PublicResearchServer(OpenAIWorkerServer):
    def __init__(self, config: WorkerServerConfig, boundary: PublicResearchBoundary):
        if type(boundary) is not PublicResearchBoundary:
            raise TypeError("public research boundary required")
        super().__init__(
            config,
            TrustedOpenAIGateway(
                store=boundary._store,
                transport=FakeOpenAITransport([]),
                config=default_openai_provider_config(),
            ),
        )
        self._boundary = boundary

    def _handle_connection(self, connection: socket.socket) -> None:
        with connection:
            try:
                peer_uid = _peer_uid(connection)
            except WorkerProtocolError:
                return
            if peer_uid not in self._config.permitted_peer_uids:
                return
            deadline = time.monotonic() + self._config.connection_lifetime_seconds
            try:
                request = decode_public_request(
                    receive_frame(
                        connection,
                        deadline=deadline,
                        io_timeout=self._config.io_timeout_seconds,
                    )
                )
                if (
                    deadline - time.monotonic()
                    < self._boundary._config.overall_timeout_seconds
                    + self._config.io_timeout_seconds
                ):
                    response = _response("timeout")
                else:
                    response = self._boundary.execute(request, peer_uid=peer_uid)
            except (TypeError, ValueError):
                response = _response("invalid_ipc_request")
            try:
                send_frame(
                    connection,
                    _encode_json(response),
                    deadline=deadline,
                    io_timeout=self._config.io_timeout_seconds,
                )
                connection.shutdown(socket.SHUT_WR)
            except (OSError, WorkerProtocolError):
                pass
