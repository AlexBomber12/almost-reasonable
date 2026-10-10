import json
import socket
import struct
from threading import Thread
import time

import pytest

from app.core.agent_proposals import ProposalValidation, parse_agent_proposal
from app.core.context import ContextRole, ContextTrust, ConversationSegment
from app.core.core_self import initial_core_self
from app.core.model_contract import (
    MODEL_ALLOWED_DECISIONS,
    MODEL_CONTRACT_VERSION,
    ModelRequest,
    ModelTask,
)
from app.trusted.openai_worker_protocol import (
    FRAME_PREFIX_BYTES,
    IPC_REJECTED,
    MAX_IPC_FRAME_BYTES,
    InferenceRequest,
    WorkerProtocolError,
    WorkerResponse,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    frame_payload,
    receive_frame,
    send_frame,
    validate_timing,
    _encode_json,
)


def model_request(content="Синтетический запрос."):
    core = initial_core_self()
    return ModelRequest(
        MODEL_CONTRACT_VERSION,
        ModelTask.PRIVATE_CONVERSATION,
        core.version,
        core.identity,
        core.principles,
        (
            ConversationSegment(
                ContextRole.PARTICIPANT,
                ContextTrust.UNTRUSTED_CONVERSATIONAL_DATA,
                content,
            ),
        ),
        False,
        MODEL_ALLOWED_DECISIONS,
    )


def request_data():
    return json.loads(
        encode_request(
            InferenceRequest("operation-a", "correlation-a", model_request())
        )
    )


def response_data():
    return json.loads(encode_response(WorkerResponse("disabled", 0, "Недоступно.")))


def test_request_round_trip_is_exact_and_revalidates_model_request():
    request = InferenceRequest("operation-a", "correlation-a", model_request())
    payload = encode_request(request)
    decoded = decode_request(payload)

    assert decoded == request
    assert tuple(json.loads(payload)) == (
        "correlation_id",
        "model_request",
        "operation_id",
        "operation_type",
        "protocol_version",
    )
    assert "api_key" not in payload.decode()


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data.update(protocol_version="future"),
        lambda data: data.update(operation_type="reset_spending"),
        lambda data: data.update(api_key="TEST-CANARY"),
        lambda data: data.pop("operation_id"),
        lambda data: data.update(operation_id="bad value"),
        lambda data: data.update(correlation_id=True),
        lambda data: data["model_request"].update(model="gpt-6.1-sol"),
        lambda data: data["model_request"].update(history_omitted=1),
        lambda data: data["model_request"]["core_self"].update(identity="forged"),
        lambda data: data["model_request"]["conversation"][0].update(role="system"),
        lambda data: data["model_request"]["conversation"][0].update(trust="trusted"),
        lambda data: data["model_request"]["conversation"][0].update(extra=True),
        lambda data: data["model_request"].update(allowed_decisions=["reply"]),
        lambda data: data["model_request"].update(task="administration"),
        lambda data: data["model_request"].update(conversation={}),
        lambda data: data["model_request"].update(allowed_decisions={}),
        lambda data: data["model_request"]["core_self"].update(principles={}),
    ],
)
def test_request_rejects_unknown_missing_privileged_and_malformed_fields(change):
    data = request_data()
    change(data)

    with pytest.raises(WorkerProtocolError):
        decode_request(json.dumps(data).encode())


def test_request_rejects_malformed_nested_enum_without_echoing_payload():
    data = request_data()
    data["model_request"]["allowed_decisions"][0] = {"secret": "CANARY"}

    with pytest.raises(WorkerProtocolError) as captured:
        decode_request(json.dumps(data).encode())

    assert "CANARY" not in str(captured.value)


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b"{",
        b"{}",
        b'{"x":1,"x":2}',
        b"",
        object(),
        b"[]",
    ],
)
def test_request_rejects_invalid_json_containers_and_duplicate_fields(payload):
    with pytest.raises(WorkerProtocolError):
        decode_request(payload)


