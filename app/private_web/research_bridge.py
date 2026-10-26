"""Trusted, opt-in application handoff to the closed synthetic research worker.

Authority is installed locally for an existing authenticated input. Neither model
text nor HTTP data can issue it. Intent uses existing payload-free provider rows;
all result payloads belong to the existing reply store and its erasure graph.
"""

from dataclasses import dataclass, replace
from datetime import timedelta
import hashlib
import hmac
import secrets
import json
import os
from pathlib import Path
import socket
import stat
import time

from app.behavior.russian_gate import RussianOutputPolicy
from app.core.agent_proposals import MAX_PROPOSAL_TEXT_UTF8_BYTES
from app.core.provider_contract import CostEstimateStatus
from app.core.provider_spend import ProviderAttemptRecord, ProviderStateStore
from app.research.assessment import (
    assessment_russian_texts,
    validate_assessment,
    prose_urls_are_recorded,
)
from app.research.authorization import SyntheticPublicResearchFixture
from app.research.brief import ResearchScreeningContext, _screen
from app.research.provenance import _valid_https_url, _valid_identifier
from app.trusted.openai_worker_protocol import (
    WorkerProtocolError,
    receive_frame,
    send_frame,
)
from app.trusted.openai_worker_server import _peer_uid
from app.trusted.research_worker import (
    ResearchWorkerRequest,
    decode_research_response,
    encode_research_request,
)

VERSION = "private-research-stage0-v1"
PURPOSE = "synthetic_public_question"
FAILURE_RU = "Исследование недоступно; проверенные выводы не сформированы."
SUSPENDED_RU = "Исследование приостановлено; результат сейчас недоступен."


def started_id(operation_id):
    return "research-started:" + hashlib.sha256(operation_id.encode()).hexdigest()


def intent_id(operation_id):
    return "research-intent:" + hashlib.sha256(operation_id.encode()).hexdigest()


class SyntheticResearchClient:
    """Only fixture/handle bytes cross the authenticated, deadline-bounded socket."""

    def __init__(
        self,
        socket_path: Path,
        requests: tuple[ResearchWorkerRequest, ...],
        *,
        worker_state: ProviderStateStore,
    ):
        if not isinstance(socket_path, Path) or not socket_path.is_absolute():
            raise ValueError("research socket must be absolute")
        if type(requests) is not tuple or not 1 <= len(requests) <= 2:
            raise ValueError("invalid trusted worker provisioning")
        for request in requests:
            encode_research_request(request)
        if len({request.fixture for request in requests}) != len(requests):
            raise ValueError("duplicate fixture provisioning")
        if type(worker_state) is not ProviderStateStore:
            raise TypeError("research requires exact durable worker state")
        self._worker_state = worker_state
        self._path = socket_path
        self._requests = {request.fixture: request for request in requests}

    def execute(self, fixture):
        request = self._requests[fixture]
        # Reject path substitution and authenticate the server too. Trusted worker
        # bootstrap and this harness run under the same OS service identity.
        parent = self._path.parent.lstat()
        endpoint = self._path.lstat()
        if (
            self._path.parent.resolve(strict=True) != self._path.parent
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or parent.st_mode & 0o077
            or not stat.S_ISSOCK(endpoint.st_mode)
            or endpoint.st_uid != os.geteuid()
            or endpoint.st_mode & 0o077
        ):
            raise WorkerProtocolError("unsafe research endpoint")
        deadline = time.monotonic() + 80.0
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(1.0)
            connection.connect(str(self._path))
            if _peer_uid(connection) != os.geteuid():
                raise WorkerProtocolError("unexpected research peer")
            send_frame(
                connection,
                encode_research_request(request),
                deadline=deadline,
                io_timeout=10.0,
            )
            connection.shutdown(socket.SHUT_WR)
            payload = receive_frame(
                connection, deadline=deadline, io_timeout=10.0, wait_for_first_byte=True
            )
        return decode_research_response(payload)


@dataclass(frozen=True, slots=True, repr=False)
class _InputGrant:
    principal_id: str
    event_id: str
    operation_id: str
    fixture: SyntheticPublicResearchFixture
    purpose: str
    version: str
    issued_at: object
    expires_at: object
    integrity: bytes = b""


