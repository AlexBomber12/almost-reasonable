"""Fixture-only Stage 0 research endpoint; no SDK, credentials or paid transport."""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import hmac
import re
import secrets
import socket
import time

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
from app.research.authorization import (
    ResearchAuthorization,
    SyntheticPublicResearchFixture,
    authorize_synthetic_public_research,
)
from app.research.brief import (
    ResearchBrief,
    ResearchQuestion,
    ResearchRecordSet,
    ResearchScreeningContext,
    _screen,
    build_safe_research_brief,
)
from app.research.provenance import SourceStatus
from app.research.provider import (
    FakeHostedSearchTransport,
    HostedSearchTransportError,
    default_hosted_search_config,
)
from app.research.service import ResearchOutcome, ResearchService
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


RESEARCH_WORKER_VERSION = "research-worker-stage0-v1"
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
MAX_RESEARCH_REPLY_BYTES = 24 * 1024
_FAILURE_RU = "Исследование недоступно; проверенные выводы не сформированы."
_UNCERTAINTY_RU = (
    "Ссылки проверены только структурно; фактическая достоверность не оценена."
)
_OUTCOMES = {outcome.value for outcome in ResearchOutcome} | {
    "invalid_ipc_request",
    "duplicate_operation",
    "persistence_failure",
    "output_gate_rejected",
}
_REQUEST_FIELDS = {"version", "operation", "fixture", "authorization"}


@dataclass(frozen=True, slots=True, repr=False)
class ResearchWorkerRequest:
    fixture: SyntheticPublicResearchFixture
    authorization: str = field(repr=False)


def encode_research_request(request: ResearchWorkerRequest) -> bytes:
    if type(request) is not ResearchWorkerRequest:
        raise TypeError("request must be a ResearchWorkerRequest")
    if type(request.fixture) is not SyntheticPublicResearchFixture or (
        type(request.authorization) is not str
        or _TOKEN.fullmatch(request.authorization) is None
    ):
        raise WorkerProtocolError("invalid research request")
    return _encode_json(
        {
            "version": RESEARCH_WORKER_VERSION,
            "operation": "synthetic_public_research",
            "fixture": request.fixture.value,
            "authorization": request.authorization,
        }
    )


def decode_research_request(payload: bytes) -> ResearchWorkerRequest:
    value = _decode_json(payload)
    _require_exact_dict(value, _REQUEST_FIELDS)
    if (
        value["version"] != RESEARCH_WORKER_VERSION
        or value["operation"] != "synthetic_public_research"
    ):
        raise WorkerProtocolError("unsupported research operation")
    try:
        request = ResearchWorkerRequest(
            SyntheticPublicResearchFixture(value["fixture"]), value["authorization"]
        )
        encode_research_request(request)
    except (TypeError, ValueError):
        raise WorkerProtocolError("invalid research request") from None
    return request


