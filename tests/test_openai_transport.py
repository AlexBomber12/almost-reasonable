import sys
from types import SimpleNamespace

import pytest

from app.core.context import (
    MAX_CONTEXT_UTF8_BYTES,
    PrivateConversationContext,
    context_utf8_size,
)
from app.core.core_self import initial_core_self
from app.core.model_contract import build_model_request
from app.core.provider_contract import (
    PROMPT_VERSION,
    DeploymentStage,
    OpenAIProviderConfig,
    build_openai_request,
)
from app.trusted.openai_transport import (
    FakeOpenAITransport,
    OpenAIResponsesTransport,
    OpenAITransportError,
    ProviderOutput,
    TrustedTransportConfigurationError,
)


def provider_request():
    core_self = initial_core_self()
    domain_request = build_model_request(
        PrivateConversationContext(
            core_self,
            (),
            False,
            context_utf8_size(core_self, ()),
            MAX_CONTEXT_UTF8_BYTES,
        )
    )
    config = OpenAIProviderConfig(
        version="openai-config-v1",
        model="gpt-6-luna",
        prompt_version=PROMPT_VERSION,
        deployment_stage=DeploymentStage.STAGE_1,
        paid_calls_enabled=True,
        max_output_tokens=512,
        timeout_seconds=10.0,
        overall_timeout_seconds=20.0,
        max_attempts=2,
        initial_backoff_seconds=0.5,
        maximum_backoff_seconds=1.0,
        maximum_jitter_seconds=0.1,
    )
    return build_openai_request(domain_request, config)


def documented_response():
    return {
        "id": "resp_synthetic",
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": (
                            '{"decision":"reply","reason_category":null,'
                            '"content":"Здравствуйте","brief":null,"draft":null}'
                        ),
                        "annotations": [],
                    }
                ],
            },
        ],
        "usage": {
            "input_tokens": 20,
            "input_tokens_details": {"cached_tokens": 5},
            "output_tokens": 8,
            "total_tokens": 28,
        },
    }


class RecordingResponses:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_sdk_transport_sends_exact_closed_request_and_normalizes_response():
    responses = RecordingResponses(documented_response())
    transport = OpenAIResponsesTransport(SimpleNamespace(responses=responses))

    result = transport.call(provider_request(), timeout_seconds=7.5)

    assert responses.calls == [{**provider_request().to_sdk_kwargs(), "timeout": 7.5}]
    assert result.response_id == "resp_synthetic"
    assert result.status == "completed"
    assert [item.type for item in result.output] == ["reasoning", "message"]
    assert result.output[1].content[0].type == "output_text"
    assert result.usage.input_tokens == 20
    assert result.usage.cached_input_tokens == 5
    assert result.usage.output_tokens == 8
    assert result.usage.total_tokens == 28
    assert "Здравствуйте" not in repr(result)


def test_transport_accepts_sdk_models_with_model_dump():
    class SDKResponse:
        def model_dump(self, *, mode):
            assert mode == "json"
            return documented_response()

    result = OpenAIResponsesTransport(
        SimpleNamespace(responses=RecordingResponses(SDKResponse()))
    ).call(provider_request(), timeout_seconds=1.0)

    assert result.response_id == "resp_synthetic"


def test_transport_accepts_reasoning_item_with_null_content():
    raw = documented_response()
    raw["output"][0]["content"] = None
    transport = OpenAIResponsesTransport(
        SimpleNamespace(responses=RecordingResponses(raw))
    )

    result = transport.call(provider_request(), timeout_seconds=1.0)

    assert result.output[0] == ProviderOutput("reasoning", ())


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        {"output": "not-a-list"},
        {"output": ["not-an-object"]},
        {"usage": "not-an-object"},
        {"usage": {"input_tokens": -1}},
    ],
)
def test_malformed_sdk_response_fails_with_sanitized_error(mutation):
    raw = object() if mutation is None else {**documented_response(), **mutation}
    transport = OpenAIResponsesTransport(
        SimpleNamespace(responses=RecordingResponses(raw))
    )

    with pytest.raises(
        OpenAITransportError, match="provider transport failed"
    ) as caught:
        transport.call(provider_request(), timeout_seconds=1.0)

    assert caught.value.error_code == "malformed_provider_response"
    assert "not-a-list" not in repr(caught.value)


