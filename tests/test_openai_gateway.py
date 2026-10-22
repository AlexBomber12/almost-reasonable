from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import json
from threading import Event, Thread

import pytest

from app.core.action_policy import ActionPolicy, CapabilityState, TrustedActionContext
from app.trusted import openai_gateway as gateway_module
from app.core.agent_proposals import ReplyProposal
from app.core.context import (
    MAX_CONTEXT_UTF8_BYTES,
    PrivateConversationContext,
    context_utf8_size,
)
from app.core.core_self import initial_core_self
from app.core.events import (
    AuthenticatedSourceReference,
    ParticipantId,
    TrustedPrincipalContext,
)
from app.core.model_contract import build_model_request
from app.core.provider_contract import (
    PROMPT_VERSION,
    DeploymentStage,
    OpenAIProviderConfig,
    PricingConfig,
    ProviderUsage,
)
from app.core.provider_spend import ProviderStateStore
from app.core.provider_spend import ProviderPersistenceError
from app.core.store import EventStore
from app.trusted.openai_gateway import (
    GatewayOutcome,
    TrustedOpenAIGateway,
)
from app.trusted.openai_transport import (
    FakeOpenAITransport,
    OpenAITransportError,
    ProviderContent,
    ProviderOutput,
    ProviderResponse,
)


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def request():
    core_self = initial_core_self()
    return build_model_request(
        PrivateConversationContext(
            core_self,
            (),
            False,
            context_utf8_size(core_self, ()),
            MAX_CONTEXT_UTF8_BYTES,
        )
    )


def config(**changes):
    result = OpenAIProviderConfig(
        version="openai-config-v1",
        model="gpt-6-luna",
        prompt_version=PROMPT_VERSION,
        deployment_stage=DeploymentStage.STAGE_1,
        paid_calls_enabled=True,
        max_output_tokens=512,
        timeout_seconds=5.0,
        overall_timeout_seconds=20.0,
        max_attempts=3,
        initial_backoff_seconds=1.0,
        maximum_backoff_seconds=4.0,
        maximum_jitter_seconds=0.25,
    )
    return replace(result, **changes)


def response(text=None, *, status="completed", output=None, usage=True):
    if text is None:
        text = '{"proposal":{"decision":"reply","content":"Синтетический ответ"}}'
    if output is None:
        output = (
            ProviderOutput("reasoning", ()),
            ProviderOutput("message", (ProviderContent("output_text", text=None),)),
        )
        output = (
            output[0],
            ProviderOutput("message", (ProviderContent("output_text", text=text),)),
        )
    provider_usage = ProviderUsage(100, 40, 20, 120) if usage else None
    return ProviderResponse(
        response_id="resp_synthetic",
        status=status,
        output=output,
        usage=provider_usage,
    )


def gateway(tmp_path, outcomes, **changes):
    transport = FakeOpenAITransport(outcomes)
    store = ProviderStateStore(EventStore(tmp_path / "events.sqlite3"))
    times = changes.pop("times", None)
    if times is None:

        def monotonic():
            return 0.0
    else:
        iterator = iter(times)

        def monotonic():
            return next(iterator)

    instance = TrustedOpenAIGateway(
        store=store,
        transport=transport,
        config=changes.pop("provider_config", config()),
        pricing=changes.pop("pricing", None),
        emergency_stop=changes.pop("emergency_stop", lambda: False),
        monotonic=monotonic,
        utcnow=changes.pop("utcnow", lambda: NOW),
        sleeper=changes.pop("sleeper", lambda _delay: None),
        jitter=changes.pop("jitter", lambda _attempt, _maximum: 0.0),
    )
    assert not changes
    return instance, transport, store


def test_success_is_locally_validated_and_records_versioned_usage_and_cost(tmp_path):
    pricing = PricingConfig(
        version="pricing-v1",
        model="gpt-6-luna",
        currency="USD",
        input_per_million=Decimal("0.10"),
        cached_input_per_million=Decimal("0.01"),
        output_per_million=Decimal("0.50"),
    )
    instance, transport, store = gateway(tmp_path, [response()], pricing=pricing)

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.COMPLETED
    assert result.validation.valid is True
    assert result.validation.proposal == ReplyProposal("Синтетический ответ")
    assert result.safe_russian_message is None
    assert result.attempts == 1
    assert len(transport.calls) == 1
    record = store.usage_attempt("operation-a:1")
    assert record.input_tokens == 100
    assert record.cached_input_tokens == 40
    assert record.output_tokens == 20
    assert record.total_tokens == 120
    assert record.estimated_cost == "0.0000164"
    assert record.pricing_version == "pricing-v1"
    assert record.provider_response_id == "resp_synthetic"


