"""Versioned, bounded JSON protocol for the local OpenAI worker."""

from dataclasses import dataclass, field
import json
import math
import socket
import struct
import time

from app.core.agent_proposals import (
    AgentDecision,
    NoActionProposal,
    ProposalValidation,
    PublicationProposal,
    ReplyProposal,
    ResearchProposal,
    parse_agent_proposal,
)
from app.core.context import ContextRole, ContextTrust, ConversationSegment
from app.cognition.conversation_context import MemoryModelRequest, ModelMemory
from app.cognition.shared import SharedRecord
from app.cognition.shared_context import SharedModelRequest
from app.core.model_contract import (
    MAX_MODEL_REQUEST_UTF8_BYTES,
    MODEL_CONTRACT_VERSION,
    ModelRequest,
    ModelTask,
    serialize_model_request,
)
from app.core.provider_contract import (
    MAX_ATTEMPTS,
    MAX_OVERALL_TIMEOUT_SECONDS,
    require_identifier,
)


PROTOCOL_VERSION = "openai-worker-v1"
CONTEXT_PROTOCOL_VERSION = "openai-worker-context-v2"
TRANSCRIPT_PROTOCOL_VERSION = "openai-worker-transcript-v3"
OPERATION_INFER = "infer"
FRAME_PREFIX_BYTES = 4
MAX_IPC_FRAME_BYTES = MAX_MODEL_REQUEST_UTF8_BYTES + 64 * 1024
MAX_SAFE_MESSAGE_UTF8_BYTES = 512
MAX_WORKER_OPERATION_ID_LENGTH = 126
MAX_JSON_NESTING_DEPTH = 16
MAX_IO_TIMEOUT_SECONDS = 10.0
MIN_CONNECTION_LIFETIME_SECONDS = (
    MAX_OVERALL_TIMEOUT_SECONDS + 2 * MAX_IO_TIMEOUT_SECONDS
)
MAX_CONNECTION_LIFETIME_SECONDS = (
    MAX_OVERALL_TIMEOUT_SECONDS + 4 * MAX_IO_TIMEOUT_SECONDS
)

IPC_REJECTED = "invalid_ipc_request"
_GATEWAY_OUTCOMES = frozenset(
    (
        "completed",
        "disabled",
        "emergency_stop",
        "spend_suspended",
        "duplicate_operation",
        "refused",
        "incomplete",
        "missing_output",
        "oversized_output",
        "unsupported_output",
        "invalid_response",
        "invalid_proposal",
        "rate_limit",
        "spend_limit_confirmed",
        "billing_quota",
        "ambiguous_quota",
        "authentication_error",
        "invalid_request",
        "permission_error",
        "server_error",
        "timeout",
        "connection_error",
        "unknown_error",
        "persistence_failure",
        "authorization_rejected",
    )
)
REMOTE_OUTCOMES = _GATEWAY_OUTCOMES | frozenset((IPC_REJECTED,))


class WorkerProtocolError(ValueError):
    """A sanitized rejection that never includes peer-controlled payload data."""


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    operation_id: str
    correlation_id: str
    model_request: ModelRequest | MemoryModelRequest | SharedModelRequest = field(
        repr=False
    )


@dataclass(frozen=True, slots=True)
class WorkerResponse:
    outcome: str
    attempts: int
    safe_russian_message: str | None
    validation: ProposalValidation | None = field(default=None, repr=False)


def encode_request(request: InferenceRequest) -> bytes:
    """Serialize the exact request allowlist after revalidating its model input."""

    if type(request) is not InferenceRequest:
        raise TypeError("request must be an InferenceRequest")
    require_identifier("operation id", request.operation_id)
    if len(request.operation_id) > MAX_WORKER_OPERATION_ID_LENGTH:
        raise ValueError("invalid operation id")
    require_identifier("correlation id", request.correlation_id)
    model = request.model_request
    if type(model) is ModelRequest:
        model_json = serialize_model_request(model)
        if model.contract_version != MODEL_CONTRACT_VERSION:
            raise ValueError("transcript requires context wrapper")
        version = PROTOCOL_VERSION
    elif type(model) in {MemoryModelRequest, SharedModelRequest}:
        model_json = model.serialize()
        version = (
            TRANSCRIPT_PROTOCOL_VERSION
            if json.loads(model_json)["contract_version"]
            == "model-contract-conversation-transcript-v4"
            else CONTEXT_PROTOCOL_VERSION
        )
    else:
        raise TypeError("unsupported model request")
    payload = {
        "protocol_version": version,
        "operation_type": OPERATION_INFER,
        "operation_id": request.operation_id,
        "correlation_id": request.correlation_id,
        "model_request": json.loads(model_json),
    }
    return _encode_json(payload)


