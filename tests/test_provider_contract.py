from dataclasses import replace
from decimal import Decimal, Inexact, localcontext

import pytest
from jsonschema import Draft202012Validator

from app.core import provider_contract as contract_module
from app.core.context import (
    MAX_CONTEXT_UTF8_BYTES,
    PrivateConversationContext,
    context_utf8_size,
)
from app.core.core_self import initial_core_self
from app.core.model_contract import build_model_request
from app.core.provider_contract import (
    APPROVED_OPENAI_MODELS,
    PROMPT_VERSION,
    CostEstimateStatus,
    DeploymentStage,
    OpenAIProviderConfig,
    OpenAIRequest,
    PricingConfig,
    ProviderUsage,
    build_openai_request,
    default_openai_provider_config,
    estimate_cost,
)


def model_request():
    core_self = initial_core_self()
    history = ()
    return build_model_request(
        PrivateConversationContext(
            core_self=core_self,
            history=history,
            history_omitted=False,
            used_utf8_bytes=context_utf8_size(core_self, history),
            max_utf8_bytes=MAX_CONTEXT_UTF8_BYTES,
        )
    )


def enabled_config(**changes):
    config = OpenAIProviderConfig(
        version="openai-config-v1",
        model="gpt-6-luna",
        prompt_version=PROMPT_VERSION,
        deployment_stage=DeploymentStage.STAGE_1,
        paid_calls_enabled=True,
        max_output_tokens=512,
        timeout_seconds=10.0,
        overall_timeout_seconds=25.0,
        max_attempts=3,
        initial_backoff_seconds=1.0,
        maximum_backoff_seconds=4.0,
        maximum_jitter_seconds=0.25,
    )
    return replace(config, **changes)


def test_default_configuration_is_stage_zero_and_disabled():
    config = default_openai_provider_config()

    assert config.deployment_stage is DeploymentStage.STAGE_0
    assert config.paid_calls_enabled is False
    assert config.model in APPROVED_OPENAI_MODELS
    assert "api" not in repr(config).lower()