def test_sanitized_gateway_result_can_enter_existing_action_policy(tmp_path):
    instance, _transport, _store = gateway(tmp_path, [response()])
    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )
    principal = TrustedPrincipalContext(
        principal_id=ParticipantId("participant-a"),
        authenticated_source=AuthenticatedSourceReference("session-a"),
        is_owner=False,
    )

    decision = ActionPolicy().evaluate(
        result.validation,
        TrustedActionContext("correlation-a", principal),
        CapabilityState(
            version="ops-v1",
            emergency_stop_active=False,
            provider_spend_suspended=False,
            private_reply_drafts_enabled=True,
            publication_drafts_enabled=False,
            simulation_only=True,
        ),
    )

    assert decision.proposal == ReplyProposal("Синтетический ответ")
    assert decision.dispatchable is True


def test_synthetic_credential_is_absent_from_requests_results_and_telemetry(
    tmp_path, monkeypatch
):
    canary = "TEST-ONLY-OPENAI-CANARY-NEVER-SEND"
    monkeypatch.setenv("OPENAI_API_KEY", canary)
    instance, transport, store = gateway(tmp_path, [response()])

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )
    visible = (
        repr(config()),
        repr(transport.calls),
        repr(result),
        repr(store.usage_attempt("operation-a:1")),
        repr(transport.calls[0].request.to_sdk_kwargs()),
    )

    assert all(canary not in item for item in visible)


def test_default_stage_zero_and_environment_key_do_not_enable_transport(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "TEST-ONLY-OPENAI-CANARY")
    instance, transport, store = gateway(
        tmp_path,
        [response()],
        provider_config=replace(
            config(),
            deployment_stage=DeploymentStage.STAGE_0,
            paid_calls_enabled=False,
        ),
    )

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.DISABLED
    assert result.validation is None
    assert result.safe_russian_message
    assert transport.calls == []
    assert store.usage_attempt_count() == 0


@pytest.mark.parametrize(
    ("provider_response", "outcome"),
    [
        (response(status="incomplete"), GatewayOutcome.INCOMPLETE),
        (response(status="failed"), GatewayOutcome.INVALID_RESPONSE),
        (
            response(
                output=(
                    ProviderOutput("message", (ProviderContent("refusal", text=None),)),
                )
            ),
            GatewayOutcome.REFUSED,
        ),
        (
            response(output=(ProviderOutput("function_call", ()),)),
            GatewayOutcome.UNSUPPORTED_OUTPUT,
        ),
        (
            response(output=(ProviderOutput("message", ()),)),
            GatewayOutcome.MISSING_OUTPUT,
        ),
        (response(text="not-json"), GatewayOutcome.INVALID_RESPONSE),
        (
            response(
                text=(
                    '{"decision":"reply","reason_category":null,'
                    '"content":"ok","brief":null,"draft":null,"admin":true}'
                )
            ),
            GatewayOutcome.INVALID_RESPONSE,
        ),
        (
            response(text=('{"proposal":{"decision":"shell"}}')),
            GatewayOutcome.INVALID_PROPOSAL,
        ),
        (
            response(
                text=(
                    '{"decision":[],"reason_category":null,'
                    '"content":null,"brief":null,"draft":null}'
                )
            ),
            GatewayOutcome.INVALID_RESPONSE,
        ),
        (
            response(
                text=(
                    '{"decision":{},"reason_category":null,'
                    '"content":null,"brief":null,"draft":null}'
                )
            ),
            GatewayOutcome.INVALID_RESPONSE,
        ),
        (
            response(text="я" * 9000),
            GatewayOutcome.OVERSIZED_OUTPUT,
        ),
    ],
)
def test_untrusted_provider_outputs_fail_closed_without_retry(
    tmp_path, provider_response, outcome
):
    instance, transport, store = gateway(tmp_path, [provider_response])

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is outcome
    assert result.safe_russian_message
    assert result.validation is None or result.validation.valid is False
    assert len(transport.calls) == 1
    assert store.usage_attempt_count() == 1
    attempt = store.usage_attempt("operation-a:1")
    assert attempt.rejection_reason in {
        outcome.value,
        "invalid_json",
        "invalid_envelope",
        "unknown_decision",
    }
    assert attempt.provider_error_code is None


