from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json

import pytest

from app.research.authorization import (
    SyntheticPublicResearchFixture,
    authorize_synthetic_public_research,
)
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


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


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


def authorization(subject, provider_config):
    return authorize_synthetic_public_research(
        SyntheticPublicResearchFixture.AURORAL_SUBSTORMS,
        subject,
        provider_config_version=provider_config.version,
        issued_at=NOW,
    )


def authorized_request(subject=None, provider_config=None):
    subject = subject or brief()
    provider_config = provider_config or config()
    return build_hosted_search_request(
        subject,
        provider_config,
        authorization(subject, provider_config),
        authorized_at=NOW,
    )


def test_default_config_keeps_live_research_disabled():
    value = default_hosted_search_config()

    assert value.enabled is False
    assert value.model in APPROVED_RESEARCH_MODELS
    assert "key" not in repr(value).lower()
    assert "credential" not in repr(value).lower()


def test_request_is_an_isolated_bounded_responses_web_search_call():
    request = authorized_request()
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
    payload = authorized_request().to_sdk_kwargs()
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
    subject = brief()
    provider_config = config(**changes)
    with pytest.raises(ValueError):
        build_hosted_search_request(
            subject,
            provider_config,
            authorization(subject, provider_config),
            authorized_at=NOW,
        )


def test_request_builder_requires_exact_types_and_authority():
    with pytest.raises(TypeError):
        build_hosted_search_request(object(), config(), None, authorized_at=NOW)
    with pytest.raises(TypeError):
        build_hosted_search_request(brief(), object(), None, authorized_at=NOW)

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
    with pytest.raises(ValueError, match="screened construction pipeline"):
        build_hosted_search_request(
            forged_brief, config(), authorization(brief(), config()), authorized_at=NOW
        )

    with pytest.raises(ValueError, match="authorization"):
        build_hosted_search_request(brief(), config(), None, authorized_at=NOW)

    subject = brief()
    provider_config = config()
    stale = authorize_synthetic_public_research(
        SyntheticPublicResearchFixture.AURORAL_SUBSTORMS,
        subject,
        provider_config_version=provider_config.version,
        issued_at=NOW - timedelta(minutes=10),
    )
    with pytest.raises(ValueError, match="authorization"):
        build_hosted_search_request(
            subject,
            provider_config,
            stale,
            authorized_at=NOW,
        )

    forged = HostedSearchRequest(
        model="gpt-6.1-sol",
        input_json="{}",
        external_web_access=True,
        search_context_size=SearchContextSize.LOW,
        max_output_tokens=1,
        max_search_calls=1,
        _authority=object(),
    )
    with pytest.raises(ValueError, match="trusted builder"):
        forged.to_sdk_kwargs()


def test_sdk_payload_and_trusted_request_cannot_be_mutated_after_validation():
    built = authorized_request()
    first = built.to_sdk_kwargs()
    first["tools"][0]["type"] = "function"
    first["tools"][0]["arbitrary"] = "value"

    assert built.to_sdk_kwargs()["tools"] == [
        {
            "type": "web_search",
            "external_web_access": True,
            "search_context_size": "medium",
        }
    ]

    for replaced in (
        replace(built, input_json='{"raw_history":"private"}'),
        replace(built, model="uncontrolled-model"),
        replace(built, max_search_calls=999),
        replace(built, search_context_size="medium"),
    ):
        with pytest.raises(ValueError, match="trusted builder"):
            replaced.to_sdk_kwargs()


def test_request_serialized_size_is_bounded(monkeypatch):
    import app.research.provider as provider_module

    monkeypatch.setattr(provider_module, "MAX_REQUEST_UTF8_BYTES", 1)
    with pytest.raises(ValueError, match="byte bound"):
        authorized_request()


def test_fake_transport_is_deterministic_and_never_performs_live_io():
    response = HostedSearchResponse("resp_test", "completed", ())
    error = HostedSearchTransportError("rate_limit", retry_after_seconds=2.0)
    transport = FakeHostedSearchTransport((response, error))
    request = authorized_request()

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
    request = authorized_request()
    with pytest.raises(HostedSearchTransportError, match="malformed_fake_response"):
        transport.call(request, timeout_seconds=1.0)


@pytest.mark.parametrize("timeout", [0, True, float("nan"), "1"])
def test_fake_transport_rejects_invalid_timeouts(timeout):
    transport = FakeHostedSearchTransport(())
    request = authorized_request()
    with pytest.raises(ValueError, match="invalid timeout"):
        transport.call(request, timeout_seconds=timeout)