def test_sdk_exception_is_reduced_to_documented_structured_error_fields():
    class SyntheticRateLimit(Exception):
        status_code = 429
        body = {
            "error": {
                "type": "rate_limit_error",
                "code": "slow_down",
                "message": "private provider detail",
            }
        }
        response = SimpleNamespace(headers={"retry-after": "2.5"})

    transport = OpenAIResponsesTransport(
        SimpleNamespace(responses=RecordingResponses(SyntheticRateLimit("secret")))
    )

    with pytest.raises(OpenAITransportError) as caught:
        transport.call(provider_request(), timeout_seconds=1.0)

    assert caught.value.status_code == 429
    assert caught.value.error_type == "rate_limit_error"
    assert caught.value.error_code == "slow_down"
    assert caught.value.retry_after_seconds == 2.5
    assert str(caught.value) == "provider transport failed"
    assert "secret" not in repr(caught.value)
    assert "private provider detail" not in repr(caught.value)


def test_fake_transport_records_calls_and_schedules_results():
    expected = OpenAIResponsesTransport(
        SimpleNamespace(responses=RecordingResponses(documented_response()))
    ).call(provider_request(), timeout_seconds=1.0)
    failure = OpenAITransportError(error_code="server_error", status_code=500)
    fake = FakeOpenAITransport([failure, expected])

    with pytest.raises(OpenAITransportError):
        fake.call(provider_request(), timeout_seconds=2.0)
    assert fake.call(provider_request(), timeout_seconds=3.0) == expected
    assert [call.timeout_seconds for call in fake.calls] == [2.0, 3.0]
    with pytest.raises(OpenAITransportError) as exhausted:
        fake.call(provider_request(), timeout_seconds=1.0)
    assert exhausted.value.error_code == "fake_schedule_exhausted"


def test_transport_factory_is_explicit_and_credential_holding(monkeypatch):
    created = []

    class SyntheticOpenAI:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.responses = RecordingResponses(documented_response())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=SyntheticOpenAI))

    with pytest.raises(TrustedTransportConfigurationError):
        OpenAIResponsesTransport.from_environment({})
    transport = OpenAIResponsesTransport.from_environment(
        {"OPENAI_API_KEY": "TEST-ONLY-OPENAI-CANARY"}
    )

    assert isinstance(transport, OpenAIResponsesTransport)
    assert created == [
        {
            "api_key": "TEST-ONLY-OPENAI-CANARY",
            "base_url": "https://api.openai.com/v1",
            "max_retries": 0,
        }
    ]
    assert "TEST-ONLY-OPENAI-CANARY" not in repr(transport)


def test_transport_rejects_invalid_client_request_and_timeout(monkeypatch):
    with pytest.raises(TypeError):
        OpenAIResponsesTransport(object())
    transport = OpenAIResponsesTransport(
        SimpleNamespace(responses=RecordingResponses(documented_response()))
    )
    with pytest.raises(TypeError):
        transport.call(object(), timeout_seconds=1.0)
    for timeout in (0.0, float("nan"), True):
        with pytest.raises(ValueError):
            transport.call(provider_request(), timeout_seconds=timeout)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(TrustedTransportConfigurationError):
        OpenAIResponsesTransport.from_environment()


def test_missing_sdk_is_a_sanitized_configuration_failure(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)

    with pytest.raises(TrustedTransportConfigurationError, match="SDK is unavailable"):
        OpenAIResponsesTransport.from_environment(
            {"OPENAI_API_KEY": "TEST-ONLY-OPENAI-CANARY"}
        )