@pytest.mark.parametrize(
    ("error", "outcome", "calls"),
    [
        (
            OpenAITransportError(
                status_code=429,
                error_type="rate_limit_error",
                error_code="rate_limit_exceeded",
            ),
            GatewayOutcome.RATE_LIMIT,
            3,
        ),
        (
            OpenAITransportError(status_code=500, error_code="server_error"),
            GatewayOutcome.SERVER_ERROR,
            3,
        ),
        (OpenAITransportError(timeout=True), GatewayOutcome.TIMEOUT, 3),
        (OpenAITransportError(connection=True), GatewayOutcome.CONNECTION_ERROR, 3),
        (
            OpenAITransportError(status_code=401, error_code="invalid_api_key"),
            GatewayOutcome.AUTHENTICATION_ERROR,
            1,
        ),
        (
            OpenAITransportError(status_code=400, error_code="invalid_request"),
            GatewayOutcome.INVALID_REQUEST,
            1,
        ),
        (
            OpenAITransportError(status_code=403, error_code="permission_denied"),
            GatewayOutcome.PERMISSION_ERROR,
            1,
        ),
        (
            OpenAITransportError(
                status_code=429,
                error_type="insufficient_quota",
                error_code="credit_balance_exhausted",
            ),
            GatewayOutcome.BILLING_QUOTA,
            1,
        ),
        (
            OpenAITransportError(status_code=429),
            GatewayOutcome.AMBIGUOUS_QUOTA,
            1,
        ),
        (OpenAITransportError(status_code=418), GatewayOutcome.UNKNOWN_ERROR, 1),
    ],
)
def test_provider_error_classification_and_retry_bounds(
    tmp_path, error, outcome, calls
):
    sleeps = []
    instance, transport, store = gateway(
        tmp_path,
        [error] * calls,
        sleeper=sleeps.append,
        jitter=lambda attempt, maximum: min(attempt / 10, maximum),
    )

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is outcome
    assert result.attempts == calls
    assert len(transport.calls) == calls
    assert store.usage_attempt_count() == calls
    assert len(sleeps) == calls - 1
    assert all(delay <= 4.25 for delay in sleeps)


def test_transient_failure_can_retry_then_succeed(tmp_path):
    sleeps = []
    instance, transport, store = gateway(
        tmp_path,
        [OpenAITransportError(status_code=503), response()],
        sleeper=sleeps.append,
    )

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.COMPLETED
    assert result.attempts == 2
    assert len(transport.calls) == 2
    assert sleeps == [1.0]
    assert store.usage_attempt_count() == 2


@pytest.mark.parametrize(
    "code",
    ["project_spend_limit_exceeded", "organization_spend_limit_exceeded"],
)
def test_confirmed_spend_limit_persists_before_reporting_and_blocks_restart(
    tmp_path, code
):
    error = OpenAITransportError(
        status_code=429, error_type="insufficient_quota", error_code=code
    )
    instance, transport, store = gateway(tmp_path, [error])

    first = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )
    restarted_transport = FakeOpenAITransport([response()])
    restarted = TrustedOpenAIGateway(
        store=ProviderStateStore(EventStore(store._event_store.database_path)),
        transport=restarted_transport,
        config=config(),
    )
    second = restarted.submit(
        request(), correlation_id="correlation-b", operation_id="operation-b"
    )

    assert first.outcome is GatewayOutcome.SPEND_LIMIT_CONFIRMED
    assert store.is_spend_suspended() is True
    assert second.outcome is GatewayOutcome.SPEND_SUSPENDED
    assert len(transport.calls) == 1
    assert restarted_transport.calls == []