def decode_request(payload: bytes) -> InferenceRequest:
    """Parse untrusted bytes into narrow typed values and rerun model validation."""

    value = _decode_json(payload)
    _require_exact_dict(
        value,
        {
            "protocol_version",
            "operation_type",
            "operation_id",
            "correlation_id",
            "model_request",
        },
    )
    if value["protocol_version"] not in {
        PROTOCOL_VERSION,
        CONTEXT_PROTOCOL_VERSION,
        TRANSCRIPT_PROTOCOL_VERSION,
    }:
        raise WorkerProtocolError("unsupported protocol version")
    if value["operation_type"] != OPERATION_INFER:
        raise WorkerProtocolError("unsupported operation")
    try:
        operation_id = require_identifier("operation id", value["operation_id"])
        if len(operation_id) > MAX_WORKER_OPERATION_ID_LENGTH:
            raise ValueError("invalid operation id")
        correlation_id = require_identifier("correlation id", value["correlation_id"])
        if value["protocol_version"] == PROTOCOL_VERSION:
            if (
                type(value["model_request"]) is not dict
                or value["model_request"].get("contract_version") != "model-contract-v1"
            ):
                raise WorkerProtocolError("invalid inference request")
            model_request = _parse_model_request(value["model_request"])
            serialize_model_request(model_request)
        else:
            model_request = _parse_context_request(
                value["model_request"], value["protocol_version"]
            )
    except (TypeError, ValueError):
        raise WorkerProtocolError("invalid inference request") from None
    return InferenceRequest(operation_id, correlation_id, model_request)


def _parse_context_request(
    value: object, protocol_version: str
) -> MemoryModelRequest | SharedModelRequest:
    if type(value) is not dict:
        raise WorkerProtocolError("invalid context request")
    transcript = protocol_version == TRANSCRIPT_PROTOCOL_VERSION
    shared = "shared_intellectual_state" in value or "shared_omitted" in value
    if not transcript:
        shared = (
            value.get("contract_version") == "model-contract-shared-intelligence-v3"
        )
    base_fields = {
        "contract_version",
        "task",
        "core_self",
        "conversation",
        "history_omitted",
        "allowed_decisions",
        "memories",
        "memories_omitted",
    }
    _require_exact_dict(
        value,
        base_fields | {"shared_intellectual_state", "shared_omitted"}
        if shared
        else base_fields,
    )
    expected = (
        "model-contract-conversation-transcript-v4"
        if transcript
        else "model-contract-shared-intelligence-v3"
        if shared
        else "model-contract-private-memory-v2"
    )
    if value["contract_version"] != expected:
        raise WorkerProtocolError("invalid context contract")
    base = {key: value[key] for key in base_fields - {"memories", "memories_omitted"}}
    base["contract_version"] = (
        "model-contract-conversation-transcript-v4"
        if transcript
        else "model-contract-v1"
    )
    conversation = _parse_model_request(base)
    memories = value["memories"]
    if type(memories) is not list:
        raise WorkerProtocolError("invalid memories")
    private = MemoryModelRequest(
        conversation,
        tuple(_parse_memory(item) for item in memories),
        value["memories_omitted"],
    )
    if shared:
        records = value["shared_intellectual_state"]
        if type(records) is not list:
            raise WorkerProtocolError("invalid shared records")
        request = SharedModelRequest(
            private,
            tuple(_parse_shared(item) for item in records),
            value["shared_omitted"],
        )
    else:
        request = private
    request.serialize()
    return request


def _parse_memory(value: object) -> ModelMemory:
    fields = {
        "kind",
        "text",
        "status",
        "confidence",
        "stance",
        "priority",
        "revision_reason",
        "supporting_evidence_count",
        "counterevidence_count",
        "reason_for_interest",
        "alternatives",
        "rationale",
        "constraints",
        "review_condition",
        "verification",
        "provenance",
        "trust",
        "can_instruct",
    }
    _require_exact_dict(value, fields)
    if value["trust"] != "untrusted_memory_data" or value["can_instruct"] is not False:
        raise WorkerProtocolError("invalid memory trust")
    if (
        type(value["alternatives"]) is not list
        or type(value["constraints"]) is not list
    ):
        raise WorkerProtocolError("invalid memory collections")
    return ModelMemory(
        value["kind"],
        value["text"],
        value["status"],
        value["confidence"],
        value["stance"],
        value["priority"],
        value["revision_reason"],
        value["supporting_evidence_count"],
        value["counterevidence_count"],
        value["reason_for_interest"],
        tuple(value["alternatives"]),
        value["rationale"],
        tuple(value["constraints"]),
        value["review_condition"],
        value["verification"],
        value["provenance"],
    )


