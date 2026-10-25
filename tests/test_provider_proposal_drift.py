"""Synthetic structural counterexamples against the actual provider boundary."""

import json

import pytest
from jsonschema import Draft202012Validator

from app.core.provider_contract import (
    build_worker_evolution_request,
    build_worker_extraction_request,
    build_worker_shared_request,
    default_openai_provider_config,
)
from app.cognition.worker_evolution import (
    EvolutionModelRequest,
    parse_evolution_provider_response,
)
from app.cognition.worker_extraction import (
    ExtractionModelRequest,
    parse_extraction_provider_response,
)
from app.cognition.worker_shared import (
    SharedProposalRequest,
    parse_shared_provider_response,
)


BUILDERS = {
    "evolution": (
        build_worker_evolution_request,
        EvolutionModelRequest("Synthetic input", ()),
        parse_evolution_provider_response,
    ),
    "extraction": (
        build_worker_extraction_request,
        ExtractionModelRequest("Synthetic input"),
        parse_extraction_provider_response,
    ),
    "shared": (
        build_worker_shared_request,
        SharedProposalRequest("Synthetic input", ()),
        parse_shared_provider_response,
    ),
}
GENERIC = {
    "decision": "new_memory",
    "handles": [],
    "kind": "candidate_useful_memory",
    "statement": "Synthetic memory",
    "confidence": 0.8,
}
EXTRACTION = {
    "decision": "new_memory",
    "kind": "candidate_useful_memory",
    "statement": "Synthetic memory",
    "confidence": 0.8,
}
SHARED = {
    "decision": "new",
    "handle": None,
    "kind": "durable_interest",
    "statement": "Synthetic memory",
    "confidence": 0.8,
}


def _schema(family):
    builder, request, _ = BUILDERS[family]
    return builder(request, default_openai_provider_config()).to_sdk_kwargs()["text"][
        "format"
    ]["schema"]


def _decode(family, proposal):
    return BUILDERS[family][2](json.dumps({"proposal": proposal}))


@pytest.mark.parametrize(
    "family,base,changes",
    [
        ("evolution", GENERIC, {"decision": "no_change"}),
        ("evolution", GENERIC, {"handles": ["h1"]}),
        ("evolution", GENERIC, {"decision": "revise_memory"}),
        ("evolution", GENERIC, {"decision": "consolidate_memories", "handles": ["h1"]}),
        ("evolution", GENERIC, {"confidence": 2}),
        ("evolution", GENERIC, {"statement": ""}),
        ("evolution", GENERIC, {"decision": "revise_memory", "handles": ["h!"]}),
        ("extraction", EXTRACTION, {"decision": "no_change"}),
        (
            "extraction",
            EXTRACTION,
            {"kind": None, "statement": None, "confidence": None},
        ),
        ("extraction", EXTRACTION, {"confidence": 2}),
        ("extraction", EXTRACTION, {"statement": ""}),
        ("shared", SHARED, {"decision": "no_change"}),
        ("shared", SHARED, {"kind": None, "statement": None, "confidence": None}),
        ("shared", SHARED, {"confidence": 2}),
        ("shared", SHARED, {"statement": ""}),
        ("shared", SHARED, {"handle": "h0123456789abcdef"}),
        ("shared", SHARED, {"decision": "revise"}),
    ],
)
def test_synthetic_drift_is_closed_by_provider_schema(family, base, changes):
    proposal = {**base, **changes}
    assert not Draft202012Validator(_schema(family)).is_valid({"proposal": proposal})
    with pytest.raises(ValueError):
        _decode(family, proposal)


def test_duplicate_handles_remain_a_local_check():
    proposal = {**GENERIC, "decision": "consolidate_memories", "handles": ["h1", "h1"]}
    assert Draft202012Validator(_schema("evolution")).is_valid({"proposal": proposal})
    with pytest.raises(ValueError):
        _decode("evolution", proposal)