def test_emergency_stop_is_checked_before_every_attempt(tmp_path):
    checks = iter([False, False, True])
    instance, transport, store = gateway(
        tmp_path,
        [OpenAITransportError(status_code=500), response()],
        emergency_stop=lambda: next(checks),
    )

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.EMERGENCY_STOP
    assert len(transport.calls) == 1
    assert store.usage_attempt_count() == 1


def test_stop_activated_during_reservation_blocks_provider_call(tmp_path):
    checks = iter([False, True])
    instance, transport, store = gateway(
        tmp_path, [response()], emergency_stop=lambda: next(checks)
    )

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.EMERGENCY_STOP
    assert result.attempts == 0
    assert transport.calls == []
    assert store.usage_attempt("operation-a:1").outcome == "emergency_stop"


def test_preexisting_stop_blocks_without_transport_or_telemetry(tmp_path):
    instance, transport, store = gateway(
        tmp_path, [response()], emergency_stop=lambda: True
    )

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.EMERGENCY_STOP
    assert transport.calls == []
    assert store.usage_attempt_count() == 0


def test_duplicate_operation_identifier_is_blocked_before_another_paid_call(tmp_path):
    instance, transport, store = gateway(tmp_path, [response(), response()])
    first = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )
    second = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert first.outcome is GatewayOutcome.COMPLETED
    assert second.outcome is GatewayOutcome.DUPLICATE_OPERATION
    assert len(transport.calls) == 1
    assert store.usage_attempt_count() == 1


def test_concurrent_duplicate_is_reserved_before_provider_contact(tmp_path):
    entered = Event()
    release = Event()

    class BlockingTransport:
        def __init__(self):
            self.call_count = 0

        def call(self, _request, *, timeout_seconds):
            self.call_count += 1
            entered.set()
            assert release.wait(timeout=5)
            return response()

    transport = BlockingTransport()
    store = ProviderStateStore(EventStore(tmp_path / "events.sqlite3"))
    instances = [
        TrustedOpenAIGateway(store=store, transport=transport, config=config())
        for _ in range(2)
    ]
    results = []
    worker = Thread(
        target=lambda: results.append(
            instances[0].submit(
                request(),
                correlation_id="correlation-a",
                operation_id="operation-a",
            )
        )
    )
    worker.start()
    assert entered.wait(timeout=5)
    duplicate = instances[1].submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )
    release.set()
    worker.join(timeout=5)

    assert worker.is_alive() is False
    assert duplicate.outcome is GatewayOutcome.DUPLICATE_OPERATION
    assert results[0].outcome is GatewayOutcome.COMPLETED
    assert transport.call_count == 1
    assert store.usage_attempt_count() == 1


def test_missing_usage_is_explicitly_unknown(tmp_path):
    instance, _transport, store = gateway(tmp_path, [response(usage=False)])

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.COMPLETED
    record = store.usage_attempt("operation-a:1")
    assert record.input_tokens is None
    assert record.estimated_cost is None
    assert record.cost_estimate_status.value == "usage_unavailable"


def test_invalid_identifiers_fail_before_transport(tmp_path):
    instance, transport, _store = gateway(tmp_path, [response()])

    with pytest.raises(ValueError):
        instance.submit(request(), correlation_id="bad value", operation_id="ok")
    with pytest.raises(ValueError):
        instance.submit(request(), correlation_id="ok", operation_id="bad value")
    with pytest.raises(TypeError):
        instance.submit(object(), correlation_id="ok", operation_id="operation")

    assert transport.calls == []


def test_gateway_requires_provider_state_store(tmp_path):
    with pytest.raises(TypeError):
        TrustedOpenAIGateway(
            store=object(),
            transport=FakeOpenAITransport([]),
            config=config(),
        )