def decode_research_response(payload: bytes) -> dict[str, object]:
    """Return bounded untrusted data, never deserialize authorization/source seals."""
    value = _decode_json(payload)
    _require_exact_dict(value, {"version", "outcome", "attempts", "message", "report"})
    if (
        len(payload) > MAX_RESEARCH_REPLY_BYTES
        or value["version"] != RESEARCH_WORKER_VERSION
        or type(value["outcome"]) is not str
        or value["outcome"] not in _OUTCOMES
        or type(value["attempts"]) is not int
        or not 0 <= value["attempts"] <= 2
    ):
        raise WorkerProtocolError("invalid research response")
    # JSON escapes can decode to unpaired surrogates. Validate all strings with
    # the existing bounded serializer before any field-specific UTF-8 sizing.
    _encode_json(value)
    report = value["report"]
    if value["outcome"] != "completed":
        if report is not None or value["message"] != _FAILURE_RU:
            raise WorkerProtocolError("invalid research failure")
        return value
    if value["message"] is not None:
        raise WorkerProtocolError("invalid research response")
    _require_exact_dict(
        report,
        {"main_finding", "uncertainty", "claim_status", "semantic_support", "sources"},
    )
    if (
        report["claim_status"] != "unverified"
        or report["semantic_support"] != "not_evaluated"
        or report["uncertainty"] != _UNCERTAINTY_RU
        or type(report["main_finding"]) is not str
        or not report["main_finding"].strip()
        or len(report["main_finding"].encode("utf-8")) > 16 * 1024
        or type(report["sources"]) is not list
        or not 1 <= len(report["sources"]) <= 3
    ):
        raise WorkerProtocolError("invalid research report")
    for source in report["sources"]:
        _require_exact_dict(
            source, {"reference", "url", "status", "metadata_validated"}
        )
        if (
            type(source["reference"]) is not str
            or len(source["reference"]) > 1024
            or type(source["status"]) is not str
            or source["status"]
            not in {s.value for s in SourceStatus if s is not SourceStatus.VERIFIED}
            or type(source["metadata_validated"]) is not bool
            or source["metadata_validated"] != (source["status"] == "recorded")
            or (
                source["url"] is not None
                and (type(source["url"]) is not str or len(source["url"]) > 4096)
            )
        ):
            raise WorkerProtocolError("invalid research source")
    return value


def _response(outcome: str, attempts: int = 0, report=None) -> dict[str, object]:
    return {
        "version": RESEARCH_WORKER_VERSION,
        "outcome": outcome,
        "attempts": attempts,
        "message": None if report is not None else _FAILURE_RU,
        "report": report,
    }


@dataclass(slots=True, repr=False)
class _FixtureGrant:
    request: ResearchWorkerRequest
    peer_uid: int
    brief: ResearchBrief
    authorization: ResearchAuthorization
    revoked: bool = False