@pytest.mark.parametrize(
    "family,proposal",
    [
        (
            "evolution",
            {
                "decision": "no_change",
                "handles": [],
                "kind": None,
                "statement": None,
                "confidence": None,
            },
        ),
        ("evolution", GENERIC),
        (
            "evolution",
            {
                **GENERIC,
                "decision": "revise_memory",
                "handles": ["h1"],
                "confidence": 0,
            },
        ),
        (
            "evolution",
            {
                **GENERIC,
                "decision": "consolidate_memories",
                "handles": ["h1", "h2"],
                "confidence": 1,
            },
        ),
        (
            "extraction",
            {
                "decision": "no_change",
                "kind": None,
                "statement": None,
                "confidence": None,
            },
        ),
        ("extraction", {**EXTRACTION, "confidence": 0}),
        ("extraction", {**EXTRACTION, "confidence": 1}),
        (
            "shared",
            {
                "decision": "no_change",
                "handle": None,
                "kind": None,
                "statement": None,
                "confidence": None,
            },
        ),
        ("shared", {**SHARED, "confidence": 0}),
        (
            "shared",
            {
                **SHARED,
                "decision": "revise",
                "handle": "h0123456789abcdef",
                "confidence": 1,
            },
        ),
    ],
)
def test_valid_provider_decisions_and_number_boundaries(family, proposal):
    assert Draft202012Validator(_schema(family)).is_valid({"proposal": proposal})
    assert _decode(family, proposal).decision == proposal["decision"]


@pytest.mark.parametrize(
    "family,raw,code",
    [
        ("evolution", None, "evolution_invalid_size"),
        ("evolution", "x" * 8193, "evolution_invalid_size"),
        ("evolution", "{", "evolution_invalid_json"),
        ("evolution", "\ud800", "evolution_invalid_size"),
        ("evolution", '{"proposal":{"decision":NaN}}', "evolution_invalid_json"),
        (
            "extraction",
            '{"proposal":{"decision":"no_change"}}',
            "extraction_invalid_shape",
        ),
        ("shared", '{"proposal":{"decision":"no_change"}}', "shared_invalid_shape"),
        ("evolution", '{"proposal":{},"proposal":{}}', "evolution_duplicate_key"),
        (
            "extraction",
            '{"proposal":{"decision":"no_change","decision":"new_memory"}}',
            "extraction_duplicate_key",
        ),
        ("evolution", "{}", "evolution_invalid_envelope"),
        ("evolution", '{"proposal":[]}', "evolution_invalid_shape"),
        ("evolution", '{"proposal":{"decision":"wat"}}', "evolution_invalid_shape"),
        (
            "extraction",
            '{"proposal":{"decision":"wat","kind":null,"statement":null,"confidence":null}}',
            "extraction_invalid_decision",
        ),
        (
            "shared",
            '{"proposal":{"decision":"wat","handle":null,"kind":null,"statement":null,"confidence":null}}',
            "shared_invalid_decision",
        ),
    ],
)
def test_provider_rejections_have_fixed_codes(family, raw, code):
    with pytest.raises(ValueError) as caught:
        BUILDERS[family][2](raw)
    assert caught.value.code == code
    assert raw is None or raw not in str(caught.value)


@pytest.mark.parametrize(
    "family,base,changes,code",
    [
        ("evolution", GENERIC, {"decision": "wat"}, "evolution_invalid_decision"),
        ("evolution", GENERIC, {"handles": ["h!"]}, "evolution_invalid_handle"),
        ("evolution", GENERIC, {"kind": None}, "evolution_invalid_kind"),
        ("evolution", GENERIC, {"statement": " "}, "evolution_invalid_text"),
        ("evolution", GENERIC, {"confidence": 2}, "evolution_invalid_number"),
        ("evolution", GENERIC, {"decision": "no_change"}, "evolution_invalid_shape"),
        ("extraction", EXTRACTION, {"kind": None}, "extraction_invalid_kind"),
        ("extraction", EXTRACTION, {"statement": " "}, "extraction_invalid_text"),
        ("extraction", EXTRACTION, {"confidence": 2}, "extraction_invalid_number"),
        (
            "extraction",
            EXTRACTION,
            {"decision": "no_change"},
            "extraction_invalid_shape",
        ),
        ("shared", SHARED, {"handle": "h0123456789abcdef"}, "shared_invalid_handle"),
        ("shared", SHARED, {"kind": None}, "shared_invalid_kind"),
        ("shared", SHARED, {"statement": " "}, "shared_invalid_text"),
        ("shared", SHARED, {"confidence": 2}, "shared_invalid_number"),
        ("shared", SHARED, {"decision": "no_change"}, "shared_invalid_shape"),
    ],
)
def test_family_validation_codes(family, base, changes, code):
    with pytest.raises(ValueError) as caught:
        _decode(family, {**base, **changes})
    assert caught.value.code == code