@pytest.mark.parametrize(
    "bad_pricing",
    [
        PricingConfig(
            "pricing-v1",
            "gpt-6-luna",
            "EUR",
            Decimal("0.1"),
            Decimal("0.01"),
            Decimal("0.5"),
        ),
        PricingConfig(
            "pricing-v1",
            "gpt-6-luna",
            "USD",
            Decimal("1e999999"),
            Decimal("0.01"),
            Decimal("0.5"),
        ),
        PricingConfig(
            "pricing-v1",
            "gpt-6-luna",
            "USD",
            Decimal("-0.1"),
            Decimal("0.01"),
            Decimal("0.5"),
        ),
    ],
)
def test_malformed_pricing_is_rejected_before_provider_contact(tmp_path, bad_pricing):
    transport = FakeOpenAITransport([response()])
    store = ProviderStateStore(EventStore(tmp_path / "events.sqlite3"))

    with pytest.raises(ValueError):
        TrustedOpenAIGateway(
            store=store,
            transport=transport,
            config=config(),
            pricing=bad_pricing,
        )

    assert transport.calls == []


def test_operation_identifier_reserves_space_for_attempt_suffix(tmp_path):
    instance, transport, _store = gateway(tmp_path, [response()])

    accepted = instance.submit(
        request(), correlation_id="correlation-a", operation_id="o" * 126
    )
    with pytest.raises(ValueError, match="invalid operation id"):
        instance.submit(
            request(), correlation_id="correlation-b", operation_id="o" * 127
        )

    assert accepted.outcome is GatewayOutcome.COMPLETED
    assert len(transport.calls) == 1


def test_persistence_preflight_failure_latches_gateway_closed(tmp_path, monkeypatch):
    instance, transport, store = gateway(tmp_path, [response()])

    def fail(*_args):
        raise ProviderPersistenceError("private detail")

    monkeypatch.setattr(store, "reserve_attempt", fail)

    first = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )
    second = instance.submit(
        request(), correlation_id="correlation-b", operation_id="operation-b"
    )

    assert first.outcome is GatewayOutcome.PERSISTENCE_FAILURE
    assert second.outcome is GatewayOutcome.PERSISTENCE_FAILURE
    assert transport.calls == []


@pytest.mark.parametrize("provider_failure", [False, True])
def test_persistence_failure_after_provider_contact_fails_closed(
    tmp_path, monkeypatch, provider_failure
):
    outcome = OpenAITransportError(status_code=500) if provider_failure else response()
    instance, transport, store = gateway(tmp_path, [outcome])

    def fail(*_args, **_kwargs):
        raise ProviderPersistenceError("private detail")

    monkeypatch.setattr(store, "record_attempt", fail)

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.PERSISTENCE_FAILURE
    assert len(transport.calls) == 1


def test_duplicate_persistence_result_fails_closed(tmp_path, monkeypatch):
    instance, _transport, store = gateway(tmp_path, [response()])
    monkeypatch.setattr(store, "record_attempt", lambda *_args, **_kwargs: False)

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.PERSISTENCE_FAILURE


def test_overall_deadline_blocks_transport_before_attempt(tmp_path):
    instance, transport, _store = gateway(tmp_path, [response()], times=[0.0, 21.0])

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.TIMEOUT
    assert transport.calls == []


def test_response_at_overall_deadline_is_recorded_as_timeout(tmp_path):
    instance, transport, store = gateway(
        tmp_path, [response()], times=[0.0, 0.0, 0.0, 20.0]
    )

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.TIMEOUT
    assert result.validation is None
    assert len(transport.calls) == 1
    record = store.usage_attempt("operation-a:1")
    assert record.outcome == "timeout"
    assert record.provider_response_id == "resp_synthetic"
    assert record.total_tokens == 120


@pytest.mark.parametrize(
    ("error", "expected", "suspended"),
    [
        (OpenAITransportError(status_code=503), GatewayOutcome.TIMEOUT, False),
        (
            OpenAITransportError(
                status_code=429,
                error_type="insufficient_quota",
                error_code="project_spend_limit_exceeded",
            ),
            GatewayOutcome.SPEND_LIMIT_CONFIRMED,
            True,
        ),
    ],
)
def test_error_at_overall_deadline_preserves_only_confirmed_spend_limit(
    tmp_path, error, expected, suspended
):
    instance, transport, store = gateway(tmp_path, [error], times=[0.0, 0.0, 0.0, 20.0])

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is expected
    assert len(transport.calls) == 1
    assert store.usage_attempt("operation-a:1").outcome == expected.value
    assert store.is_spend_suspended() is suspended


