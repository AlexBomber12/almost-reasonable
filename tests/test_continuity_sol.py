"""Live adapter contract tests use only an injected SDK, never credentials."""

import json
from types import SimpleNamespace

import pytest

from app.continuity_experiment import Lab, ModelFailure
from app.continuity_sol import Sol, SCHEMA
from tests.test_continuity_experiment import decision, post


class Responses:
    def __init__(self, **changes):
        self.response = SimpleNamespace(
            status="completed",
            output=[],
            output_text=json.dumps(decision()),
        )
        self.response.__dict__.update(changes)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.response


def test_sol_has_no_tools_one_call_and_explicit_output_contract(tmp_path):
    lab = Lab.create(tmp_path / "lab")
    post(lab)
    responses = Responses()
    model = Sol(responses, stopped=lambda: False, max_calls=2)
    result = lab.step("live-contract", "post", model)
    assert result["status"] == "decided"
    assert result["model_kind"] == "sol"
    assert len(responses.requests) == 1
    request = responses.requests[0]
    assert request["tools"] == []
    assert request["store"] is False
    assert request["timeout"] == 25
    assert request["model"] == "gpt-6.1-sol"
    assert request["text"]["format"]["schema"] == SCHEMA
    assert request["reasoning"] == {"effort": "low"}
    assert lab.step("live-contract", "post", model) == result
    assert len(responses.requests) == 1
    # The maximum is a durable experiment bound, including after process restart.
    assert Lab(lab.root).step("second", "post", model)["status"] == "decided"
    assert Lab(lab.root).step("third", "post", model)["error"] == "operation_limit"
    assert len(responses.requests) == 2


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"status": "incomplete"}, "incomplete"),
        ({"status": "failed"}, "incomplete"),
        (
            {"output": [SimpleNamespace(content=[SimpleNamespace(type="refusal")])]},
            "refused",
        ),
        ({"output_text": "not-json"}, "invalid_output"),
        ({"output_text": "x" * 17000}, "invalid_output"),
        ({"output_text": None}, "invalid_output"),
    ],
)
def test_incomplete_refused_and_invalid_outputs_are_not_applied(changes, code):
    model = Sol(Responses(**changes), stopped=lambda: False, max_calls=1)
    with pytest.raises(ModelFailure, match=code):
        model.complete({})


def test_stop_precedes_paid_boundary():
    responses = Responses()
    model = Sol(responses, stopped=lambda: True, max_calls=1)
    with pytest.raises(ModelFailure, match="stopped"):
        model.complete({})
    assert responses.requests == []


@pytest.mark.parametrize("maximum", [0, True, 61, 1.5])
def test_invalid_live_operation_limits(maximum):
    with pytest.raises(ValueError):
        Sol(Responses(), stopped=lambda: False, max_calls=maximum)


def test_sdk_exception_is_not_retried_or_logged():
    class Bad(Responses):
        def create(self, **kwargs):
            self.requests.append(kwargs)
            raise TimeoutError("sensitive SDK response")

    responses = Bad()
    with pytest.raises(ModelFailure, match="timeout"):
        Sol(responses, stopped=lambda: False, max_calls=1).complete({})
    assert len(responses.requests) == 1


@pytest.mark.parametrize(
    "name,code,expected",
    [
        ("APITimeoutError", None, "timeout"),
        ("BudgetError", "public_monthly_budget_exhausted", "budget_blocked"),
        ("ConnectionError", None, "provider_error"),
    ],
)
def test_sdk_failures_are_classified_without_provider_text(name, code, expected):
    error = type(name, (Exception,), {"code": code})

    class Bad:
        def create(self, **kwargs):
            raise error("private exception text")

    with pytest.raises(ModelFailure, match=expected):
        Sol(Bad(), stopped=lambda: False, max_calls=1).complete({})


def test_normal_output_items_and_duplicate_or_wrong_schema_json():
    raw = Responses(
        output=[
            SimpleNamespace(),
            SimpleNamespace(content=[SimpleNamespace(type="output_text")]),
        ]
    )
    assert Sol(raw, stopped=lambda: False, max_calls=1).complete({}) == decision()
    from app.continuity_sol import decode

    for text in ['{"action":"reply","action":"no_action"}', "{}", "[]"]:
        with pytest.raises(ModelFailure, match="invalid_output"):
            decode(text)
