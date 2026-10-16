"""Connected, credential-free public research worker regressions."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import multiprocessing
import os
from pathlib import Path
import socket
import time

import pytest

from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.research.brief import ResearchScreeningContext
from app.trusted.openai_worker_protocol import receive_frame, send_frame
from app.trusted.openai_worker_server import WorkerServerConfig
from app.trusted.public_research import (
    PublicResearchBoundary,
    PublicResearchRequest,
    PublicResearchServer,
    decode_public_request,
    decode_public_response,
    encode_public_request,
    _build_request,
    _response,
)
from app.trusted.public_research_transport import PublicResearchTransport, _error_code
from app.trusted.openai_transport import OpenAITransportError
from app.research.provider import HostedSearchTransportError
from tests.test_research_service import success_response


NOW = datetime(2026, 10, 16, tzinfo=UTC)
QUESTION = "How do deep ocean currents form?"


def raw_response():
    response = success_response()
    return {
        "id": response.response_id,
        "status": response.status,
        "output": list(response.output),
        "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    }


class SDK:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def boundary(tmp_path, outcomes=None, **kwargs):
    sdk = SDK([raw_response()] if outcomes is None else outcomes)
    store = ProviderStateStore(EventStore(tmp_path / "public.sqlite3"))
    worker = PublicResearchBoundary(
        store=store,
        transport=PublicResearchTransport(sdk),
        utcnow=lambda: NOW,
        monotonic=lambda: 0.0,
        **kwargs,
    )
    return worker, sdk, store


def grant(
    worker,
    *,
    question=QUESTION,
    operation_id="int20-ocean",
    peer_uid=None,
    expires_at=None,
):
    return worker.install_synthetic_authorization(
        operation_id=operation_id,
        question=question,
        peer_uid=os.geteuid() if peer_uid is None else peer_uid,
        expires_at=NOW + timedelta(minutes=5) if expires_at is None else expires_at,
    )


def test_distinct_topic_usage_and_consumption_survive_restart(tmp_path):
    worker, sdk, store = boundary(tmp_path, enabled=True)
    request = grant(worker)
    result = worker.execute(request, peer_uid=os.geteuid())
    assert result["outcome"] == "completed"
    assert result["usage"] == {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
    }
    assert result["report"]["semantic_support"] == "not_evaluated"
    assert result["report"]["sources"][0]["status"] == "recorded"
    assert json.loads(sdk.calls[0]["input"])["question"] == QUESTION
    assert sdk.calls[0]["store"] is False
    assert sdk.calls[0]["max_tool_calls"] == 1
    assert store.attempt_exists("int20-ocean")
    recorded = store.usage_attempt("int20-ocean")
    assert (recorded.outcome, recorded.provider_response_id) == (
        "completed",
        "resp_synthetic",
    )
    assert (recorded.input_tokens, recorded.output_tokens, recorded.total_tokens) == (
        100,
        50,
        150,
    )
    restarted, again, _ = boundary(tmp_path, enabled=True)
    assert (
        restarted.execute(grant(restarted), peer_uid=os.geteuid())["outcome"]
        == "duplicate_operation"
    )
    assert again.calls == []


def test_unauthorized_and_disabled_do_not_contact_provider(tmp_path):
    worker, sdk, store = boundary(tmp_path)
    request = grant(worker)
    assert worker.execute(request, peer_uid=os.geteuid())["outcome"] == "disabled"
    assert (
        worker.execute(
            replace(request, question="Different public question"),
            peer_uid=os.geteuid(),
        )["outcome"]
        == "authorization_denied"
    )
    assert (
        worker.execute(replace(request, authorization="0" * 64), peer_uid=os.geteuid())[
            "outcome"
        ]
        == "authorization_denied"
    )
    assert (
        worker.execute(request, peer_uid=os.geteuid() + 1)["outcome"]
        == "authorization_denied"
    )
    assert (
        worker.execute(replace(request, operation_id="missing"), peer_uid=os.geteuid())[
            "outcome"
        ]
        == "authorization_denied"
    )
    assert sdk.calls == []
    assert not store.attempt_exists("int20-ocean")


@pytest.mark.parametrize(
    "case", ["expired", "revoked", "stop", "spend", "config", "screen"]
)
def test_authority_and_controls_fail_closed(tmp_path, monkeypatch, case):
    stopped = [False]
    screening = ResearchScreeningContext(private_canaries=("secret-canary",))
    worker, sdk, store = boundary(
        tmp_path, enabled=True, screening=screening, emergency_stop=lambda: stopped[0]
    )
    if case == "screen":
        with pytest.raises(ValueError):
            grant(worker, question="What is secret-canary?")
        assert sdk.calls == []
        return
    request = grant(worker)
    if case == "expired":
        worker._utcnow = lambda: NOW + timedelta(minutes=6)
    elif case == "revoked":
        worker.revoke(request.operation_id)
    elif case == "stop":
        stopped[0] = True
    elif case == "spend":
        monkeypatch.setattr(store, "is_spend_suspended", lambda: True)
    else:
        worker._config = replace(worker._config, version="changed")
    expected = {
        "expired": "authorization_denied",
        "revoked": "authorization_denied",
        "stop": "emergency_stop",
        "spend": "spend_suspended",
        "config": "authorization_denied",
    }[case]
    assert worker.execute(request, peer_uid=os.geteuid())["outcome"] == expected
    assert sdk.calls == []


def test_provider_errors_and_invalid_results_are_consumed(tmp_path):
    class ServerError(Exception):
        status_code = 503

    for index, outcome in enumerate(
        (ServerError("private"), {**raw_response(), "output": []})
    ):
        directory = tmp_path / str(index)
        directory.mkdir()
        worker, sdk, store = boundary(directory, [outcome], enabled=True)
        request = grant(worker)
        assert worker.execute(request, peer_uid=os.geteuid())["outcome"] in {
            "server_error",
            "invalid_response",
        }
        assert store.attempt_exists(request.operation_id)
        recorded = store.usage_attempt(request.operation_id)
        assert recorded.outcome in {
            "server_error",
            "invalid_response",
        }
        if index == 1:
            assert recorded.provider_response_id == "resp_synthetic"
            assert recorded.total_tokens == 150
        assert (
            worker.execute(request, peer_uid=os.geteuid())["outcome"]
            == "duplicate_operation"
        )
        assert len(sdk.calls) == 1


def test_request_and_response_framing_is_closed():
    request = PublicResearchRequest("op", QUESTION, "a" * 64)
    assert decode_public_request(encode_public_request(request)) == request
    for payload in (b"{}", b"\xff", b'{"version":1,"version":2}', b"x" * 300000):
        with pytest.raises(ValueError):
            decode_public_request(payload)
    with pytest.raises(ValueError):
        encode_public_request(replace(request, question=""))
    with pytest.raises(ValueError):
        encode_public_request(replace(request, authorization="bad"))
    with pytest.raises(TypeError):
        encode_public_request(object())
    with pytest.raises(ValueError):
        decode_public_response(b"{}")
    wrong = json.loads(encode_public_request(request))
    wrong["operation"] = "infer"
    with pytest.raises(ValueError):
        decode_public_request(json.dumps(wrong).encode())


@pytest.mark.parametrize(
    "change",
    [
        {"version": "wrong"},
        {"outcome": []},
        {"outcome": "invented"},
        {"attempts": True},
        {"attempts": 2},
        {"message": "unsafe"},
        {"report": {}},
        {"usage": {}},
        {"usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": True}},
        {"usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 4}},
    ],
)
def test_response_envelope_rejects_untrusted_authority(change):
    response = _response("authorization_denied")
    response.update(change)
    with pytest.raises(ValueError):
        decode_public_response(json.dumps(response).encode())


@pytest.mark.parametrize(
    "change",
    [
        {"message": "wrong"},
        {"main_finding": ""},
        {"main_finding": None},
        {"main_finding": "я" * 9000},
        {"uncertainty": "verified"},
        {"claim_status": "verified"},
        {"semantic_support": "supported"},
        {"sources": []},
        {"sources": None},
    ],
)
def test_completed_response_cannot_claim_verification(change):
    worker_result = _response(
        "completed",
        1,
        {
            "main_finding": "Синтетический вывод.",
            "uncertainty": "Ссылки проверены только структурно; фактическая достоверность не оценена.",
            "claim_status": "unverified",
            "semantic_support": "not_evaluated",
            "sources": [
                {
                    "reference": "r",
                    "url": "https://example.org",
                    "status": "recorded",
                    "metadata_validated": True,
                }
            ],
        },
    )
    if "message" in change:
        worker_result.update(change)
    else:
        worker_result["report"].update(change)
    with pytest.raises(ValueError):
        decode_public_response(json.dumps(worker_result, ensure_ascii=False).encode())


@pytest.mark.parametrize(
    "change",
    [
        {"reference": 1},
        {"reference": "x" * 1025},
        {"url": 1},
        {"url": "x" * 4097},
        {"status": "verified"},
        {"metadata_validated": False},
        {"metadata_validated": "true"},
    ],
)
def test_response_source_metadata_is_bounded(change):
    result = _response(
        "completed",
        1,
        {
            "main_finding": "Вывод.",
            "uncertainty": "Ссылки проверены только структурно; фактическая достоверность не оценена.",
            "claim_status": "unverified",
            "semantic_support": "not_evaluated",
            "sources": [
                {
                    "reference": "r",
                    "url": None,
                    "status": "recorded",
                    "metadata_validated": True,
                }
            ],
        },
    )
    result["report"]["sources"][0].update(change)
    with pytest.raises(ValueError):
        decode_public_response(json.dumps(result).encode())


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", 1),
        ("operation_id", "invalid space"),
        ("question", 1),
        ("question", "x" * 2049),
        ("question", "Please inspect https://example.org"),
        ("question", "Please inspect www.example.org"),
        ("authorization", 1),
    ],
)
def test_request_rejects_nonpublic_shapes(field, value):
    request = PublicResearchRequest("op", QUESTION, "a" * 64)
    with pytest.raises(ValueError):
        encode_public_request(replace(request, **{field: value}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", "bad id"),
        ("peer_uid", -1),
        ("expires_at", NOW),
        ("expires_at", datetime(2026, 10, 17)),
        ("expires_at", NOW + timedelta(minutes=11)),
    ],
)
def test_trusted_installation_rejects_invalid_binding(tmp_path, field, value):
    worker, _, _ = boundary(tmp_path)
    arguments = {
        "operation_id": "op",
        "question": QUESTION,
        "peer_uid": os.geteuid(),
        "expires_at": NOW + timedelta(minutes=5),
    }
    arguments[field] = value
    with pytest.raises(ValueError):
        worker.install_synthetic_authorization(**arguments)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/private",
        "example.org/private",
        "//example.org/private",
        "example.org?key=private",
        "EXAMPLE．ORG/private",
        "example%2Eorg%2Fprivate",
        "example%2525252Eorg%2525252Fprivate",
        "exam\u200bple.org/private",
        "example&#46;org&#47;private",
        "example&amp;amp;#46;org",
        "127.0.0.1:8080?token=secret",
        "127.0.0.1",
        "[::1]:8080?token=secret",
        "2001:db8::1",
    ],
)
def test_installation_does_not_admit_arbitrary_url(tmp_path, url):
    worker, _, _ = boundary(tmp_path)
    with pytest.raises(ValueError):
        grant(worker, question="Read " + url)


def test_clock_before_grant_validity_denies(tmp_path):
    worker, sdk, _ = boundary(tmp_path, enabled=True)
    request = grant(worker)
    worker._utcnow = lambda: NOW - timedelta(seconds=1)
    assert (
        worker.execute(request, peer_uid=os.geteuid())["outcome"]
        == "authorization_denied"
    )
    assert sdk.calls == []


def test_bad_trusted_dependencies_and_server_type(tmp_path):
    worker, _, store = boundary(tmp_path)
    with pytest.raises(TypeError):
        PublicResearchBoundary(store=None, transport=worker._transport)
    with pytest.raises(TypeError):
        PublicResearchBoundary(store=store, transport=None)
    with pytest.raises(TypeError):
        PublicResearchBoundary(store=store, transport=worker._transport, enabled=1)
    with pytest.raises(TypeError):
        PublicResearchBoundary(store=store, transport=worker._transport, screening=None)
    with pytest.raises(TypeError):
        PublicResearchServer(
            WorkerServerConfig(tmp_path / "s", frozenset({os.geteuid()})), None
        )


@pytest.mark.parametrize("race", ["stop", "spend", "revoke", "expiry", "config"])
def test_recheck_after_durable_reservation(tmp_path, monkeypatch, race):
    stopped = [False]
    worker, sdk, store = boundary(
        tmp_path, enabled=True, emergency_stop=lambda: stopped[0]
    )
    request = grant(worker)
    reserve = store.reserve_attempt

    def changing_reserve(*args):
        result = reserve(*args)
        if race == "stop":
            stopped[0] = True
        elif race == "spend":
            monkeypatch.setattr(store, "is_spend_suspended", lambda: True)
        elif race == "revoke":
            worker._grants[request.operation_id].revoked = True
        elif race == "expiry":
            worker._utcnow = lambda: NOW + timedelta(minutes=6)
        else:
            worker._config = replace(worker._config, model="gpt-6.1-sol")
        return result

    monkeypatch.setattr(store, "reserve_attempt", changing_reserve)
    assert worker.execute(request, peer_uid=os.geteuid())["outcome"] in {
        "emergency_stop",
        "spend_suspended",
        "authorization_denied",
    }
    assert store.attempt_exists(request.operation_id)
    assert sdk.calls == []


def test_stop_after_request_build_prevents_contact(tmp_path, monkeypatch):
    import app.trusted.public_research as module

    stopped = [False]
    worker, sdk, store = boundary(
        tmp_path, enabled=True, emergency_stop=lambda: stopped[0]
    )
    request = grant(worker)
    build = module._build_request

    def changing_build(*args):
        result = build(*args)
        stopped[0] = True
        return result

    monkeypatch.setattr(module, "_build_request", changing_build)
    assert worker.execute(request, peer_uid=os.geteuid())["outcome"] == "emergency_stop"
    assert store.attempt_exists(request.operation_id)
    assert sdk.calls == []


def test_persistence_failure_latches_and_sanitizes(tmp_path, monkeypatch):
    from app.core.provider_spend import ProviderPersistenceError

    worker, sdk, store = boundary(tmp_path, enabled=True)
    request = grant(worker)

    def failed(*args):
        raise ProviderPersistenceError("private")

    monkeypatch.setattr(store, "reserve_attempt", failed)
    first = worker.execute(request, peer_uid=os.geteuid())
    assert first["outcome"] == "persistence_failure" and first["attempts"] == 0
    monkeypatch.undo()
    assert (
        worker.execute(request, peer_uid=os.geteuid())["outcome"]
        == "persistence_failure"
    )
    assert sdk.calls == []


def test_concurrent_duplicate_has_one_provider_contact(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    worker, sdk, store = boundary(tmp_path, enabled=True)
    request = grant(worker)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda _: worker.execute(request, peer_uid=os.geteuid())["outcome"],
                range(2),
            )
        )
    assert sorted(outcomes) == ["completed", "duplicate_operation"]
    assert len(sdk.calls) == 1
    assert store.attempt_exists(request.operation_id)


def test_timeout_at_dispatch_and_after_provider(tmp_path):
    worker, sdk, store = boundary(tmp_path, enabled=True)
    request = grant(worker)
    worker._monotonic = iter((0, 31, 31)).__next__
    assert worker.execute(request, peer_uid=os.geteuid())["outcome"] == "timeout"
    assert sdk.calls == [] and store.attempt_exists(request.operation_id)
    assert store.usage_attempt(request.operation_id).outcome == "timeout"

    other = tmp_path / "other"
    other.mkdir()
    worker, sdk, store = boundary(other, enabled=True)
    request = grant(worker)
    worker._monotonic = iter((0, 0, 71, 71)).__next__
    assert worker.execute(request, peer_uid=os.geteuid())["outcome"] == "timeout"
    assert len(sdk.calls) == 1
    assert store.usage_attempt(request.operation_id).total_tokens == 150


def test_result_gate_rejects_private_canary_and_unsupported_url(tmp_path):
    canary = "private-canary"
    for index, alteration in enumerate(("canary", "id", "url", "english")):
        raw = raw_response()
        if alteration == "canary":
            raw["output"][1]["content"][0]["text"] += " " + canary
        elif alteration == "id":
            raw["id"] = "resp_" + canary
        elif alteration == "url":
            raw["output"][1]["content"][0]["annotations"][0]["url"] = (
                "http://unsafe.example"
            )
        else:
            raw["output"][1]["content"][0]["text"] = "English only answer"
        folder = tmp_path / str(index)
        folder.mkdir()
        worker, sdk, store = boundary(
            folder,
            [raw],
            enabled=True,
            screening=ResearchScreeningContext(private_canaries=(canary,)),
        )
        result = worker.execute(grant(worker), peer_uid=os.geteuid())
        assert result["outcome"] in {"invalid_response", "output_gate_rejected"}
        assert canary not in json.dumps(result)
        assert len(sdk.calls) == 1
        recorded = store.usage_attempt("int20-ocean")
        assert recorded.outcome == result["outcome"]
        assert recorded.provider_response_id == (
            None if alteration == "id" else "resp_synthetic"
        )
        assert canary not in repr(recorded)


def test_provider_transport_is_bounded_and_sanitized(tmp_path, monkeypatch):
    worker, sdk, _ = boundary(tmp_path, enabled=True)
    request = _build_request(
        worker.install_synthetic_authorization(
            operation_id="op",
            question=QUESTION,
            peer_uid=os.geteuid(),
            expires_at=NOW + timedelta(minutes=5),
        )
        and worker._grants["op"].brief,
        worker._config,
    )
    transport = worker._transport
    with pytest.raises(TypeError):
        PublicResearchTransport(None)
    with pytest.raises(TypeError):
        transport.call(None, timeout_seconds=1)
    for timeout in (True, 0, -1, float("nan"), 121):
        with pytest.raises(ValueError):
            transport.call(request, timeout_seconds=timeout)

    class Dump:
        def model_dump(self, *, mode):
            assert mode == "json"
            return raw_response()

    sdk.outcomes = [Dump()]
    assert transport.call(request, timeout_seconds=1).status == "completed"
    for malformed, expected_usage in (
        (None, None),
        ({**raw_response(), "usage": {}}, None),
        (
            {
                **raw_response(),
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 149,
                },
            },
            None,
        ),
        ({**raw_response(), "output": {}}, 150),
        ({**raw_response(), "output": "x" * 600000}, 150),
        ({**raw_response(), "output": object()}, 150),
    ):
        sdk.outcomes = [malformed]
        with pytest.raises(HostedSearchTransportError):
            transport.call(request, timeout_seconds=1)
        assert (
            None
            if transport.last_usage is None
            else transport.last_usage["total_tokens"]
        ) == expected_usage
    sdk.outcomes = [{**raw_response(), "usage": None}]
    transport.call(request, timeout_seconds=1)
    assert transport.last_usage is None
    monkeypatch.setattr(
        "app.trusted.public_research_transport.build_worker_openai_transport",
        lambda **kwargs: type("T", (), {"_client": sdk})(),
    )
    assert (
        PublicResearchTransport.from_protected_file(
            project_id="proj_test", organization_id="org-test"
        )._client
        is sdk
    )


def test_error_mapping_and_provider_refusal(tmp_path):
    assert _error_code(OpenAITransportError(timeout=True)) == "timeout"
    assert _error_code(OpenAITransportError(connection=True)) == "connection_error"
    assert _error_code(OpenAITransportError(status_code=403)) == "permission_error"
    assert _error_code(OpenAITransportError(status_code=429)) == "ambiguous_quota"
    assert (
        _error_code(
            OpenAITransportError(status_code=429, error_type="insufficient_quota")
        )
        == "billing_quota"
    )
    assert (
        _error_code(
            OpenAITransportError(status_code=429, error_type="rate_limit_error")
        )
        == "rate_limit"
    )
    assert (
        _error_code(OpenAITransportError(error_code="credit_balance_exhausted"))
        == "billing_quota"
    )
    assert (
        _error_code(OpenAITransportError(error_code="project_spend_limit_exceeded"))
        == "project_spend_limit_exceeded"
    )
    assert _error_code(OpenAITransportError()) == "provider_error"
    worker, sdk, store = boundary(
        tmp_path, [{**raw_response(), "status": "incomplete"}], enabled=True
    )
    assert (
        worker.execute(grant(worker), peer_uid=os.geteuid())["outcome"]
        == "invalid_response"
    )
    assert len(sdk.calls) == 1
    assert store.usage_attempt("int20-ocean").provider_response_id == "resp_synthetic"


@pytest.mark.parametrize(
    "response_id,expected_id",
    [
        ("resp_synthetic", "resp_synthetic"),
        ("resp_private-canary", None),
    ],
)
def test_malformed_output_keeps_safe_usage_and_id(tmp_path, response_id, expected_id):
    raw = {**raw_response(), "id": response_id, "output": {}}
    worker, sdk, store = boundary(
        tmp_path,
        [raw],
        enabled=True,
        screening=ResearchScreeningContext(private_canaries=("private-canary",)),
    )
    request = grant(worker)
    result = worker.execute(request, peer_uid=os.geteuid())
    assert result["outcome"] == "invalid_response"
    assert result["usage"]["total_tokens"] == 150
    recorded = store.usage_attempt(request.operation_id)
    assert recorded.total_tokens == 150
    assert recorded.provider_response_id == expected_id
    assert len(sdk.calls) == 1


@pytest.mark.parametrize(
    "error_body,expected",
    [
        ({"type": "rate_limit_error"}, "rate_limit"),
        ({"type": "insufficient_quota"}, "billing_quota"),
        ({"code": "credit_balance_exhausted"}, "billing_quota"),
        ({}, "ambiguous_quota"),
    ],
)
def test_provider_quota_outcomes_are_terminal_and_recorded(
    tmp_path, error_body, expected
):
    class Quota(Exception):
        status_code = 429
        body = {"error": error_body}

    worker, sdk, store = boundary(tmp_path, [Quota("private")], enabled=True)
    request = grant(worker)
    assert worker.execute(request, peer_uid=os.geteuid())["outcome"] == expected
    assert store.usage_attempt(request.operation_id).outcome == expected
    assert len(sdk.calls) == 1


def test_confirmed_spend_limit_suspends_future_research(tmp_path):
    class Limit(Exception):
        status_code = 429
        body = {"error": {"code": "project_spend_limit_exceeded"}}

    worker, sdk, store = boundary(tmp_path, [Limit("private")], enabled=True)
    request = grant(worker)
    assert (
        worker.execute(request, peer_uid=os.geteuid())["outcome"]
        == "spend_limit_confirmed"
    )
    assert store.is_spend_suspended()
    assert len(sdk.calls) == 1
    assert (
        worker.execute(grant(worker, operation_id="next"), peer_uid=os.geteuid())[
            "outcome"
        ]
        == "spend_suspended"
    )


def test_unrecorded_spend_limit_fails_closed(tmp_path, monkeypatch):
    class Limit(Exception):
        status_code = 429
        body = {"error": {"code": "project_spend_limit_exceeded"}}

    worker, sdk, store = boundary(tmp_path, [Limit("private")], enabled=True)
    request = grant(worker)
    monkeypatch.setattr(store, "record_attempt", lambda *args, **kwargs: False)
    first = worker.execute(request, peer_uid=os.geteuid())
    assert first["outcome"] == "persistence_failure" and first["attempts"] == 1
    assert (
        worker.execute(request, peer_uid=os.geteuid())["outcome"]
        == "persistence_failure"
    )
    assert len(sdk.calls) == 1


def test_unrecorded_success_fails_closed(tmp_path, monkeypatch):
    worker, sdk, store = boundary(tmp_path, enabled=True)
    request = grant(worker)
    monkeypatch.setattr(store, "record_attempt", lambda *args, **kwargs: False)
    first = worker.execute(request, peer_uid=os.geteuid())
    assert first["outcome"] == "persistence_failure" and first["attempts"] == 1
    assert first["usage"]["total_tokens"] == 150
    assert (
        worker.execute(request, peer_uid=os.geteuid())["outcome"]
        == "persistence_failure"
    )
    assert len(sdk.calls) == 1


def test_invalid_pre_admission_state_and_duration_fail_closed(tmp_path):
    worker, sdk, store = boundary(tmp_path, enabled=True)
    request = grant(worker)
    worker._stop = lambda: (_ for _ in ()).throw(ValueError("private"))
    assert (
        worker.execute(request, peer_uid=os.geteuid())["outcome"] == "invalid_response"
    )
    assert not store.attempt_exists(request.operation_id)
    assert sdk.calls == []

    worker._stop = lambda: False
    worker._monotonic = iter((5, 5, 4, 3)).__next__
    result = worker.execute(request, peer_uid=os.geteuid())
    assert result["outcome"] == "persistence_failure" and result["attempts"] == 1
    assert store.attempt_exists(request.operation_id)


def test_post_contact_store_failure_reports_one_attempt(tmp_path, monkeypatch):
    from app.core.provider_spend import ProviderPersistenceError

    worker, sdk, store = boundary(tmp_path, enabled=True)
    request = grant(worker)
    contacted = [False]
    create = sdk.create

    def contact(**kwargs):
        response = create(**kwargs)
        contacted[0] = True
        return response

    def spend_state():
        if contacted[0]:
            raise ProviderPersistenceError("private")
        return False

    monkeypatch.setattr(sdk, "create", contact)
    monkeypatch.setattr(store, "is_spend_suspended", spend_state)
    result = worker.execute(request, peer_uid=os.geteuid())
    assert result["outcome"] == "persistence_failure"
    assert result["attempts"] == 1
    assert result["usage"]["total_tokens"] == 150
    assert len(sdk.calls) == 1


def test_provider_dispatch_exception_and_post_response_stop(tmp_path, monkeypatch):
    worker, sdk, store = boundary(tmp_path, enabled=True)
    request = grant(worker)
    monkeypatch.setattr(
        worker._transport,
        "call",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("private")),
    )
    assert worker.execute(request, peer_uid=os.geteuid())["outcome"] == "provider_error"
    assert store.attempt_exists(request.operation_id)
    assert sdk.calls == []

    folder = tmp_path / "stopped"
    folder.mkdir()
    stopped = [False]
    worker, sdk, _ = boundary(folder, enabled=True, emergency_stop=lambda: stopped[0])
    request = grant(worker)
    create = sdk.create

    def change_after_contact(**kwargs):
        result = create(**kwargs)
        stopped[0] = True
        return result

    monkeypatch.setattr(sdk, "create", change_after_contact)
    assert worker.execute(request, peer_uid=os.geteuid())["outcome"] == "emergency_stop"
    assert len(sdk.calls) == 1


def test_provider_request_size_bound(tmp_path, monkeypatch):
    import app.trusted.public_research as module

    worker, _, _ = boundary(tmp_path)
    grant(worker)
    monkeypatch.setattr(module, "_encode_json", lambda value: b"x" * 20000)
    with pytest.raises(ValueError):
        _build_request(worker._grants["int20-ocean"].brief, worker._config)


@pytest.mark.parametrize(
    "scenario",
    ["bad_peer", "forbidden_peer", "bad_request", "short_deadline", "send_error"],
)
def test_socket_handler_fail_closed(tmp_path, monkeypatch, scenario):
    import app.trusted.public_research as module

    worker, sdk, _ = boundary(tmp_path, enabled=True)
    request = grant(worker)
    config = WorkerServerConfig(
        tmp_path / "unused.sock",
        frozenset({os.geteuid() + 1 if scenario == "forbidden_peer" else os.geteuid()}),
    )
    server = PublicResearchServer(config, worker)
    child, parent = socket.socketpair(socket.AF_UNIX)
    with parent:
        if scenario == "bad_peer":
            monkeypatch.setattr(
                module,
                "_peer_uid",
                lambda value: (_ for _ in ()).throw(module.WorkerProtocolError("bad")),
            )
        else:
            monkeypatch.setattr(module, "_peer_uid", lambda value: os.geteuid())
        if scenario in {
            "bad_request",
            "short_deadline",
            "send_error",
            "forbidden_peer",
        }:
            monkeypatch.setattr(
                module,
                "receive_frame",
                lambda *args, **kwargs: b"{}"
                if scenario == "bad_request"
                else encode_public_request(request),
            )
        if scenario == "short_deadline":
            from types import SimpleNamespace

            started = time.monotonic()
            values = iter((started, started + 300))
            monkeypatch.setattr(
                module, "time", SimpleNamespace(monotonic=lambda: next(values))
            )
        if scenario == "send_error":
            monkeypatch.setattr(
                module,
                "send_frame",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    module.WorkerProtocolError("failed")
                ),
            )
        server._handle_connection(child)
        if scenario in {"bad_peer", "forbidden_peer", "send_error"}:
            assert parent.recv(1) == b""
        else:
            result = decode_public_response(
                receive_frame(parent, deadline=time.monotonic() + 10, io_timeout=1)
            )
            assert result["outcome"] == (
                "timeout" if scenario == "short_deadline" else "invalid_ipc_request"
            )
    assert len(sdk.calls) == (1 if scenario == "send_error" else 0)


def _spawned_worker(directory, channel):
    import pytest

    original_socket = socket.socket

    def only_local(family=socket.AF_INET, *args, **kwargs):
        assert family == socket.AF_UNIX
        return original_socket(family, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("network or credential access")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket, "socket", only_local)
        patch.setattr(socket, "getaddrinfo", forbidden)
        patch.setattr(
            "app.trusted.public_research_transport.build_worker_openai_transport",
            forbidden,
        )
        worker, sdk, _store = boundary(Path(directory), enabled=True)
        request = grant(worker)
        server = PublicResearchServer(
            WorkerServerConfig(
                Path(directory) / "public.sock", frozenset({os.geteuid()})
            ),
            worker,
        )
        try:
            server.start()
            channel.send(encode_public_request(request))
            server.serve(max_connections=2)
            channel.send(sdk.calls)
        finally:
            server.close()
            channel.close()


def test_spawned_real_worker_ipc_and_lost_response(tmp_path):
    tmp_path.chmod(0o700)
    parent, child = multiprocessing.get_context("spawn").Pipe(duplex=False)
    process = multiprocessing.get_context("spawn").Process(
        target=_spawned_worker, args=(str(tmp_path), child)
    )
    process.start()
    child.close()
    try:
        assert parent.poll(15)
        payload = parent.recv()
        for index in range(2):
            with socket.socket(socket.AF_UNIX) as connection:
                connection.connect(str(tmp_path / "public.sock"))
                deadline = time.monotonic() + 15
                send_frame(connection, payload, deadline=deadline, io_timeout=1)
                connection.shutdown(socket.SHUT_WR)
                response = decode_public_response(
                    receive_frame(connection, deadline=deadline, io_timeout=1)
                )
            assert response["outcome"] == (
                "completed" if index == 0 else "duplicate_operation"
            )
        assert parent.poll(15)
        calls = parent.recv()
        assert len(calls) == 1
        assert QUESTION in calls[0]["input"]
        process.join(15)
        assert process.exitcode == 0
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(5)


def _spawned_rejection_worker(directory, scenario, channel):
    worker, sdk, _ = boundary(Path(directory), enabled=True)
    request = grant(worker)
    allowed = os.geteuid() + 1 if scenario == "peer" else os.geteuid()
    server = PublicResearchServer(
        WorkerServerConfig(Path(directory) / "reject.sock", frozenset({allowed})),
        worker,
    )
    try:
        server.start()
        channel.send(encode_public_request(request))
        server.serve(max_connections=1)
        channel.send(len(sdk.calls))
    finally:
        server.close()
        channel.close()


@pytest.mark.parametrize("scenario", ["peer", "frame"])
def test_spawned_worker_rejects_peer_or_malformed_frame(tmp_path, scenario):
    tmp_path.chmod(0o700)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_spawned_rejection_worker, args=(str(tmp_path), scenario, child)
    )
    process.start()
    child.close()
    try:
        assert parent.poll(15)
        payload = parent.recv()
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(tmp_path / "reject.sock"))
            deadline = time.monotonic() + 15
            send_frame(
                connection,
                payload if scenario == "peer" else b"{}",
                deadline=deadline,
                io_timeout=1,
            )
            connection.shutdown(socket.SHUT_WR)
            if scenario == "peer":
                try:
                    assert connection.recv(1) == b""
                except ConnectionResetError:
                    pass
            else:
                result = decode_public_response(
                    receive_frame(connection, deadline=deadline, io_timeout=1)
                )
                assert result["outcome"] == "invalid_ipc_request"
        assert parent.poll(15) and parent.recv() == 0
        process.join(15)
        assert process.exitcode == 0
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(5)


def _exit_at_boundary(directory, mode, channel):
    worker, sdk, store = boundary(Path(directory), enabled=True)
    request = grant(worker)
    if mode == "admission":
        reserve = store.reserve_attempt

        def admitted(*args):
            status = reserve(*args)
            channel.send("admitted")
            os._exit(41)
            return status

        store.reserve_attempt = admitted
    elif mode == "dispatch":

        def dispatched(**kwargs):
            channel.send(kwargs["input"])
            os._exit(42)

        sdk.create = dispatched
    result = worker.execute(request, peer_uid=os.geteuid())
    channel.send(result["outcome"])
    os._exit(43)


@pytest.mark.parametrize(
    "mode,exit_code",
    [
        ("admission", 41),
        ("dispatch", 42),
        ("result", 43),
    ],
)
def test_process_exit_keeps_operation_consumed(tmp_path, mode, exit_code):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_exit_at_boundary, args=(str(tmp_path), mode, child)
    )
    process.start()
    child.close()
    try:
        assert parent.poll(15)
        marker = parent.recv()
        assert marker == "admitted" if mode == "admission" else bool(marker)
        process.join(15)
        assert process.exitcode == exit_code
        restarted, sdk, store = boundary(tmp_path, enabled=True)
        request = grant(restarted)
        assert (
            restarted.execute(request, peer_uid=os.geteuid())["outcome"]
            == "duplicate_operation"
        )
        assert store.attempt_exists(request.operation_id)
        assert sdk.calls == []
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(5)