def test_reservation_that_consumes_deadline_blocks_provider_call(tmp_path):
    instance, transport, store = gateway(tmp_path, [response()], times=[0.0, 0.0, 20.0])

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.TIMEOUT
    assert result.attempts == 0
    assert transport.calls == []
    assert store.usage_attempt("operation-a:1").outcome == "timeout"


def test_pre_call_deadline_persistence_failure_fails_closed(tmp_path, monkeypatch):
    instance, transport, store = gateway(tmp_path, [response()], times=[0.0, 0.0, 20.0])

    def fail(*_args, **_kwargs):
        raise ProviderPersistenceError("private detail")

    monkeypatch.setattr(store, "record_attempt", fail)
    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.PERSISTENCE_FAILURE
    assert transport.calls == []


def test_retry_stops_when_backoff_would_exceed_overall_deadline(tmp_path):
    instance, transport, store = gateway(
        tmp_path,
        [OpenAITransportError(status_code=500), response()],
        times=[0.0, 0.0, 0.0, 19.5],
    )

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.SERVER_ERROR
    assert len(transport.calls) == 1
    assert store.usage_attempt_count() == 1


def test_retry_after_above_trusted_backoff_skips_retry(tmp_path):
    sleeps = []
    instance, transport, _store = gateway(
        tmp_path,
        [
            OpenAITransportError(
                status_code=429,
                error_type="rate_limit_error",
                retry_after_seconds=10.0,
            ),
            response(),
        ],
        sleeper=sleeps.append,
    )

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.RATE_LIMIT
    assert len(transport.calls) == 1
    assert sleeps == []


def test_retry_after_within_bound_is_a_minimum_delay(tmp_path):
    sleeps = []
    instance, transport, _store = gateway(
        tmp_path,
        [
            OpenAITransportError(
                status_code=429,
                error_type="rate_limit_error",
                retry_after_seconds=2.0,
            ),
            response(),
        ],
        sleeper=sleeps.append,
    )

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is GatewayOutcome.COMPLETED
    assert len(transport.calls) == 2
    assert sleeps == [2.0]


def test_invalid_injected_clock_jitter_and_duration_fail_closed(tmp_path):
    clock_path = tmp_path / "clock"
    jitter_path = tmp_path / "jitter"
    backwards_path = tmp_path / "backwards"
    clock_path.mkdir()
    jitter_path.mkdir()
    backwards_path.mkdir()
    invalid_clock, _, _ = gateway(clock_path, [response()], times=[float("nan")])
    with pytest.raises(ValueError, match="clock"):
        invalid_clock.submit(
            request(), correlation_id="correlation-a", operation_id="operation-a"
        )

    invalid_jitter, _, _ = gateway(
        jitter_path,
        [OpenAITransportError(status_code=500)],
        jitter=lambda _attempt, _maximum: -0.1,
    )
    with pytest.raises(ValueError, match="jitter"):
        invalid_jitter.submit(
            request(), correlation_id="correlation-a", operation_id="operation-a"
        )

    backwards, _, _ = gateway(backwards_path, [response()], times=[0.0, 2.0, 2.0, 1.0])
    result = backwards.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )
    assert result.outcome is GatewayOutcome.PERSISTENCE_FAILURE


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            OpenAITransportError(
                status_code=429,
                error_type="insufficient_quota",
                error_code="undocumented_quota",
            ),
            GatewayOutcome.BILLING_QUOTA,
        ),
        (OpenAITransportError(status_code=422), GatewayOutcome.INVALID_REQUEST),
        (
            OpenAITransportError(status_code=429, error_code="slow_down"),
            GatewayOutcome.RATE_LIMIT,
        ),
        (OpenAITransportError(), GatewayOutcome.UNKNOWN_ERROR),
    ],
)
def test_additional_documented_error_shapes(tmp_path, error, expected):
    instance, transport, _store = gateway(
        tmp_path, [error] * (3 if expected is GatewayOutcome.RATE_LIMIT else 1)
    )

    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )

    assert result.outcome is expected
    assert len(transport.calls) == (3 if expected is GatewayOutcome.RATE_LIMIT else 1)