def test_request_encoder_rejects_wrong_type_and_invalid_model():
    with pytest.raises(TypeError):
        encode_request(object())
    with pytest.raises(ValueError):
        encode_request(InferenceRequest("bad value", "correlation", model_request()))
    with pytest.raises(TypeError):
        encode_request(InferenceRequest("operation", "correlation", object()))
    with pytest.raises(ValueError):
        encode_request(InferenceRequest("o" * 127, "correlation", model_request()))


def test_request_decoder_rejects_operation_identifier_too_long():
    data = request_data()
    data["operation_id"] = "o" * 127
    with pytest.raises(WorkerProtocolError):
        decode_request(json.dumps(data).encode())


@pytest.mark.parametrize(
    "proposal",
    [
        {"decision": "no_action", "reason_category": "not_warranted"},
        {"decision": "reply", "content": "Ответ."},
        {"decision": "research", "brief": "Тема."},
        {"decision": "propose_publication", "draft": "Черновик."},
    ],
)
def test_response_round_trip_returns_only_validated_proposals(proposal):
    response = WorkerResponse("completed", 1, None, parse_agent_proposal(proposal))

    decoded = decode_response(encode_response(response))

    assert decoded == response


def test_response_round_trip_without_proposal():
    response = WorkerResponse(IPC_REJECTED, 0, "Запрос отклонён.")
    assert decode_response(encode_response(response)) == response


@pytest.mark.parametrize(
    "response",
    [
        object(),
        WorkerResponse("invented", 0, None),
        WorkerResponse("disabled", True, None),
        WorkerResponse("disabled", 4, None),
        WorkerResponse("disabled", 0, ""),
        WorkerResponse("disabled", 0, "x" * 513),
        WorkerResponse("completed", 1, None),
        WorkerResponse("completed", 1, None, ProposalValidation(None, None)),
        WorkerResponse(
            "disabled",
            0,
            None,
            parse_agent_proposal({"decision": "reply", "content": "Нет."}),
        ),
        WorkerResponse(
            "completed",
            1,
            None,
            parse_agent_proposal({"decision": "remember"}),
        ),
    ],
)
def test_response_encoder_rejects_non_allowlisted_values(response):
    error = TypeError if response is object else (TypeError, ValueError)
    with pytest.raises(error):
        encode_response(response)


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data.update(protocol_version="future"),
        lambda data: data.update(outcome="invented"),
        lambda data: data.update(attempts=True),
        lambda data: data.update(safe_russian_message=[]),
        lambda data: data.update(agent_proposal={"decision": "shell"}),
        lambda data: data.update(extra=True),
        lambda data: data.pop("outcome"),
    ],
)
def test_response_decoder_rejects_malformed_values(change):
    data = response_data()
    change(data)
    with pytest.raises(WorkerProtocolError):
        decode_response(json.dumps(data).encode())


def test_response_decoder_rejects_completed_outcome_without_proposal():
    data = response_data()
    data["outcome"] = "completed"
    data["attempts"] = 1
    with pytest.raises(WorkerProtocolError, match="invalid worker response"):
        decode_response(json.dumps(data).encode())


def test_response_serialization_rejects_invalid_unicode():
    with pytest.raises(ValueError):
        encode_response(WorkerResponse("disabled", 0, "\ud800"))


def test_frame_round_trip_uses_fixed_length_prefix_and_eof_termination():
    sender, receiver = socket.socketpair()
    payload = b'{"test":true}'

    def write():
        send_frame(sender, payload, deadline=time.monotonic() + 1, io_timeout=1)
        sender.shutdown(socket.SHUT_WR)

    thread = Thread(target=write)
    thread.start()
    received = receive_frame(receiver, deadline=time.monotonic() + 1, io_timeout=1)
    thread.join(timeout=1)
    sender.close()
    receiver.close()

    assert received == payload
    assert frame_payload(payload)[:FRAME_PREFIX_BYTES] == struct.pack(
        "!I", len(payload)
    )