class ResearchWorkerBoundary:
    """Trusted bootstrap installs at most two local grants, bound to OS peer UIDs.

    IPC supplies only a random lookup handle. No JSON object becomes authority.
    Fixed durable fixture identities cannot be rearmed by reissuing a handle.
    """

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
        sleeper=time.sleep,
        jitter=lambda: 0.5,
    ):
        if (
            type(store) is not ProviderStateStore
            or type(transport) is not FakeHostedSearchTransport
        ):
            raise TypeError("Stage 0 requires exact store and fake transport types")
        if type(enabled) is not bool or type(screening) is not ResearchScreeningContext:
            raise TypeError("invalid trusted research configuration")
        self._store = store
        self._transport = transport
        self._config = replace(
            default_hosted_search_config(), enabled=enabled, external_web_access=False
        )
        self._screening = screening
        self._stop = emergency_stop
        self._utcnow = utcnow
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._jitter = jitter
        self._grants: dict[SyntheticPublicResearchFixture, _FixtureGrant] = {}
        self._persistence_failed = False

    @staticmethod
    def operation_id(fixture: SyntheticPublicResearchFixture) -> str:
        return f"research-stage0:{fixture.value}"

    def authorize_fixture(
        self, fixture, *, peer_uid, issued_at=None
    ) -> ResearchWorkerRequest:
        """Trusted local bootstrap only; no issuer is exposed through the socket."""
        if (
            type(fixture) is not SyntheticPublicResearchFixture
            or type(peer_uid) is not int
            or not 0 <= peer_uid <= MAX_LINUX_UID
        ):
            raise ValueError("invalid synthetic fixture grant")
        built = build_safe_research_brief(
            ResearchRecordSet((ResearchQuestion("How do auroral substorms form?"),)),
            result_budget=1
            if fixture is SyntheticPublicResearchFixture.AURORAL_SUBSTORMS_SINGLE_RESULT
            else 3,
            trusted_config_version="research-config-v1",
            screening=self._screening,
        )
        if built.brief is None:
            raise ValueError("synthetic fixture failed screening")
        authorization = authorize_synthetic_public_research(
            fixture,
            built.brief,
            provider_config_version=self._config.version,
            issued_at=self._utcnow() if issued_at is None else issued_at,
        )
        request = ResearchWorkerRequest(fixture, secrets.token_hex(32))
        self._grants[fixture] = _FixtureGrant(
            request, peer_uid, built.brief, authorization
        )
        return request

    def revoke_fixture(self, fixture: SyntheticPublicResearchFixture) -> None:
        self._grants[fixture].revoked = True

    def _guard(self, grant: _FixtureGrant) -> str | None:
        if (
            grant.revoked
            or self._grants.get(grant.request.fixture) is not grant
            or not grant.authorization.permits(
                grant.brief,
                provider_config_version=self._config.version,
                now=self._utcnow(),
            )
        ):
            return "authorization_denied"
        if not self._config.enabled:
            return "disabled"
        if self._stop():
            return "emergency_stop"
        if self._store.is_spend_suspended():
            return "spend_suspended"
        return None

    def execute(
        self, request: ResearchWorkerRequest, *, peer_uid: int
    ) -> dict[str, object]:
        encode_research_request(request)
        grant = self._grants.get(request.fixture)
        if (
            grant is None
            or peer_uid != grant.peer_uid
            or not hmac.compare_digest(
                request.authorization, grant.request.authorization
            )
        ):
            return _response("authorization_denied")
        if self._persistence_failed:
            return _response("persistence_failure")
        try:
            blocked = self._guard(grant)
            if blocked is not None:
                return _response(blocked)
            operation_id = self.operation_id(request.fixture)
            reservation = self._store.reserve_attempt(
                operation_id, operation_id, self._utcnow()
            )
            if reservation is not AttemptReservationStatus.CREATED:
                return _response(
                    "spend_suspended"
                    if reservation is AttemptReservationStatus.SPEND_SUSPENDED
                    else "duplicate_operation"
                )
            guarded = _DurableFakeResearchTransport(self, grant, operation_id)
            started = self._utcnow()
            began = self._monotonic()
            response = self._run(grant, guarded)
            guarded.record_operation(started, began, response["outcome"])
            return response
        except ProviderPersistenceError:
            self._persistence_failed = True
            return _response("persistence_failure")
        except (TypeError, ValueError):
            return _response("invalid_response")

    def _run(self, grant, guarded):
        try:
            result = ResearchService(
                config=self._config,
                transport=guarded,
                emergency_stop=self._stop,
                spend_suspended=self._store.is_spend_suspended,
                utcnow=self._utcnow,
                monotonic=self._monotonic,
                sleeper=self._sleeper,
                jitter=self._jitter,
            ).run(grant.brief, grant.authorization)
            if result.outcome is not ResearchOutcome.COMPLETED:
                return _response(result.outcome.value, guarded.attempts)
            blocked = self._guard(grant)
            if blocked is not None:
                return _response(blocked, guarded.attempts)
            evidence = result.evidence
            values = [evidence.text, evidence.response_id]
            for source in evidence.sources:
                values.extend(
                    value
                    for value in (source.reference, source.url, source.title)
                    if value is not None
                )
            if _screen(values, self._screening) is not None:
                return _response("invalid_response", guarded.attempts)
            # No model-controlled references are accepted as semantic support.
            synthesis = build_russian_synthesis(
                ResearchSynthesisDraft(
                    evidence.text, (), _UNCERTAINTY_RU, (), (evidence.text,)
                ),
                evidence.sources,
            )
            delivery = RussianOutputPolicy(max_regenerations=0).deliver(
                synthesis.main_finding
            )
            if delivery.used_fallback:
                return _response("output_gate_rejected", guarded.attempts)
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
            response = _response("completed", guarded.attempts, report)
            # Revalidate the response bounds before returning across IPC.
            decode_research_response(_encode_json(response))
            return response
        except (TypeError, ValueError):
            return _response("invalid_response", guarded.attempts)