@pytest.mark.parametrize(
    ("provider_response", "expected"),
    [
        (
            ProviderResponse(
                "resp",
                "completed",
                (ProviderOutput("reasoning", ()),),
                None,
            ),
            GatewayOutcome.MISSING_OUTPUT,
        ),
        (
            response(
                output=(
                    ProviderOutput("message", (ProviderContent("output_text", "{}"),)),
                    ProviderOutput("message", (ProviderContent("output_text", "{}"),)),
                )
            ),
            GatewayOutcome.MISSING_OUTPUT,
        ),
        (
            response(
                output=(
                    ProviderOutput(
                        "message",
                        (
                            ProviderContent("output_text", "{}"),
                            ProviderContent("output_text", "{}"),
                        ),
                    ),
                )
            ),
            GatewayOutcome.UNSUPPORTED_OUTPUT,
        ),
        (
            response(
                output=(
                    ProviderOutput("message", (ProviderContent("output_text", None),)),
                )
            ),
            GatewayOutcome.MISSING_OUTPUT,
        ),
        (response(text="bad\ud800"), GatewayOutcome.INVALID_RESPONSE),
        (
            response(
                text=('{"proposal":{"decision":"reply","content":"ok","brief":"also"}}')
            ),
            GatewayOutcome.INVALID_PROPOSAL,
        ),
        (
            response(text=('{"proposal":{"decision":"reply","content":null}}')),
            GatewayOutcome.INVALID_PROPOSAL,
        ),
    ],
)
def test_additional_response_shapes_are_classified(provider_response, expected):
    parsed = gateway_module._parse_response(provider_response)
    assert parsed.outcome is expected


def test_parser_rejects_non_transport_response():
    assert gateway_module._parse_response(object()).outcome is (
        GatewayOutcome.INVALID_RESPONSE
    )


@pytest.mark.parametrize(
    "text",
    [
        ('{"proposal":{"decision":"no_action","reason_category":"low_value"}}'),
        ('{"proposal":{"decision":"research","brief":"Проверить факт"}}'),
        ('{"proposal":{"decision":"propose_publication","draft":"Черновик"}}'),
    ],
)
def test_every_supported_proposal_shape_is_locally_validated(text):
    parsed = gateway_module._parse_response(response(text=text))
    assert parsed.outcome is GatewayOutcome.COMPLETED
    assert parsed.validation.valid is True


@pytest.mark.parametrize(
    ("proposal", "reason"),
    [
        ({"decision": "reply", "content": None}, "invalid_field"),
        ({"decision": "no_action", "reason_category": None}, "invalid_field"),
        ({"decision": "reply", "content": "ok", "draft": "also"}, "conflicting_fields"),
        ({"decision": "reply", "content": 7}, "invalid_field"),
        ({"decision": "reply", "content": "ok", "admin": True}, "unknown_fields"),
        ({"decision": "reply", "content": "я" * 2049}, "oversized_field"),
        ({"decision": "shell", "content": "ok"}, "unknown_decision"),
        ({"decision": "remember"}, "unknown_decision"),
        ({"decision": "remember", "content": "ok"}, "unknown_fields"),
        ({"decision": []}, "invalid_decision"),
        ([], "invalid_container"),
    ],
)
def test_invalid_generic_proposal_reason_is_persisted_without_retry(
    tmp_path, proposal, reason
):
    instance, transport, store = gateway(
        tmp_path,
        [response(text=json.dumps({"proposal": proposal}, ensure_ascii=False))],
    )
    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )
    attempt = store.usage_attempt("operation-a:1")
    assert result.outcome is GatewayOutcome.INVALID_PROPOSAL
    assert attempt.rejection_reason == reason
    assert attempt.provider_error_code is None
    assert attempt.retry_count == 0
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("text", "reason"),
    [("{", "invalid_json"), ('{"unexpected":"reply"}', "invalid_envelope")],
)
def test_malformed_response_diagnostic_stays_worker_side(tmp_path, text, reason):
    instance, transport, store = gateway(tmp_path, [response(text=text)])
    result = instance.submit(
        request(), correlation_id="correlation-a", operation_id="operation-a"
    )
    assert result.outcome is GatewayOutcome.INVALID_RESPONSE
    assert result.validation is None
    assert store.usage_attempt("operation-a:1").rejection_reason == reason
    assert store.usage_attempt("operation-a:1").provider_error_code is None
    assert len(transport.calls) == 1
