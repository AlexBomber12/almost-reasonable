"""Bounded unprivileged mission data, enabled only by trusted application mode."""

import json

from app.behavior.mission import MissionPurpose, _contains_forbidden_mission_content
from app.core.self_state_screen import screen_self_state_text

FREE_PURPOSES = ("agent_defined", "agent_defined_without_threads")
ENABLED = "free-mission-v1"
PENDING = "free-mission-pending-v1"
MAX_TEXT_BYTES = 2048
MAX_CONTEXT_BYTES = 8192


def validate_free_text(value, *, limit=MAX_TEXT_BYTES):
    if type(value) is not str or not value.strip():
        raise ValueError("invalid free mission text")
    try:
        bounded = len(value.encode()) <= limit
    except UnicodeError:
        raise ValueError("invalid free mission text") from None
    if (
        not bounded
        or _contains_forbidden_mission_content(value)
        or not screen_self_state_text(value).allowed
    ):
        raise ValueError("unsafe or overbound free mission text")
    return value


def _reasons(value, *, required):
    if type(value) is not list or len(value) > 6 or (required and not value):
        raise ValueError("invalid free mission reasons")
    return tuple(validate_free_text(text, limit=512) for text in value)


def parse_free_choice(value):
    if (
        type(value) is not dict
        or set(value)
        != {"decision", "action", "purpose", "text", "reasons", "counterevidence"}
        or value["decision"] != "mission"
    ):
        raise ValueError("invalid free mission choice")
    action, purpose, text = value["action"], value["purpose"], value["text"]
    if type(action) is not str or action not in (
        "propose",
        "confirm",
        "reject",
        "abandon",
    ):
        raise ValueError("invalid free mission action")
    if action == "propose":
        if type(purpose) is not str or purpose not in FREE_PURPOSES:
            raise ValueError("invalid free mission purpose")
        validate_free_text(text)
        purpose = MissionPurpose(purpose)
    elif purpose is not None or text is not None:
        raise ValueError("resolution cannot rewrite the pending mission")
    reasons = _reasons(value["reasons"], required=True)
    counterevidence = _reasons(value["counterevidence"], required=False)
    if action == "propose":
        validate_free_context(
            (
                PENDING,
                purpose.value,
                text,
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(counterevidence, ensure_ascii=False),
            )
        )
    return (
        action,
        purpose,
        text,
        reasons,
        counterevidence,
    )


def is_free_context(value):
    return type(value) is tuple and bool(value) and value[0] in (ENABLED, PENDING)


def validate_free_context(value):
    if value == (ENABLED,):
        return value
    if not is_free_context(value) or len(value) != 5 or value[0] != PENDING:
        raise ValueError("invalid free mission context")
    if type(value[1]) is not str or value[1] not in MissionPurpose._value2member_map_:
        raise ValueError("invalid pending mission purpose")
    validate_free_text(value[2])
    if (
        any(type(text) is not str for text in value[3:])
        or len(json.dumps(value, ensure_ascii=False).encode()) > MAX_CONTEXT_BYTES
    ):
        raise ValueError("free mission context exceeds bound")
    _reasons(json.loads(value[3]), required=True)
    _reasons(json.loads(value[4]), required=False)
    return value


def free_context(proposal):
    if proposal is None:
        return (ENABLED,)
    return validate_free_context(
        (
            PENDING,
            proposal.purpose.value,
            proposal.proposed_mission,
            json.dumps(proposal.supporting_reasons, ensure_ascii=False),
            json.dumps(proposal.counterevidence, ensure_ascii=False),
        )
    )


def public_prompt_version(context, intellectual):
    if is_free_context(context):
        return (
            "threads-public-free-mission-intellectual-v1"
            if intellectual
            else "threads-public-free-mission-prompt-v1"
        )
    from app.core.intellectual_state import PROMPT_VERSION

    return PROMPT_VERSION if intellectual else "threads-public-prompt-v1"