class SyntheticResearchBridge:
    """Application-owned exact-input grants, independent of worker authority."""

    def __init__(
        self,
        store,
        gate,
        client,
        *,
        enabled=False,
        clock,
        screening=ResearchScreeningContext(),
    ):
        if type(client) is not SyntheticResearchClient:
            raise TypeError("research requires the exact synthetic client")
        if (
            gate._store is not store
            or type(enabled) is not bool
            or type(screening) is not ResearchScreeningContext
        ):
            raise ValueError("invalid application research configuration")
        self._store, self._gate, self._client = store, gate, client
        self.enabled, self._clock = enabled, clock
        self._screening = screening
        self._grants = {}
        self._key = secrets.token_bytes(32)

    def authorize_input(self, principal, event_id, fixture):
        """Trusted bootstrap only; the private input must already exist."""
        from .runtime import Stage0ConversationRuntime

        if (
            type(fixture) is not SyntheticPublicResearchFixture
            or fixture not in self._client._requests
        ):
            raise ValueError("unknown application fixture")
        events = self._gate.retrieve_private_events(principal)
        event = next((item for item in events if item.event_id == event_id), None)
        if event is None:
            raise ValueError("unavailable authenticated input")
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("invalid grant clock")
        operation_id = Stage0ConversationRuntime._operation_id(event_id)
        grant = _InputGrant(
            principal.principal_id.value,
            event_id,
            operation_id,
            fixture,
            PURPOSE,
            VERSION,
            now,
            now + timedelta(minutes=10),
        )
        self._grants[operation_id] = replace(grant, integrity=self._seal(grant))

    def _seal(self, grant):
        fields = [
            grant.principal_id,
            grant.event_id,
            grant.operation_id,
            grant.fixture.value,
            grant.purpose,
            grant.version,
            grant.issued_at.isoformat(),
            grant.expires_at.isoformat(),
        ]
        return hmac.digest(self._key, json.dumps(fields).encode(), "sha256")

    def revoke_input(self, event_id):
        from .runtime import Stage0ConversationRuntime

        self._grants.pop(Stage0ConversationRuntime._operation_id(event_id), None)

    def permits(self, principal, operation, fixture):
        grant = self._grants.get(operation.operation_id)
        if type(grant) is not _InputGrant:
            return False
        try:
            sealed = hmac.compare_digest(grant.integrity, self._seal(grant))
        except (AttributeError, TypeError):
            return False
        return (
            self.enabled
            and sealed
            and grant.principal_id == principal.principal_id.value
            and grant.event_id == operation.initiating_event_id
            and grant.operation_id == operation.operation_id
            and grant.fixture == fixture
            and grant.purpose == PURPOSE
            and grant.version == VERSION
            and grant.issued_at <= self._clock() < grant.expires_at
        )

    @staticmethod
    def record(provider, operation, fixture, outcome, now, *, require_reservation=True):
        """Intent/result evidence contains identities and closed outcomes only."""
        worker_id = "research-stage0:" + (fixture.value if fixture else "denied")
        return provider.record_attempt(
            ProviderAttemptRecord(
                intent_id(operation.operation_id),
                worker_id,
                "openai",
                fixture.value if fixture else "unavailable",
                VERSION,
                VERSION,
                operation.core_self_version,
                None,
                now,
                0,
                None,
                None,
                None,
                None,
                outcome,
                0,
                None,
                None,
                None,
                CostEstimateStatus.USAGE_UNAVAILABLE,
                None,
            ),
            require_reservation=require_reservation,
        )


def render_report(report, screening=ResearchScreeningContext()):
    """Bound untrusted prose and preserve source views without factual approval."""
    sources = report["sources"]
    values = [report["main_finding"], report["uncertainty"]]
    for source in sources:
        values.extend(
            value for value in (source["reference"], source["url"]) if value is not None
        )
    if "assessment" in report:
        try:
            validate_assessment(
                report["assessment"], {source["reference"] for source in sources}
            )
        except (TypeError, ValueError):
            return None
        values.extend(assessment_russian_texts(report["assessment"]))
    if _screen(values, screening) is not None:
        return None
    references = [source["reference"] for source in sources]
    if len(set(references)) != len(references) or any(
        not _valid_identifier(source["reference"])
        or source["status"] != "recorded"
        or not source["metadata_validated"]
        or not _valid_https_url(source["url"])
        for source in sources
    ):
        return None
    prose = [report["main_finding"]]
    if "assessment" in report:
        prose.extend(assessment_russian_texts(report["assessment"]))
    if any(
        RussianOutputPolicy(max_regenerations=0).deliver(text).used_fallback
        for text in prose
    ):
        return None
    recorded_urls = {source["url"] for source in sources}
    if not prose_urls_are_recorded(prose, recorded_urls):
        return None
    metadata = {
        key: report[key] for key in ("claim_status", "semantic_support", "sources")
    }
    if "assessment" in report:
        metadata["assessment"] = report["assessment"]
    text = (
        report["main_finding"]
        + "\n\n"
        + report["uncertainty"]
        + "\nДанные источников и статусы проверки:\n```json\n"
        + json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        + "\n```"
    )
    if len(text.encode()) > MAX_PROPOSAL_TEXT_UTF8_BYTES:
        return None
    if RussianOutputPolicy(max_regenerations=0).deliver(text).used_fallback:
        return None
    return text
