from dataclasses import replace
import json

import pytest

from app.research.brief import (
    BriefDisposition,
    ResearchBrief,
    ResearchQuestion,
    ResearchRecordSet,
    ResearchScreeningContext,
    build_safe_research_brief,
)
from app.research.provider import (
    APPROVED_RESEARCH_MODELS,
    FakeHostedSearchTransport,
    HostedSearchConfig,
    HostedSearchRequest,
    HostedSearchResponse,
    HostedSearchTransportError,
    SearchContextSize,
    build_hosted_search_request,
    default_hosted_search_config,
)


def brief():
    result = build_safe_research_brief(
        ResearchRecordSet((ResearchQuestion("How do auroral substorms form?"),)),
        result_budget=3,
        trusted_config_version="research-config-v1",
        screening=ResearchScreeningContext(),
    )
    assert result.disposition is BriefDisposition.READY
    return result.brief


def config(**changes):
    value = HostedSearchConfig(
        version="hosted-search-v1",
        model="gpt-6.1-sol",
        enabled=True,
        external_web_access=True,
        search_context_size=SearchContextSize.MEDIUM,
        max_output_tokens=1200,
        max_search_calls=2,
        max_attempts=3,
        timeout_seconds=20.0,
        overall_timeout_seconds=50.0,
        initial_backoff_seconds=1.0,
        maximum_backoff_seconds=4.0,
    )
    return replace(value, **changes)


def test_default_config_keeps_live_research_disabled():
    value = default_hosted_search_config()

    assert value.enabled is False
    assert value.model in APPROVED_RESEARCH_MODELS
    assert "key" not in repr(value).lower()
    assert "credential" not in repr(value).lower()


def test_request_is_an_isolated_bounded_responses_web_search_call():
    request = build_hosted_search_request(brief(), config())
    payload = request.to_sdk_kwargs()

    assert tuple(payload) == (
        "model",
        "instructions",
        "input",
        "tools",
        "tool_choice",
        "include",
        "store",
        "background",
        "stream",
        "max_output_tokens",
        "max_tool_calls",
        "truncation",
    )
    assert payload["store"] is False
    assert payload["background"] is False
    assert payload["stream"] is False
    assert payload["tool_choice"] == "required"
    assert payload["include"] == ["web_search_call.action.sources"]
    assert payload["tools"] == [
        {
            "type": "web_search",
            "external_web_access": True,
            "search_context_size": "medium",
        }
    ]
    assert payload["max_tool_calls"] == 2
    assert payload["max_output_tokens"] == 1200
    assert payload["truncation"] == "disabled"
    assert "conversation" not in payload
    assert "previous_response_id" not in payload
    assert all(tool["type"] != "function" for tool in payload["tools"])
    assert "http" not in json.dumps(payload["tools"])


def test_request_contains_only_the_safe_brief_not_screening_or_private_state():
    payload = build_hosted_search_request(brief(), config()).to_sdk_kwargs()
    parsed = json.loads(payload["input"])

    assert set(parsed) == {
        "question",
        "broad_topic",
        "purpose",
        "language_preferences",
        "time_requirement",
        "public_reference_identifiers",
        "result_budget",
        "trusted_config_version",
    }
    assert "principal" not in payload["input"]
    assert "history" not in payload["input"]
    assert "provenance" not in payload["input"]


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "bad version"},
        {"model": "uncontrolled-model"},
        {"enabled": 1},
        {"external_web_access": 1},
        {"search_context_size": "high"},
        {"max_output_tokens": 0},
        {"max_output_tokens": 4097},
        {"max_search_calls": 0},
        {"max_search_calls": 4},
        {"max_attempts": 0},
        {"max_attempts": 4},
        {"timeout_seconds": 0},
        {"overall_timeout_seconds": 0},
        {"overall_timeout_seconds": 19},
        {"initial_backoff_seconds": -1},
        {"maximum_backoff_seconds": 0.5},
    ],
)
def test_untrusted_or_unbounded_provider_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        build_hosted_search_request(brief(), config(**changes))


def test_request_builder_requires_exact_types_and_authority():
    with pytest.raises(TypeError):
        build_hosted_search_request(object(), config())
    with pytest.raises(TypeError):
        build_hosted_search_request(brief(), object())

    forged_brief = ResearchBrief(
        "raw private history",
        None,
        None,
        (),
        None,
        (),
        1,
        "research-config-v1",
        object(),
    )
    with pytest.raises(ValueError, match="safe construction pipeline"):
        build_hosted_search_request(forged_brief, config())

    forged = HostedSearchRequest(
        model="gpt-6.1-sol",
        input_json="{}",
        tool={},
        max_output_tokens=1,
        max_search_calls=1,
        _authority=object(),
    )
    with pytest.raises(ValueError, match="trusted builder"):
        forged.to_sdk_kwargs()


def test_request_serialized_size_is_bounded(monkeypatch):
    import app.research.provider as provider_module

    monkeypatch.setattr(provider_module, "MAX_REQUEST_UTF8_BYTES", 1)
    with pytest.raises(ValueError, match="byte bound"):
        build_hosted_search_request(brief(), config())


def test_fake_transport_is_deterministic_and_never_performs_live_io():
    response = HostedSearchResponse("resp_test", "completed", ())
    error = HostedSearchTransportError("rate_limit", retry_after_seconds=2.0)
    transport = FakeHostedSearchTransport((response, error))
    request = build_hosted_search_request(brief(), config())

    assert transport.call(request, timeout_seconds=5.0) == response
    with pytest.raises(HostedSearchTransportError) as caught:
        transport.call(request, timeout_seconds=4.0)
    assert caught.value.code == "rate_limit"
    assert [call.timeout_seconds for call in transport.calls] == [5.0, 4.0]
    with pytest.raises(HostedSearchTransportError, match="fake_schedule_exhausted"):
        transport.call(request, timeout_seconds=1.0)


def test_fake_transport_rejects_malformed_schedule_and_request():
    transport = FakeHostedSearchTransport((object(),))
    with pytest.raises(TypeError):
        transport.call(object(), timeout_seconds=1.0)
    request = build_hosted_search_request(brief(), config())
    with pytest.raises(HostedSearchTransportError, match="malformed_fake_response"):
        transport.call(request, timeout_seconds=1.0)


@pytest.mark.parametrize("timeout", [0, True, float("nan"), "1"])
def test_fake_transport_rejects_invalid_timeouts(timeout):
    transport = FakeHostedSearchTransport(())
    request = build_hosted_search_request(brief(), config())
    with pytest.raises(ValueError, match="invalid timeout"):
        transport.call(request, timeout_seconds=timeout)
