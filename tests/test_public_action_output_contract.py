"""Synthetic producer/consumer contract regressions, with no provider calls."""

from copy import deepcopy
from dataclasses import replace

from jsonschema import Draft202012Validator
import pytest

from app.core.free_mission import ENABLED
from app.core.intellectual_state import intellectual_format
from app.core.public_behavior import PublicBehaviorRequest
from app.trusted.public_behavior_worker import (
    FREE_MISSION_PROPOSAL_FORMAT,
    MISSION_PROPOSAL_FORMAT,
    PUBLIC_PROPOSAL_FORMAT,
    PublicSDKAdapter,
)
from test_public_proposal_diagnostics import RawSDK, GOOD


FORMATS = [
    PUBLIC_PROPOSAL_FORMAT,
    MISSION_PROPOSAL_FORMAT,
    FREE_MISSION_PROPOSAL_FORMAT,
]


def action_body(fmt, decision="no_action", **active):
    schema = fmt["schema"]
    fields = (
        schema["properties"]["proposal"]["anyOf"][0]["properties"]
        if "proposal" in schema["properties"]
        else schema["properties"]
    )
    return {**dict.fromkeys(fields), "decision": decision, **active}


def payload(fmt, action):
    return {"proposal": action} if "proposal" in fmt["schema"]["properties"] else action


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize(
    "decision,active,extra",
    [
        ("no_action", {"reason_category": "other"}, {"content": "explanation"}),
        ("reply", {"content": "Hello"}, {"draft": "extra draft"}),
        ("propose_publication", {"draft": "A thought"}, {"topic_id": "extra"}),
        ("research", {"topic_id": "A public question?"}, {"reason_category": "other"}),
    ],
)
def test_schema_allows_one_action_and_excludes_inactive_fields(
    fmt, decision, active, extra
):
    validator = Draft202012Validator(fmt["schema"])
    action = action_body(fmt, decision, **active)
    validator.validate(payload(fmt, action))
    assert not validator.is_valid(payload(fmt, {**action, **extra}))


@pytest.mark.parametrize("fmt", FORMATS)
def test_producer_question_bound_fits_native_utf8_limit(fmt):
    validator = Draft202012Validator(fmt["schema"])
    action = action_body(fmt, "research", topic_id="🙂" * 128)
    validator.validate(payload(fmt, action))
    assert len(action["topic_id"].encode()) == 512
    assert not validator.is_valid(payload(fmt, {**action, "topic_id": "🙂" * 129}))


@pytest.mark.parametrize("fmt", FORMATS[1:])
def test_schema_forbids_resolution_rewriting_pending_mission(fmt):
    active = {"mission_action": "confirm"}
    if fmt is FREE_MISSION_PROPOSAL_FORMAT:
        active.update(
            mission_reasons=["Continue considering this"], mission_counterevidence=[]
        )
    action = action_body(fmt, "mission", **active)
    validator = Draft202012Validator(fmt["schema"])
    validator.validate(payload(fmt, action))
    forbidden = (
        "agent_defined" if fmt is FREE_MISSION_PROPOSAL_FORMAT else "general_inquiry"
    )
    assert not validator.is_valid(
        payload(fmt, {**action, "mission_purpose": forbidden})
    )


@pytest.mark.parametrize("context", [(), ("enabled",), (ENABLED,)])
@pytest.mark.parametrize("intellectual", [False, True])
def test_real_sdk_envelope_reaches_unchanged_local_parser(context, intellectual):
    fmt = FORMATS[2 if context == (ENABLED,) else 1 if context else 0]
    action = action_body(fmt, reason_category="other")
    value = {"proposal": action}
    request = PublicBehaviorRequest(
        "operation-1", "operation-1", "autonomous_wakeup", (), (), 5
    )
    request = replace(request, mission_evolution=context)
    if intellectual:
        request = replace(request, intellectual_state={})
        value.update(
            intellectual_version="agent-intellectual-state-v1", intellectual=None
        )
        schema = intellectual_format(fmt["schema"])
    else:
        schema = fmt["schema"]
    Draft202012Validator(schema).validate(value)
    result = PublicSDKAdapter(RawSDK(value), model="gpt-6.1-sol").complete(request)
    expected = {"decision": "no_action", "reason_category": "other"}
    assert result == (
        {"version": "agent-intellectual-state-v1", "action": expected, "state": None}
        if intellectual
        else expected
    )


@pytest.mark.parametrize(
    "value",
    [
        {"proposal": {**GOOD, "content": "inactive"}},
        {"proposal": GOOD, "extra": "unknown"},
        {"proposal": {**GOOD, "unexpected": "unknown"}},
        {"proposal": None},
    ],
)
def test_envelope_never_salvages_invalid_or_unknown_fields(value):
    request = PublicBehaviorRequest(
        "operation-1", "operation-1", "autonomous_wakeup", (), (), 5
    )
    with pytest.raises(ValueError):
        PublicSDKAdapter(RawSDK(value), model="gpt-6.1-sol").complete(request)


def test_format_building_does_not_mutate_base_or_intellectual_schema():
    before = deepcopy(FORMATS)
    for fmt in FORMATS:
        schema = intellectual_format(fmt["schema"])
        Draft202012Validator.check_schema(schema)
        assert schema["type"] == "object" and "anyOf" not in schema
        assert "proposal" in schema["properties"]
    assert FORMATS == before