def _parse_shared(value: object) -> SharedRecord:
    if type(value) is not dict:
        raise WorkerProtocolError("invalid shared record")
    _require_exact_dict(
        value,
        set(ModelMemory.__dataclass_fields__)
        | {"trust", "can_instruct", "scope", "revision", "kind"},
    )
    if value["scope"] != "approved_shared_abstract":
        raise WorkerProtocolError("invalid shared scope")
    memory = _parse_memory(
        {key: item for key, item in value.items() if key not in {"scope", "revision"}}
    )
    return SharedRecord("worker_projection", memory, value["revision"], value["kind"])


def encode_response(response: WorkerResponse) -> bytes:
    """Serialize only the bounded worker response allowlist."""

    if type(response) is not WorkerResponse:
        raise TypeError("response must be a WorkerResponse")
    _validate_response_fields(response)
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "outcome": response.outcome,
        "attempts": response.attempts,
        "safe_russian_message": response.safe_russian_message,
        "agent_proposal": _proposal_data(response.validation),
    }
    return _encode_json(payload)


def decode_response(payload: bytes) -> WorkerResponse:
    value = _decode_json(payload)
    _require_exact_dict(
        value,
        {
            "protocol_version",
            "outcome",
            "attempts",
            "safe_russian_message",
            "agent_proposal",
        },
    )
    if value["protocol_version"] != PROTOCOL_VERSION:
        raise WorkerProtocolError("unsupported protocol version")
    proposal = value["agent_proposal"]
    validation = None if proposal is None else parse_agent_proposal(proposal)
    if validation is not None and not validation.valid:
        raise WorkerProtocolError("invalid worker proposal")
    response = WorkerResponse(
        value["outcome"],
        value["attempts"],
        value["safe_russian_message"],
        validation,
    )
    try:
        _validate_response_fields(response)
    except (TypeError, ValueError):
        raise WorkerProtocolError("invalid worker response") from None
    return response


def frame_payload(payload: bytes) -> bytes:
    if type(payload) is not bytes or not payload:
        raise WorkerProtocolError("invalid frame payload")
    if len(payload) > MAX_IPC_FRAME_BYTES:
        raise WorkerProtocolError("frame is too large")
    return struct.pack("!I", len(payload)) + payload


def send_frame(
    connection: socket.socket,
    payload: bytes,
    *,
    deadline: float,
    io_timeout: float,
) -> None:
    framed = frame_payload(payload)
    _set_bounded_timeout(connection, deadline, io_timeout)
    try:
        connection.sendall(framed)
    except (OSError, TimeoutError):
        raise WorkerProtocolError("frame write failed") from None


def receive_frame(
    connection: socket.socket,
    *,
    deadline: float,
    io_timeout: float,
    wait_for_first_byte: bool = False,
) -> bytes:
    header = _receive_exact(
        connection,
        FRAME_PREFIX_BYTES,
        deadline=deadline,
        io_timeout=io_timeout,
        wait_for_first_byte=wait_for_first_byte,
    )
    size = struct.unpack("!I", header)[0]
    if size == 0:
        raise WorkerProtocolError("empty frame")
    if size > MAX_IPC_FRAME_BYTES:
        raise WorkerProtocolError("frame is too large")
    payload = _receive_exact(connection, size, deadline=deadline, io_timeout=io_timeout)
    _set_bounded_timeout(connection, deadline, io_timeout)
    try:
        trailing = connection.recv(1)
    except (OSError, TimeoutError):
        raise WorkerProtocolError("frame termination failed") from None
    if trailing:
        raise WorkerProtocolError("unexpected trailing data")
    return payload


def validate_timing(connection_lifetime: object, io_timeout: object) -> None:
    if (
        type(connection_lifetime) not in {int, float}
        or not math.isfinite(connection_lifetime)
        or not MIN_CONNECTION_LIFETIME_SECONDS
        <= connection_lifetime
        <= MAX_CONNECTION_LIFETIME_SECONDS
    ):
        raise ValueError("connection lifetime is invalid")
    _bounded_seconds("I/O timeout", io_timeout, MAX_IO_TIMEOUT_SECONDS)


