"""Small fixed request and allowlisted response for worker telemetry."""

import json
import struct

REQUEST = b"AR-WORKER-STATUS/1\n"
VERSION = "worker-status-v1"
MAX_RESPONSE = 2048
TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")


def unavailable():
    return {
        "available": False,
        "collected_at": None,
        "stop": {"valid": False, "active": None},
        "provider": {
            "available": False,
            "attempt_count": None,
            "recorded_tokens": None,
            "unknown_usage_attempts": None,
            "unresolved_reservations": None,
            "spend_suspended": None,
        },
    }


def encode(value):
    body = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(body) > MAX_RESPONSE:
        raise ValueError("telemetry response is too large")
    return struct.pack("!I", len(body)) + body


def decode(body):
    if len(body) > MAX_RESPONSE:
        raise ValueError("telemetry response is too large")
    value = json.loads(body)
    if (
        type(value) is not dict
        or set(value) != {"version", "available", "collected_at", "stop", "provider"}
        or value["version"] != VERSION
        or value["available"] is not True
        or type(value["collected_at"]) is not str
    ):
        raise ValueError("invalid telemetry response")
    stop, provider = value["stop"], value["provider"]
    if (
        type(stop) is not dict
        or set(stop) != {"valid", "active"}
        or type(stop["valid"]) is not bool
        or (
            type(stop["active"]) is not bool
            if stop["valid"]
            else stop["active"] is not None
        )
    ):
        raise ValueError("invalid stop response")
    if (
        type(provider) is not dict
        or set(provider)
        != {
            "available",
            "attempt_count",
            "recorded_tokens",
            "unknown_usage_attempts",
            "unresolved_reservations",
            "spend_suspended",
        }
        or type(provider["available"]) is not bool
    ):
        raise ValueError("invalid provider response")
    if provider["available"]:
        tokens = provider["recorded_tokens"]
        if (
            type(tokens) is not dict
            or set(tokens) != set(TOKEN_KEYS)
            or any(type(v) is not int or v < 0 for v in tokens.values())
            or type(provider["attempt_count"]) is not int
            or provider["attempt_count"] < 0
            or type(provider["unknown_usage_attempts"]) is not int
            or not 0 <= provider["unknown_usage_attempts"] <= provider["attempt_count"]
            or type(provider["unresolved_reservations"]) is not int
            or provider["unresolved_reservations"] < 0
            or type(provider["spend_suspended"]) is not bool
        ):
            raise ValueError("invalid provider response")
    elif any(
        provider[key] is not None
        for key in (
            "attempt_count",
            "recorded_tokens",
            "unknown_usage_attempts",
            "unresolved_reservations",
            "spend_suspended",
        )
    ):
        raise ValueError("invalid provider response")
    return {
        key: value[key] for key in ("available", "collected_at", "stop", "provider")
    }