class _DurableFakeResearchTransport:
    """Recheck state after reservation and record only closed technical evidence."""

    def __init__(self, worker, grant, operation_id):
        self.worker = worker
        self.grant = grant
        self.operation_id = operation_id
        self.attempts = 0

    def call(self, request, *, timeout_seconds):
        worker = self.worker
        began = worker._monotonic()
        blocked = worker._guard(self.grant)
        if blocked is not None:
            raise HostedSearchTransportError(blocked)
        attempt_id = f"{self.operation_id}:{self.attempts + 1}"
        started = worker._utcnow()
        reservation = worker._store.reserve_attempt(
            attempt_id, self.operation_id, started
        )
        if reservation is not AttemptReservationStatus.CREATED:
            raise HostedSearchTransportError(
                "spend_suspended"
                if reservation is AttemptReservationStatus.SPEND_SUSPENDED
                else "invalid_request"
            )
        blocked = worker._guard(self.grant)
        if blocked is not None:
            raise HostedSearchTransportError(blocked)
        remaining = timeout_seconds - (worker._monotonic() - began)
        if remaining <= 0:
            raise HostedSearchTransportError("timeout", retry_after_seconds=999)
        self.attempts += 1
        self._call_started = worker._monotonic()
        confirmed = None
        try:
            response = worker._transport.call(request, timeout_seconds=remaining)
        except HostedSearchTransportError as error:
            if error.code in CONFIRMED_SPEND_LIMIT_CODES:
                confirmed = error.code
                code = "spend_limit_confirmed"
            else:
                code = (
                    error.code
                    if type(error.code) is str
                    and error.code
                    in {
                        "rate_limit",
                        "server_error",
                        "timeout",
                        "connection_error",
                        "authentication_error",
                        "permission_error",
                        "invalid_request",
                        "billing_quota",
                    }
                    else "provider_error"
                )
            self._record(attempt_id, started, code, confirmed)
            raise HostedSearchTransportError(
                code, retry_after_seconds=error.retry_after_seconds
            ) from None
        self._record(attempt_id, started, "completed", None)
        return response

    def record_operation(self, started, began, outcome):
        # Operation telemetry has a distinct ID from provider attempts.
        self._call_started = began
        self._record(self.operation_id, started, outcome, None)

    def _record(self, attempt_id, started, outcome, confirmed):
        record = ProviderAttemptRecord(
            attempt_id,
            self.operation_id,
            "openai",
            self.worker._config.model,
            self.worker._config.version,
            RESEARCH_WORKER_VERSION,
            "research-no-core-self-v1",
            None,
            started,
            max(0, int((self.worker._monotonic() - self._call_started) * 1000)),
            None,
            None,
            None,
            None,
            outcome,
            max(0, self.attempts - 1),
            None,
            None,
            None,
            CostEstimateStatus.USAGE_UNAVAILABLE,
            confirmed,
        )
        if not self.worker._store.record_attempt(
            record, confirmed_spend_limit_code=confirmed, require_reservation=True
        ):
            raise ProviderPersistenceError("research evidence persistence failed")


class ResearchWorkerServer(OpenAIWorkerServer):
    """Separate endpoint reusing the existing bounded socket lifecycle only."""

    def __init__(self, config: WorkerServerConfig, boundary: ResearchWorkerBoundary):
        if type(boundary) is not ResearchWorkerBoundary:
            raise TypeError("boundary must be a ResearchWorkerBoundary")
        # The inherited infer handler is replaced; this gateway is always inert.
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
            authorized = peer_uid in self._config.permitted_peer_uids
            if self._config.log_peer_uids:
                import sys

                print(
                    f"research-worker peer uid={peer_uid} authorized={str(authorized).lower()}",
                    file=sys.stderr,
                    flush=True,
                )
            if not authorized:
                return
            deadline = time.monotonic() + self._config.connection_lifetime_seconds
            try:
                request = decode_research_request(
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