def test_local_only_whitespace_and_utf8_limits_remain_enforced():
    for family, proposal in (
        ("evolution", GENERIC),
        ("extraction", EXTRACTION),
        ("shared", SHARED),
    ):
        for statement in (" ", "x" * 4097):
            changed = {**proposal, "statement": statement}
            assert Draft202012Validator(_schema(family)).is_valid({"proposal": changed})
            with pytest.raises(ValueError):
                _decode(family, changed)


def test_all_provider_purpose_schema_decoder_pairs_are_audited():
    from app.cognition.worker_typed import TypedModelRequest
    from app.cognition.conversation_context import MemoryModelRequest
    from app.cognition.shared_context import SharedModelRequest
    from app.cognition.worker_narrative import NarrativeModelRequest
    from app.core.context import ContextRole, ContextTrust, ConversationSegment
    from app.core.provider_contract import (
        build_openai_request,
        build_worker_context_request,
        build_worker_typed_request,
        build_worker_narrative_request,
    )
    from app.trusted.openai_gateway import GatewayOutcome, _parse_response
    from test_provider_contract import model_request
    from test_openai_gateway import response

    cases = (
        (
            build_openai_request,
            model_request(),
            {"decision": "reply", "content": "Synthetic reply"},
            {},
        ),
        (
            build_worker_context_request,
            MemoryModelRequest(model_request(), (), False),
            {"decision": "reply", "content": "Synthetic reply"},
            {},
        ),
        (
            build_worker_context_request,
            SharedModelRequest(
                MemoryModelRequest(model_request(), (), False), (), False
            ),
            {"decision": "reply", "content": "Synthetic reply"},
            {},
        ),
        (
            build_worker_context_request,
            MemoryModelRequest(model_request(), (), False, narrative_mode=True),
            {"decision": "reply", "content": "Synthetic reply"},
            {},
        ),
        (
            build_worker_extraction_request,
            BUILDERS["extraction"][1],
            {
                "decision": "no_change",
                "kind": None,
                "statement": None,
                "confidence": None,
            },
            {"extraction": True},
        ),
        (
            build_worker_evolution_request,
            BUILDERS["evolution"][1],
            {
                "decision": "no_change",
                "handles": [],
                "kind": None,
                "statement": None,
                "confidence": None,
            },
            {"evolution": True},
        ),
        (
            build_worker_typed_request,
            TypedModelRequest("Synthetic input", ()),
            {
                "decision": "no_change",
                "handle": None,
                "kind": None,
                "fields": None,
                "source_role": None,
            },
            {"typed": True},
        ),
        (
            build_worker_narrative_request,
            NarrativeModelRequest(
                (
                    ConversationSegment(
                        ContextRole.PARTICIPANT,
                        ContextTrust.UNTRUSTED_CONVERSATIONAL_DATA,
                        "Synthetic input",
                    ),
                ),
                (),
            ),
            {"decision": "no_change", "handle": None, "episode": None},
            {"narrative": True},
        ),
        (
            build_worker_shared_request,
            BUILDERS["shared"][1],
            {
                "decision": "no_change",
                "handle": None,
                "kind": None,
                "statement": None,
                "confidence": None,
            },
            {"shared": True},
        ),
    )
    for builder, request, proposal, selector in cases:
        sdk = builder(request, default_openai_provider_config()).to_sdk_kwargs()
        schema = sdk["text"]["format"]["schema"]
        Draft202012Validator.check_schema(schema)
        assert schema["type"] == "object" and "anyOf" not in schema
        assert schema["additionalProperties"] is False
        assert Draft202012Validator(schema).is_valid({"proposal": proposal})
        parsed = _parse_response(
            response(json.dumps({"proposal": proposal})), **selector
        )
        assert parsed.outcome is GatewayOutcome.COMPLETED