def _parse_model_request(value: object) -> ModelRequest:
    _require_exact_dict(
        value,
        {
            "contract_version",
            "task",
            "core_self",
            "conversation",
            "history_omitted",
            "allowed_decisions",
        },
    )
    core_self = value["core_self"]
    _require_exact_dict(core_self, {"version", "identity", "principles"})
    principles = core_self["principles"]
    conversation = value["conversation"]
    decisions = value["allowed_decisions"]
    if type(principles) is not list or type(conversation) is not list:
        raise WorkerProtocolError("invalid model request")
    if type(decisions) is not list:
        raise WorkerProtocolError("invalid model request")
    segments = []
    for item in conversation:
        _require_exact_dict(item, {"role", "trust", "content"})
        try:
            segment = ConversationSegment(
                ContextRole(item["role"]),
                ContextTrust(item["trust"]),
                item["content"],
            )
        except (TypeError, ValueError):
            raise WorkerProtocolError("invalid model request") from None
        segments.append(segment)
    try:
        return ModelRequest(
            contract_version=value["contract_version"],
            task=ModelTask(value["task"]),
            core_self_version=core_self["version"],
            core_self_identity=core_self["identity"],
            core_self_principles=tuple(principles),
            conversation=tuple(segments),
            history_omitted=value["history_omitted"],
            allowed_decisions=tuple(AgentDecision(item) for item in decisions),
        )
    except (TypeError, ValueError):
        raise WorkerProtocolError("invalid model request") from None


def _proposal_data(validation: ProposalValidation | None) -> dict[str, str] | None:
    if validation is None:
        return None
    if type(validation) is not ProposalValidation or not validation.valid:
        raise ValueError("response proposal is not valid")
    proposal = validation.proposal
    if type(proposal) is NoActionProposal:
        return {
            "decision": proposal.decision.value,
            "reason_category": proposal.reason_category.value,
        }
    if type(proposal) is ReplyProposal:
        return {"decision": proposal.decision.value, "content": proposal.content}
    if type(proposal) is ResearchProposal:
        return {"decision": proposal.decision.value, "brief": proposal.brief}
    if type(proposal) is PublicationProposal:
        return {"decision": proposal.decision.value, "draft": proposal.draft}
    raise ValueError("response proposal is not supported")


def _validate_response_fields(response: WorkerResponse) -> None:
    if response.outcome not in REMOTE_OUTCOMES:
        raise ValueError("response outcome is invalid")
    if type(response.attempts) is not int or not 0 <= response.attempts <= MAX_ATTEMPTS:
        raise ValueError("response attempts are invalid")
    message = response.safe_russian_message
    if message is not None:
        if type(message) is not str or not message:
            raise ValueError("response message is invalid")
        try:
            size = len(message.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("response message is invalid") from None
        if size > MAX_SAFE_MESSAGE_UTF8_BYTES:
            raise ValueError("response message is too large")
    if response.validation is not None:
        _proposal_data(response.validation)
    if (response.outcome == "completed") != (response.validation is not None):
        raise ValueError("completed response and proposal are inconsistent")


def _encode_json(value: object) -> bytes:
    try:
        payload = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError):
        raise WorkerProtocolError("JSON serialization failed") from None
    if len(payload) > MAX_IPC_FRAME_BYTES:
        raise WorkerProtocolError("frame is too large")
    return payload


def _decode_json(payload: bytes) -> object:
    if type(payload) is not bytes or not payload or len(payload) > MAX_IPC_FRAME_BYTES:
        raise WorkerProtocolError("invalid JSON payload")
    try:
        text = payload.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, RecursionError, ValueError):
        raise WorkerProtocolError("invalid JSON payload") from None
    _validate_json_depth(value)
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkerProtocolError("duplicate JSON field")
        result[key] = value
    return result


def _validate_json_depth(value: object) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_JSON_NESTING_DEPTH:
            raise WorkerProtocolError("invalid JSON payload")
        if type(item) is dict:
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)


def _require_exact_dict(value: object, fields: set[str]) -> None:
    if type(value) is not dict or set(value) != fields:
        raise WorkerProtocolError("unexpected object fields")


def _receive_exact(
    connection: socket.socket,
    size: int,
    *,
    deadline: float,
    io_timeout: float,
    wait_for_first_byte: bool = False,
) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        timeout = None if wait_for_first_byte and not chunks else io_timeout
        _set_bounded_timeout(connection, deadline, timeout)
        try:
            chunk = connection.recv(size - len(chunks))
        except (OSError, TimeoutError):
            raise WorkerProtocolError("frame read failed") from None
        if not chunk:
            raise WorkerProtocolError("truncated frame")
        chunks.extend(chunk)
    return bytes(chunks)


def _set_bounded_timeout(
    connection: socket.socket, deadline: float, io_timeout: float | None
) -> None:
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise WorkerProtocolError("connection lifetime exceeded")
    connection.settimeout(
        remaining if io_timeout is None else min(remaining, io_timeout)
    )


def _bounded_seconds(name: str, value: object, maximum: float) -> None:
    if (
        type(value) not in {int, float}
        or not math.isfinite(value)
        or not 0 < value <= maximum
    ):
        raise ValueError(f"{name} is invalid")
