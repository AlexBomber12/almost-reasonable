"""Secretless one-call public behavior model client."""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import socket
import struct
import time

from app.behavior.deliberation import parse_behavior_proposal
from app.core.public_behavior import PublicBehaviorRequest, _unique_fields
from app.core.core_self import CORE_SELF_VERSION
from app.core.provider_contract import CostEstimateStatus, PROVIDER_NAME
from app.core.provider_spend import CONFIRMED_SPEND_LIMIT_CODES, ProviderAttemptRecord
from app.trusted.openai_worker_protocol import receive_frame, send_frame


class PublicBehaviorClient:
    def __init__(self, socket_path: Path, *, expected_uid: int):
        if (
            not socket_path.is_absolute()
            or len(str(socket_path).encode()) > 107
            or type(expected_uid) is not int
            or not 0 <= expected_uid <= 2**32 - 2
        ):
            raise ValueError("invalid public worker binding")
        self.socket_path, self.expected_uid = socket_path, expected_uid
        self.reservation_evidence = None
        self.provider_store = None
        self.public_context = None

    def complete(self, request, timeout_seconds):
        operation = request["correlation_id"]
        public = PublicBehaviorRequest(
            operation,
            operation,
            request["trigger"],
            tuple(request["authorized_context"]),
            tuple(request["permitted_mutable_state"]),
            timeout_seconds,
        )
        if self.public_context is not None:
            catalog, evidence = self.public_context(operation)
            public = replace(public, catalog=catalog, evidence=evidence)
        if self.reservation_evidence is not None:
            public = replace(public, reservation=self.reservation_evidence(public))
        payload = public.encode()
        deadline = time.monotonic() + timeout_seconds
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout_seconds)
                connection.connect(str(self.socket_path))
                peer = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                if struct.unpack("iII", peer)[1] != self.expected_uid:
                    raise ValueError("public worker peer mismatch")
                send_frame(
                    connection, payload, deadline=deadline, io_timeout=timeout_seconds
                )
                connection.shutdown(socket.SHUT_WR)
                raw = receive_frame(
                    connection, deadline=deadline, io_timeout=timeout_seconds
                )
        except TimeoutError as error:
            # The worker may already have consumed the one-use provider grant.
            raise ValueError("public worker response uncertain") from error
        try:
            value = json.loads(raw, object_pairs_hook=_unique_fields)
        except (UnicodeError, ValueError) as error:
            raise ValueError("invalid public worker result") from error
        if (
            type(value) is not dict
            or set(value) != {"operation_id", "outcome", "proposal", "telemetry"}
            or value["operation_id"] != operation
        ):
            raise ValueError("invalid public worker result")
        outcome = value["outcome"]
        if outcome not in {
            "completed",
            "provider_error",
            "spend_limit_confirmed",
            "spend_suspended",
        }:
            raise ValueError("invalid public worker result")
        telemetry = value["telemetry"]
        fields = {
            "attempt_id",
            "correlation_id",
            "model_identifier",
            "provider_response_id",
            "started_at",
            "duration_ms",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "outcome",
            "provider_error_code",
        }
        if type(telemetry) is not dict or set(telemetry) != fields:
            raise ValueError("invalid public worker telemetry")
        if (
            telemetry["attempt_id"] != operation and outcome != "spend_suspended"
        ) or telemetry["attempt_id"] != telemetry["correlation_id"]:
            raise ValueError("invalid public worker telemetry")
        if outcome == "completed" and telemetry["outcome"] != "completed":
            raise ValueError("invalid public worker telemetry")
        if outcome == "spend_limit_confirmed" and (
            telemetry["outcome"] != outcome
            or telemetry["provider_error_code"] not in CONFIRMED_SPEND_LIMIT_CODES
        ):
            raise ValueError("invalid public worker telemetry")
        if (
            outcome == "spend_suspended"
            and telemetry["provider_error_code"] not in CONFIRMED_SPEND_LIMIT_CODES
        ):
            raise ValueError("invalid public worker telemetry")
        if outcome == "provider_error" and telemetry["outcome"] != outcome:
            raise ValueError("invalid public worker telemetry")
        try:
            started = datetime.fromisoformat(telemetry["started_at"])
            record = ProviderAttemptRecord(
                telemetry["attempt_id"],
                telemetry["correlation_id"],
                PROVIDER_NAME,
                telemetry["model_identifier"],
                "threads-public-worker-v1",
                "threads-public-prompt-v1",
                CORE_SELF_VERSION,
                telemetry["provider_response_id"],
                started,
                telemetry["duration_ms"],
                telemetry["input_tokens"],
                None,
                telemetry["output_tokens"],
                telemetry["total_tokens"],
                telemetry["outcome"],
                0,
                None,
                None,
                None,
                CostEstimateStatus.PRICING_UNAVAILABLE
                if telemetry["total_tokens"] is not None
                else CostEstimateStatus.USAGE_UNAVAILABLE,
                telemetry["provider_error_code"],
            )
            if self.provider_store is not None:
                if not self.provider_store.attempt_exists(record.attempt_id):
                    self.provider_store.record_attempt(
                        record,
                        confirmed_spend_limit_code=(
                            record.provider_error_code
                            if record.provider_error_code in CONFIRMED_SPEND_LIMIT_CODES
                            else None
                        ),
                    )
                # Another authenticated client may have committed the same
                # attempt after our existence check. Verify the durable row,
                # including the confirmed suspension, before accepting it.
                if self.provider_store.usage_attempt(record.attempt_id) != record:
                    raise ValueError("public worker telemetry persistence failed")
                if (
                    record.provider_error_code in CONFIRMED_SPEND_LIMIT_CODES
                    and not self.provider_store.is_spend_suspended()
                ):
                    raise ValueError("public worker suspension persistence failed")
        except (TypeError, ValueError) as error:
            raise ValueError("invalid public worker telemetry") from error
        if outcome != "completed":
            raise ValueError("public worker inference unavailable")
        if not parse_behavior_proposal(value["proposal"]).valid:
            raise ValueError("invalid public worker proposal")
        return value["proposal"]