def test_existing_sanitized_transport_error_is_rethrown_unchanged():
    expected = OpenAITransportError(error_code="server_error")
    transport = OpenAIResponsesTransport(
        SimpleNamespace(responses=RecordingResponses(expected))
    )

    with pytest.raises(OpenAITransportError) as caught:
        transport.call(provider_request(), timeout_seconds=1.0)

    assert caught.value is expected


def test_fake_transport_rejects_nonresponse_schedule_value():
    fake = FakeOpenAITransport([object()])

    with pytest.raises(OpenAITransportError) as caught:
        fake.call(provider_request(), timeout_seconds=1.0)

    assert caught.value.error_code == "malformed_fake_response"


@pytest.mark.parametrize(
    ("body", "headers", "expected_code", "expected_delay"),
    [
        ({"type": "invalid_request_error", "code": "bad"}, {}, "bad", None),
        ({}, {"retry-after": "nan"}, None, None),
        ({}, {"retry-after": "-1"}, None, None),
        ({}, {"retry-after": "999999"}, None, None),
    ],
)
def test_exception_metadata_extraction_is_bounded(
    body, headers, expected_code, expected_delay
):
    error = RuntimeError("private")
    error.status_code = 400
    error.body = body
    error.response = SimpleNamespace(headers=headers)
    transport = OpenAIResponsesTransport(
        SimpleNamespace(responses=RecordingResponses(error))
    )

    with pytest.raises(OpenAITransportError) as caught:
        transport.call(provider_request(), timeout_seconds=1.0)

    assert caught.value.error_code == expected_code
    assert caught.value.retry_after_seconds == expected_delay


@pytest.mark.parametrize(
    "mutation",
    [
        {"id": None, "usage": None},
        {"id": "bad value"},
        {"status": "bad value"},
        {
            "output": [
                {"type": "message", "content": "not-a-list"},
            ]
        },
        {
            "output": [
                {"type": "message", "content": ["not-an-object"]},
            ]
        },
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text"}],
                },
            ]
        },
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "refusal", "refusal": "no"}],
                },
            ]
        },
        {"usage": {"input_tokens_details": "bad"}},
        {
            "usage": {
                "input_tokens": 1,
                "input_tokens_details": {"cached_tokens": 2},
            }
        },
        {
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 3,
            }
        },
    ],
)
def test_additional_sdk_response_shapes_are_safely_normalized(mutation):
    raw = {**documented_response(), **mutation}
    transport = OpenAIResponsesTransport(
        SimpleNamespace(responses=RecordingResponses(raw))
    )

    if mutation in (
        {"id": None, "usage": None},
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text"}],
                },
            ]
        },
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "refusal", "refusal": "no"}],
                },
            ]
        },
    ):
        result = transport.call(provider_request(), timeout_seconds=1.0)
        if mutation == {"id": None, "usage": None}:
            assert result.response_id is None
            assert result.usage is None
        elif "text" not in mutation["output"][0]["content"][0]:
            assert result.output[0].content[0].text is None
    else:
        with pytest.raises(OpenAITransportError):
            transport.call(provider_request(), timeout_seconds=1.0)


def test_sdk_error_without_structured_metadata_and_named_network_errors():
    for error_name, expected_flag in (
        ("APITimeoutError", "timeout"),
        ("APIConnectionError", "connection"),
    ):
        error_type = type(error_name, (Exception,), {})
        error = error_type("private")
        error.status_code = "not-an-int"
        error.body = "private body"
        error.response = SimpleNamespace(headers=object())
        transport = OpenAIResponsesTransport(
            SimpleNamespace(responses=RecordingResponses(error))
        )

        with pytest.raises(OpenAITransportError) as caught:
            transport.call(provider_request(), timeout_seconds=1.0)

        assert caught.value.status_code is None
        assert getattr(caught.value, expected_flag) is True
