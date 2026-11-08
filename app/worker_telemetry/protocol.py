"""Small fixed request and allowlisted response for worker telemetry."""

import json
import struct
import re

from app.core.public_rejection import PUBLIC_PROPOSAL_REJECTIONS
from app.owner_control.diagnostics import safe_time

REQUEST = b"AR-WORKER-STATUS/1\n"
VERSION = "worker-status-v1"
MAX_RESPONSE = 2048
DIAGNOSTICS_REQUEST = b"AR-WORKER-DIAGNOSTICS/1\n"
DIAGNOSTICS_VERSION = "worker-diagnostics-v1"
MAX_DIAGNOSTICS_RESPONSE = 8192
TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")
RESEARCH_REJECTIONS = frozenset(
    {
        "research_response_invalid",
        "research_usage_invalid",
        "research_output_truncated",
        "research_output_filtered",
        "research_output_incomplete",
        "research_provenance_invalid",
        "research_citation_budget",
        "research_annotation_budget",
        "research_citation_conflict",
        "research_assessment_json",
        "research_assessment_fields",
        "research_assessment_source",
        "research_source_rejected",
        "research_privacy_rejected",
        "research_assessment_invalid",
        "research_uncited_url",
        "research_language_rejected",
        "research_report_invalid",
        "research_request_invalid",
    }
)


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


def encode(value, maximum=MAX_RESPONSE):
    body = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(body) > maximum:
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


def diagnostics_unavailable():
    return {"available": False, "rows": []}


def decode_diagnostics(body):
    if len(body) > MAX_DIAGNOSTICS_RESPONSE:
        raise ValueError("diagnostics response is too large")
    value = json.loads(body, object_pairs_hook=_unique)
    if (
        type(value) is not dict
        or set(value) - {"budget"} != {"version", "available", "rows"}
        or value["version"] != DIAGNOSTICS_VERSION
        or type(value["available"]) is not bool
        or type(value["rows"]) is not list
        or len(value["rows"]) > 10
        or (not value["available"] and value["rows"])
    ):
        raise ValueError("invalid diagnostics response")
    for row in value["rows"]:
        if (
            type(row) is not dict
            or set(row)
            != {"time", "family", "outcome", "reason", "tokens", "reference"}
            or not _valid_time(row["time"])
            or row["family"] != "provider_attempt"
            or type(row["outcome"]) is not str
            or row["outcome"]
            not in {
                "completed",
                "rejected",
                "stopped",
                "uncertain",
                "reservation",
                "rate_limited",
                "unknown",
            }
            or (row["reason"] is not None and type(row["reason"]) is not str)
            or row["reason"]
            not in {
                None,
                "invalid_proposal",
                "invalid_response",
                "refused",
                "timeout",
                "spend_limit",
                "authorization",
                "provider_error",
                "rate_limit",
                "incomplete_usage",
                "unknown",
            }
            | RESEARCH_REJECTIONS
            | PUBLIC_PROPOSAL_REJECTIONS
            or (
                row["tokens"] is not None
                and (
                    type(row["tokens"]) is not str
                    or any(c not in "0123456789" for c in row["tokens"])
                    or not row["tokens"]
                    or len(row["tokens"]) > 20
                )
            )
            or (
                row["reference"] is not None
                and (
                    type(row["reference"]) is not str
                    or len(row["reference"]) != 24
                    or any(c not in "0123456789abcdef" for c in row["reference"])
                )
            )
        ):
            raise ValueError("invalid diagnostics row")
    result = {"available": value["available"], "rows": value["rows"]}
    if "budget" in value:
        result["budget"] = _budget(value["budget"])
    return result


def _budget(value):
    if (
        type(value) is dict
        and set(value) == {"available"}
        and value["available"] is False
    ):
        return value
    numbers = {
        "limit_microusd",
        "settled_microusd",
        "unresolved_microusd",
        "unresolved_attempts",
        "remaining_microusd",
        "month_attempts",
        "next_reservation_microusd",
    }
    if (
        type(value) is not dict
        or set(value)
        != numbers
        | {
            "available",
            "scope",
            "billing_authority",
            "collected_at",
            "month",
            "lane",
            "search_limit",
            "admission_available",
        }
        or value["available"] is not True
        or value["scope"]
        not in ("conservative_monthly_wallet", "shared_conservative_project_wallet")
        or value["billing_authority"] is not False
        or value["lane"] not in ("sol", "research", "private_sol")
        or type(value["month"]) is not str
        or re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value["month"]) is None
        or not _valid_time(value["collected_at"])
        or any(
            type(value[key]) is not int or not 0 <= value[key] <= 2**63 - 1
            for key in numbers
        )
        or type(value["admission_available"]) is not bool
        or (
            value["search_limit"] != 100 or type(value["search_limit"]) is not int
            if value["lane"] == "research"
            else value["search_limit"] is not None
        )
    ):
        raise ValueError("invalid monthly budget observation")
    used = value["settled_microusd"] + value["unresolved_microusd"]
    allowed = used + value["next_reservation_microusd"] <= value["limit_microusd"] and (
        value["search_limit"] is None or value["month_attempts"] < value["search_limit"]
    )
    if (
        value["remaining_microusd"] != max(0, value["limit_microusd"] - used)
        or value["admission_available"] != allowed
    ):
        raise ValueError("inconsistent monthly budget observation")
    return value


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate diagnostics field")
        value[key] = item
    return value


def _valid_time(value):
    try:
        safe_time(value)
    except (TypeError, ValueError):
        return False
    return True