@pytest.mark.parametrize(
    "family,proposal,code",
    [
        (
            "evolution",
            {**GENERIC, "decision": "revise_memory"},
            "evolution_invalid_handle",
        ),
        ("extraction", {**EXTRACTION, "confidence": 2}, "extraction_invalid_number"),
        ("shared", {**SHARED, "handle": "h0123456789abcdef"}, "shared_invalid_handle"),
    ],
)
def test_gateway_persists_bounded_rejection_and_actual_usage(
    tmp_path, family, proposal, code
):
    from app.trusted.openai_gateway import GatewayOutcome
    from test_openai_gateway import gateway, response

    instance, transport, store = gateway(
        tmp_path, [response(json.dumps({"proposal": proposal}))]
    )
    result = getattr(instance, f"submit_{family}")(
        BUILDERS[family][1],
        correlation_id="synthetic-correlation",
        operation_id="synthetic-operation",
    )
    attempt = store.usage_attempt("synthetic-operation:1")
    assert result.outcome is GatewayOutcome.INVALID_PROPOSAL
    assert attempt.outcome == "invalid_proposal"
    assert attempt.rejection_reason == "invalid_purpose_proposal"
    assert attempt.provider_error_code == code
    assert (
        attempt.prompt_version
        == {
            "evolution": "private-evolution-v2",
            "extraction": "private-extraction-v2",
            "shared": "shared-proposal-v2",
        }[family]
    )
    assert (attempt.input_tokens, attempt.output_tokens, attempt.total_tokens) == (
        100,
        20,
        120,
    )
    assert attempt.retry_count == 0
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "family,proposal",
    [
        ("evolution", GENERIC),
        ("extraction", EXTRACTION),
        ("shared", SHARED),
    ],
)
def test_flat_legacy_provider_output_is_rejected(family, proposal):
    with pytest.raises(ValueError) as caught:
        BUILDERS[family][2](json.dumps(proposal))
    assert caught.value.code == f"{family}_invalid_envelope"


def test_evolution_handle_and_text_boundaries_match_supported_schema():
    schema = Draft202012Validator(_schema("evolution"))
    valid = {
        **GENERIC,
        "decision": "consolidate_memories",
        "handles": [str(index) for index in range(8)],
        "statement": "x",
    }
    assert schema.is_valid({"proposal": valid})
    assert _decode("evolution", valid).handles == tuple(valid["handles"])
    for changed in (
        {**valid, "handles": [str(index) for index in range(9)]},
        {**valid, "handles": ["a" * 33, "h2"]},
        {**valid, "confidence": -0.01},
    ):
        assert not schema.is_valid({"proposal": changed})
        with pytest.raises(ValueError):
            _decode("evolution", changed)


@pytest.mark.parametrize("family", ["evolution", "extraction", "shared"])
def test_gateway_invalid_json_retains_family_code_and_usage(tmp_path, family):
    from app.trusted.openai_gateway import GatewayOutcome
    from test_openai_gateway import gateway, response

    instance, _, store = gateway(tmp_path, [response("{")])
    result = getattr(instance, f"submit_{family}")(
        BUILDERS[family][1],
        correlation_id="synthetic-correlation",
        operation_id="synthetic-operation",
    )
    attempt = store.usage_attempt("synthetic-operation:1")
    assert result.outcome is GatewayOutcome.INVALID_PROPOSAL
    assert attempt.rejection_reason == "invalid_purpose_proposal"
    assert attempt.provider_error_code == f"{family}_invalid_json"
    assert attempt.total_tokens == 120


@pytest.mark.parametrize(
    "code",
    [
        "evolution_invalid_handle",
        "extraction_invalid_number",
        "shared_invalid_text",
    ],
)
def test_acceptance_diagnostic_projection_retains_only_known_codes(code):
    from acceptance.private_memory_journey import JourneyOutcomeError

    failure = JourneyOutcomeError(
        "combined-recall", "invalid_proposal", diagnostic_code=code
    )
    assert failure.diagnostic_code == code
    unsafe = JourneyOutcomeError(
        "combined-recall", "invalid_proposal", diagnostic_code="synthetic private text"
    )
    assert unsafe.diagnostic_code == "unknown"


def test_handle_provenance_requires_the_application_snapshot():
    # These handles are syntactically valid. The provider schema and decoder have
    # no authority to decide whether the current request actually showed them.
    examples = (
        ("evolution", {**GENERIC, "decision": "revise_memory", "handles": ["unshown"]}),
        ("shared", {**SHARED, "decision": "revise", "handle": "hffffffffffffffff"}),
    )
    for family, proposal in examples:
        assert Draft202012Validator(_schema(family)).is_valid({"proposal": proposal})
        assert _decode(family, proposal).decision == proposal["decision"]


def test_typed_invalid_json_keeps_existing_gateway_classification():
    from app.trusted.openai_gateway import GatewayOutcome, _parse_response
    from test_openai_gateway import response

    parsed = _parse_response(response("{"), typed=True)
    assert parsed.outcome is GatewayOutcome.INVALID_RESPONSE
    assert parsed.diagnostic_code is None