def test_request_builder_produces_closed_responses_contract():
    request = build_openai_request(model_request(), enabled_config())
    payload = request.to_sdk_kwargs()

    assert tuple(payload) == (
        "model",
        "instructions",
        "input",
        "text",
        "tools",
        "store",
        "background",
        "stream",
        "max_output_tokens",
        "truncation",
    )
    assert payload["model"] == "gpt-6-luna"
    assert payload["store"] is False
    assert payload["background"] is False
    assert payload["stream"] is False
    assert payload["tools"] == []
    assert payload["truncation"] == "disabled"
    assert payload["max_output_tokens"] == 512
    assert "previous_response_id" not in payload
    assert "conversation" not in payload
    assert "web_search" not in repr(payload)
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    schema = payload["text"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_generic_schema_independently_enforces_decision_fields():
    schema = build_openai_request(model_request(), enabled_config()).to_sdk_kwargs()[
        "text"
    ]["format"]["schema"]
    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object" and "anyOf" not in schema
    forbidden = {"allOf", "not", "if", "then", "else", "dependentSchemas"}

    def check_subset(node):
        if isinstance(node, dict):
            assert not forbidden.intersection(node)
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                check_subset(value)
        elif isinstance(node, list):
            for value in node:
                check_subset(value)

    check_subset(schema)
    validator = Draft202012Validator(schema)
    valid = [
        {"decision": "no_action", "reason_category": "other"},
        {"decision": "reply", "content": "Звездолёт\nлетит"},
        {"decision": "research", "brief": "Исследуй\nЮпитер"},
        {"decision": "propose_publication", "draft": "Черновик\nпубликации"},
        {"decision": "reply", "content": " "},
    ]
    assert all(validator.is_valid({"proposal": item}) for item in valid)
    invalid = [
        {"decision": "reply", "content": "ok", "reason_category": "other"},
        {"decision": "reply", "content": None},
        {"decision": "no_action", "reason_category": None},
        {"decision": "reply", "content": "ok", "draft": "also"},
        {"decision": "reply", "content": 3},
        {"decision": "reply", "content": "ok", "admin": True},
        {"decision": "reply", "content": ""},
        {"decision": "research", "brief": ""},
        {"decision": "propose_publication", "draft": ""},
    ]
    assert all(not validator.is_valid({"proposal": item}) for item in invalid)


def test_prior_nullable_contract_admitted_rejected_decision_combinations():
    old_schema = {
        "type": "object",
        "properties": {
            "decision": {
                "enum": ["no_action", "reply", "research", "propose_publication"]
            },
            "reason_category": {"type": ["string", "null"]},
            "content": {"type": ["string", "null"]},
            "brief": {"type": ["string", "null"]},
            "draft": {"type": ["string", "null"]},
        },
        "required": ["decision", "reason_category", "content", "brief", "draft"],
        "additionalProperties": False,
    }
    old = Draft202012Validator(old_schema)
    examples = [
        {
            "decision": "reply",
            "reason_category": "other",
            "content": "ok",
            "brief": None,
            "draft": None,
        },
        {
            "decision": "reply",
            "reason_category": None,
            "content": None,
            "brief": None,
            "draft": None,
        },
        {
            "decision": "no_action",
            "reason_category": None,
            "content": None,
            "brief": None,
            "draft": None,
        },
        {
            "decision": "reply",
            "reason_category": None,
            "content": "ok",
            "brief": None,
            "draft": "also",
        },
    ]
    assert all(old.is_valid(item) for item in examples)
    new = Draft202012Validator(
        build_openai_request(model_request(), enabled_config()).to_sdk_kwargs()["text"][
            "format"
        ]["schema"]
    )
    assert all(not new.is_valid({"proposal": item}) for item in examples)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "bad value"},
        {"model": "invented-model"},
        {"prompt_version": "invented-prompt"},
        {"deployment_stage": "stage_1"},
        {"paid_calls_enabled": 1},
        {"max_output_tokens": True},
        {"max_output_tokens": 0},
        {"timeout_seconds": 0.0},
        {"overall_timeout_seconds": 0.0},
        {"overall_timeout_seconds": 9.0},
        {"max_attempts": 0},
        {"max_attempts": 4},
        {"initial_backoff_seconds": -1.0},
        {"initial_backoff_seconds": -0.1},
        {"maximum_backoff_seconds": 0.5},
        {"maximum_jitter_seconds": -0.1},
    ],
)
def test_provider_configuration_rejects_untrusted_or_unbounded_values(changes):
    with pytest.raises(ValueError):
        build_openai_request(model_request(), enabled_config(**changes))


def test_request_builder_requires_exact_domain_type():
    with pytest.raises(TypeError):
        build_openai_request(object(), enabled_config())
    with pytest.raises(TypeError):
        build_openai_request(model_request(), object())


def test_provider_request_has_an_independent_serialized_byte_bound(monkeypatch):
    monkeypatch.setattr(contract_module, "MAX_PROVIDER_INPUT_UTF8_BYTES", 1)

    with pytest.raises(ValueError, match="provider request exceeds"):
        build_openai_request(model_request(), enabled_config())


def test_directly_constructed_provider_request_cannot_cross_trusted_boundary():
    forged = OpenAIRequest(
        model="gpt-6-luna",
        input_text="forged",
        max_output_tokens=1,
        _authority=object(),
    )

    with pytest.raises(ValueError, match="trusted builder"):
        forged.to_sdk_kwargs()


def test_returned_sdk_payload_cannot_mutate_the_fixed_schema():
    built = build_openai_request(model_request(), enabled_config())
    first = built.to_sdk_kwargs()
    first["text"]["format"]["schema"]["properties"]["proposal"]["anyOf"][0][
        "properties"
    ]["decision"]["const"] = "shell"

    second = built.to_sdk_kwargs()

    assert (
        second["text"]["format"]["schema"]["properties"]["proposal"]["anyOf"][0][
            "properties"
        ]["decision"]["const"]
        != "shell"
    )