@pytest.mark.parametrize(
    "wire",
    [
        b"",
        b"\x00\x00",
        struct.pack("!I", 0),
        struct.pack("!I", 3) + b"ab",
        struct.pack("!I", 1) + b"ab",
        struct.pack("!I", MAX_IPC_FRAME_BYTES + 1),
    ],
)
def test_frame_reader_rejects_truncated_empty_trailing_and_oversized(wire):
    sender, receiver = socket.socketpair()
    sender.sendall(wire)
    sender.shutdown(socket.SHUT_WR)
    with pytest.raises(WorkerProtocolError):
        receive_frame(receiver, deadline=time.monotonic() + 1, io_timeout=1)
    sender.close()
    receiver.close()


def test_frame_helpers_reject_invalid_or_oversized_payloads():
    for payload in (b"", object(), b"x" * (MAX_IPC_FRAME_BYTES + 1)):
        with pytest.raises(WorkerProtocolError):
            frame_payload(payload)


def test_frame_read_and_write_timeouts_are_sanitized():
    sender, receiver = socket.socketpair()
    with pytest.raises(WorkerProtocolError, match="read"):
        receive_frame(receiver, deadline=time.monotonic() + 0.02, io_timeout=0.01)
    with pytest.raises(WorkerProtocolError, match="lifetime"):
        send_frame(sender, b"x", deadline=time.monotonic() - 1, io_timeout=1)
    sender.close()
    receiver.close()


class FailingSocket:
    def settimeout(self, _timeout):
        pass

    def sendall(self, _payload):
        raise OSError("CANARY")

    def recv(self, _size):
        raise OSError("CANARY")


def test_frame_socket_failures_are_sanitized():
    with pytest.raises(WorkerProtocolError, match="write") as write_error:
        send_frame(FailingSocket(), b"x", deadline=time.monotonic() + 1, io_timeout=1)
    with pytest.raises(WorkerProtocolError, match="termination") as read_error:
        receive_frame(
            SequenceSocket([struct.pack("!I", 1), b"x"]),
            deadline=time.monotonic() + 1,
            io_timeout=1,
        )
    assert "CANARY" not in str(write_error.value)
    assert "CANARY" not in str(read_error.value)


class SequenceSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def settimeout(self, _timeout):
        pass

    def recv(self, _size):
        if self.chunks:
            return self.chunks.pop(0)
        raise OSError("CANARY")


@pytest.mark.parametrize(
    ("lifetime", "timeout"),
    [
        (0, 1),
        (319, 1),
        (341, 1),
        (float("nan"), 1),
        (320, 0),
        (320, 11),
        (True, 1),
    ],
)
def test_timing_configuration_is_strictly_bounded(lifetime, timeout):
    with pytest.raises(ValueError):
        validate_timing(lifetime, timeout)


def test_large_but_valid_request_stays_within_frame_bound():
    payload = encode_request(
        InferenceRequest(
            "operation-a", "correlation-a", model_request("я" * (128 * 1024))
        )
    )
    assert len(payload) < MAX_IPC_FRAME_BYTES
    assert decode_request(payload).model_request.conversation[0].content.startswith("я")


def test_json_encoder_and_decoder_resource_failures_are_sanitized():
    with pytest.raises(WorkerProtocolError, match="serialization"):
        _encode_json({"unsupported": {1}})
    with pytest.raises(WorkerProtocolError, match="too large"):
        _encode_json("x" * (MAX_IPC_FRAME_BYTES + 1))
    nested = b"[" * 2000 + b"]" * 2000
    with pytest.raises(WorkerProtocolError, match="invalid JSON"):
        decode_request(nested)
    oversized_integer = b'{"value":' + b"1" * 5000 + b"}"
    with pytest.raises(WorkerProtocolError, match="invalid JSON"):
        decode_request(oversized_integer)