def test_versioned_decimal_pricing_estimates_uncached_cached_and_output_tokens():
    usage = ProviderUsage(
        input_tokens=100,
        cached_input_tokens=40,
        output_tokens=20,
        total_tokens=120,
    )
    pricing = PricingConfig(
        version="pricing-2026-10-10",
        model="gpt-6-luna",
        currency="USD",
        input_per_million=Decimal("0.10"),
        cached_input_per_million=Decimal("0.01"),
        output_per_million=Decimal("0.50"),
    )

    estimate = estimate_cost(usage, pricing, "gpt-6-luna")

    assert estimate.status is CostEstimateStatus.ESTIMATED
    assert estimate.amount == Decimal("0.0000164")
    assert estimate.pricing_version == "pricing-2026-10-10"
    assert estimate.currency == "USD"


def test_cost_estimation_never_turns_missing_data_or_price_into_zero():
    complete = ProviderUsage(10, 0, 5, 15)
    incomplete = ProviderUsage(10, None, 5, 15)

    assert estimate_cost(None, None, "gpt-6-luna").status is (
        CostEstimateStatus.USAGE_UNAVAILABLE
    )
    assert estimate_cost(complete, None, "gpt-6-luna").status is (
        CostEstimateStatus.PRICING_UNAVAILABLE
    )
    assert estimate_cost(incomplete, None, "gpt-6-luna").status is (
        CostEstimateStatus.USAGE_INCOMPLETE
    )


@pytest.mark.parametrize(
    "usage",
    [
        ProviderUsage(-1, 0, 0, 0),
        ProviderUsage(1, 2, 0, 1),
        ProviderUsage(1, 0, 0, 2),
        ProviderUsage(True, 0, 0, 0),
    ],
)
def test_invalid_provider_usage_fails_closed(usage):
    with pytest.raises(ValueError):
        estimate_cost(usage, None, "gpt-6-luna")


def test_provider_usage_and_pricing_require_exact_contract_types():
    with pytest.raises(ValueError, match="provider usage is invalid"):
        estimate_cost(object(), None, "gpt-6-luna")
    with pytest.raises(ValueError, match="pricing configuration is invalid"):
        estimate_cost(ProviderUsage(1, 0, 1, 2), object(), "gpt-6-luna")


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "bad value"},
        {"model": "other-model"},
        {"currency": "usd"},
        {"input_per_million": Decimal("-1")},
        {"cached_input_per_million": 0},
        {"output_per_million": Decimal("NaN")},
        {"output_per_million": Decimal("1e999999")},
        {"output_per_million": Decimal("1e-999999")},
    ],
)
def test_invalid_pricing_configuration_fails_closed(changes):
    values = {
        "version": "pricing-v1",
        "model": "gpt-6-luna",
        "currency": "USD",
        "input_per_million": Decimal("0.1"),
        "cached_input_per_million": Decimal("0.01"),
        "output_per_million": Decimal("0.5"),
    }
    values.update(changes)
    with pytest.raises(ValueError):
        estimate_cost(ProviderUsage(1, 0, 1, 2), PricingConfig(**values), "gpt-6-luna")


def test_price_for_a_different_model_is_unavailable():
    pricing = PricingConfig(
        version="pricing-v1",
        model="gpt-6.1-sol",
        currency="USD",
        input_per_million=Decimal("2"),
        cached_input_per_million=Decimal("0.1"),
        output_per_million=Decimal("10"),
    )

    result = estimate_cost(ProviderUsage(1, 0, 1, 2), pricing, "gpt-6-luna")

    assert result.status is CostEstimateStatus.PRICING_UNAVAILABLE
    assert result.amount is None


def test_cost_estimate_is_isolated_from_ambient_decimal_context():
    pricing = PricingConfig(
        version="pricing-v1",
        model="gpt-6-luna",
        currency="USD",
        input_per_million=Decimal("0.123456789012"),
        cached_input_per_million=Decimal("0.012345678901"),
        output_per_million=Decimal("0.987654321098"),
    )

    with localcontext() as ambient:
        ambient.prec = 2
        ambient.traps[Inexact] = True
        result = estimate_cost(ProviderUsage(3, 1, 2, 5), pricing, "gpt-6-luna")

    assert result.amount == Decimal("0.000002234567899121")
