"""Offline model suggestion enters the existing reviewed shared lifecycle."""

import json
import os
import sqlite3
from dataclasses import replace
from threading import Thread

import pytest

from app.cognition.worker_shared import (
    SharedProposalItem,
    SharedProposalRequest,
    SharedSuggestion,
    parse_shared_suggestion,
)
from app.cognition.shared import SharedIntelligence
from app.cognition.sharing import screen_abstract_candidate
from app.core.agent_proposals import ProposalValidation, ReplyProposal
from app.core.openai_worker_client import ClientResult, OpenAIWorkerClient
from app.core.provider_spend import ProviderStateStore
from app.core.provider_contract import (
    build_worker_shared_request,
    default_openai_provider_config,
    DeploymentStage,
)
from app.core.store import EventStore
from app.trusted.private_pilot import PilotAllowance, load_activation
from app.trusted.private_pilot_worker import (
    PilotRequestGate,
    PilotStop,
    SHARED_PERMISSION_VERSION,
)
from app.trusted.openai_gateway import (
    TrustedOpenAIGateway,
    GatewayOutcome,
    _parse_response,
)
from app.trusted.openai_transport import (
    FakeOpenAITransport,
    ProviderResponse,
    ProviderOutput,
    ProviderContent,
)
from app.trusted.openai_worker_server import OpenAIWorkerServer, WorkerServerConfig
from app.trusted.openai_worker_protocol import (
    InferenceRequest,
    WorkerResponse,
    WorkerProtocolError,
    encode_request,
    decode_request,
    encode_response,
    decode_response,
)
from app.private_web.runtime import Stage0ConversationRuntime, WorkerConversationRuntime
from app.private_web.shared_audit import SharedProposalAuditStore
from app.privacy.application import PrivacyReadGate, PrivacyReadBlocked
from app.privacy.application import EventStoreErasureAdapter, EventStoreRestoreAdapter
from app.privacy.ledger import PrivacyLedger
from app.privacy.deletion import DeletionCoordinator
from app.privacy.recovery import QuarantinedRestore, create_sqlite_backup
from test_reviewed_shared import consent, candidate as reviewed_candidate
from test_cognition_pipeline import NOW, append_event, principal
from test_shared_intelligence import approval
from test_int22_controls import activation_file, _stop
from test_private_conversation_runtime import response as reply_response
from test_privacy_deletion import (
    context as deletion_context,
    request as deletion_request,
)


def _setup(tmp_path, monkeypatch, suggestion):
    events = EventStore(tmp_path / "events.db")
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, events, clock=lambda: NOW)
    assert gate.reconcile()
    shared = SharedIntelligence(events, gate, clock=lambda: NOW)
    provider = ProviderStateStore(EventStore(tmp_path / "provider.db"))
    client = OpenAIWorkerClient(
        tmp_path / "worker.sock", expected_worker_uid=os.getuid()
    )
    requests = []

    def fake_infer(request, *, operation_id, correlation_id):
        requests.append(request)
        if type(request) is SharedProposalRequest:
            proposed = suggestion(request) if callable(suggestion) else suggestion
            return ClientResult(
                operation_id, correlation_id, "completed", 1, None, shared=proposed
            )
        return ClientResult(
            operation_id,
            correlation_id,
            "completed",
            1,
            None,
            validation=ProposalValidation(ReplyProposal("Да, обсудим."), None),
        )

    monkeypatch.setattr(client, "infer", fake_infer)
    runtime = WorkerConversationRuntime(
        events,
        gate,
        client,
        provider_state=provider,
        shared_intelligence=shared,
        shared_proposals_enabled=True,
        shared_proposal_audience=("participant-b",),
        clock=lambda: NOW,
    )
    return events, ledger, gate, shared, runtime, requests


def test_new_proposal_stays_pending_until_independent_review_and_exact_consent(
    tmp_path, monkeypatch
):
    suggestion = SharedSuggestion(
        "new",
        None,
        "belief_or_hypothesis",
        "Compare independent explanations before adopting a view.",
        0.5,
    )
    events, ledger, gate, shared, runtime, requests = _setup(
        tmp_path, monkeypatch, suggestion
    )
    a, b = principal("a"), principal("b")
    runtime.submit(
        a, "first", "Private example about doubt and competing observations."
    )
    assert len(requests) == 2
    assert shared.context(b) == ()
    with events._open() as db:
        operation_id, source = db.execute(
            "SELECT s.operation_id,s.event_id FROM protected_shared_sources s JOIN shared_operations o USING(operation_id) WHERE o.author_principal='participant-a'"
        ).fetchone()
        assert db.execute(
            "SELECT status FROM shared_operations WHERE operation_id=?", (operation_id,)
        ).fetchone() == ("quarantined",)
    consent(ledger, source)
    assert gate.reconcile()
    from app.cognition.review_cli import _inspect

    _status, candidate, _sources = _inspect(events, gate, operation_id)
    shared.review(
        candidate,
        decision_id="decision-new",
        review_version="local-review-v1",
        approve=True,
    )
    assert shared.context(b)[0].text == suggestion.statement
    assert "Private example" not in repr(shared.context(b)[0].structured())
    fake = FakeOpenAITransport([reply_response()])
    Stage0ConversationRuntime(
        events, gate, fake, shared_intelligence=shared, clock=lambda: NOW
    ).submit(b, "after-review", "Could we compare ideas?")
    worker_input = fake.calls[0].request.input_text
    assert suggestion.statement in worker_input
    assert "Private example" not in worker_input
    assert source not in worker_input


def test_no_change_and_closed_contract(tmp_path, monkeypatch):
    events, _ledger, _gate, shared, runtime, requests = _setup(
        tmp_path, monkeypatch, SharedSuggestion("no_change", None, None, None, None)
    )
    runtime.submit(
        principal("a"), "first", "A private input with no useful general claim."
    )
    assert len(requests) == 2
    assert shared.context(principal("b")) == ()
    with events._open() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_operations").fetchone() == (0,)
    base = {
        "decision": "new",
        "handle": None,
        "kind": "belief_or_hypothesis",
        "statement": "Review alternative explanations.",
        "confidence": 0.5,
    }
    for changed in (
        {**base, "authority": "approve"},
        {**base, "kind": "private_fact"},
        {**base, "handle": "h0123456789abcdef"},
        {**base, "statement": "x" * 4097},
    ):
        with pytest.raises(ValueError, match="invalid shared suggestion"):
            parse_shared_suggestion(json.dumps(changed))


def test_consent_and_review_revocations_erase_model_derived_candidates(
    tmp_path, monkeypatch
):
    events, ledger, gate, shared, runtime, _requests, source, content, reply = (
        _direct_runtime(
            tmp_path,
            monkeypatch,
            SharedSuggestion(
                "new",
                None,
                "belief_or_hypothesis",
                "Compare independent explanations.",
                0.5,
            ),
        )
    )
    runtime._propose_shared(principal("a"), source, content, "corr", reply)
    with events._open() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_payloads").fetchone() == (1,)
    service, context, grant = consent(ledger, source)
    assert gate.reconcile()
    assert service.revoke(
        context,
        consent_id=grant.consent_id,
        operation_id="revoke-shared-model-consent",
        revoked_at=NOW,
        correlation_id="revoke-shared-model-correlation",
    ).created
    assert gate.reconcile()
    with events._open() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_payloads").fetchone() == (0,)
        assert db.execute("SELECT status FROM shared_operations").fetchone() == (
            "invalidated",
        )
    other = tmp_path / "review"
    other.mkdir()
    (
        events2,
        ledger2,
        gate2,
        shared2,
        runtime2,
        _requests2,
        source2,
        content2,
        reply2,
    ) = _direct_runtime(
        other,
        monkeypatch,
        SharedSuggestion(
            "new",
            None,
            "belief_or_hypothesis",
            "Compare independent explanations.",
            0.5,
        ),
    )
    runtime2._propose_shared(principal("a"), source2, content2, "corr", reply2)
    reviewed = reviewed_candidate(source2)
    assert shared2.quarantine(reviewed) == "shared_commit_disabled"
    consent(ledger2, source2)
    assert gate2.reconcile()
    shared2.review(
        reviewed,
        decision_id="approve-older-review",
        review_version="local-review-v1",
        approve=True,
    )
    assert (
        shared2.revoke_review(reviewed.proposal.operation_id, "revoke-older-review")
        == "revoked"
    )
    with events2._open() as db:
        # The proposal predates this review and never consumed its content.
        assert db.execute("SELECT COUNT(*) FROM shared_payloads").fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM shared_operations WHERE status='invalidated'"
        ).fetchone() == (1,)


def test_stop_during_shared_screen_blocks_quarantine_persistence(tmp_path, monkeypatch):
    events, _ledger, _gate, shared, runtime, requests, source, content, reply = (
        _direct_runtime(
            tmp_path,
            monkeypatch,
            SharedSuggestion(
                "new",
                None,
                "belief_or_hypothesis",
                "Compare independent explanations.",
                0.5,
            ),
        )
    )

    def stop_during_screen(candidate, sources):
        runtime._stop = lambda: True
        return screen_abstract_candidate(candidate, sources)

    shared._privacy_screen = stop_during_screen
    runtime._propose_shared(principal("a"), source, content, "corr", reply)
    assert len(requests) == 1
    with events._open() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_operations").fetchone() == (0,)


def test_shared_worker_protocol_rejects_forged_fields_and_cross_operation_results():
    request = SharedProposalRequest(
        "Private initiating input.",
        (
            SharedProposalItem(
                "h0123456789abcdef", "durable_interest", "General curiosity.", 0.4
            ),
        ),
    )
    encoded = encode_request(InferenceRequest("sharedgen:one", "corr-one", request))
    sdk = build_worker_shared_request(
        request, default_openai_provider_config()
    ).to_sdk_kwargs()
    assert sdk["text"]["format"]["name"] == "shared_abstraction_proposal"
    assert sdk["tools"] == [] and sdk["store"] is False
    assert decode_request(encoded).model_request == request
    raw = json.loads(encoded)
    raw["model_request"]["items"][0]["source_event_id"] = "forged-source"
    with pytest.raises(WorkerProtocolError):
        decode_request(json.dumps(raw).encode())
    suggestion = SharedSuggestion(
        "revise",
        "h0123456789abcdef",
        "durable_interest",
        "Curiosity benefits from multiple perspectives.",
        0.5,
    )
    result = WorkerResponse(
        "completed", 1, None, shared=suggestion, shared_operation=True
    )
    assert decode_response(encode_response(result)) == result
    raw_response = json.loads(encode_response(result))
    raw_response["memory_proposal"]["reviewed"] = True
    with pytest.raises(WorkerProtocolError):
        decode_response(json.dumps(raw_response).encode())
    with pytest.raises(ValueError):
        encode_response(WorkerResponse("completed", 1, None, shared=suggestion))
    bad_request = json.loads(encoded)
    bad_request["model_request"]["task"] = "grant_consent"
    with pytest.raises(WorkerProtocolError):
        decode_request(json.dumps(bad_request).encode())
    bad_request = json.loads(encoded)
    bad_request["model_request"]["items"][0]["can_instruct"] = True
    with pytest.raises(WorkerProtocolError):
        decode_request(json.dumps(bad_request).encode())
    with pytest.raises(ValueError, match="invalid shared response"):
        encode_response(
            WorkerResponse(
                "disabled",
                0,
                None,
                shared_operation=True,
                typed_operation=True,
            )
        )


def test_shared_request_bounds_and_duplicate_output_keys():
    for request in (
        SharedProposalRequest("", ()),
        SharedProposalRequest(
            "input", (SharedProposalItem("bad", "durable_interest", "Idea.", 0.5),)
        ),
        SharedProposalRequest(
            "x" * 8192,
            tuple(
                SharedProposalItem(f"h{i:016x}", "durable_interest", "a" * 4096, 0.5)
                for i in range(8)
            ),
        ),
    ):
        with pytest.raises(ValueError, match="invalid shared proposal request"):
            request.serialize()
    with pytest.raises(ValueError, match="invalid shared suggestion"):
        parse_shared_suggestion("x" * 8193)
    with pytest.raises(ValueError, match="invalid shared suggestion"):
        parse_shared_suggestion(
            '{"decision":"no_change","decision":"new","handle":null,'
            '"kind":null,"statement":null,"confidence":null}'
        )


def test_shared_gateway_and_server_reject_invalid_operation_shapes(tmp_path):
    request = SharedProposalRequest("Private input", ())
    config = replace(
        default_openai_provider_config(),
        deployment_stage=DeploymentStage.STAGE_1,
        paid_calls_enabled=True,
    )
    store = ProviderStateStore(EventStore(tmp_path / "worker.db"))
    gateway = TrustedOpenAIGateway(
        store=store, transport=FakeOpenAITransport([]), config=config
    )
    with pytest.raises(TypeError, match="shared proposal request"):
        gateway.submit_shared(
            object(), correlation_id="corr", operation_id="sharedgen:one"
        )
    with pytest.raises(TypeError, match="shared proposal request"):
        build_worker_shared_request(object(), config)
    bad = ProviderResponse(
        "invalid",
        "completed",
        (
            ProviderOutput(
                "message", (ProviderContent("output_text", '{"decision":"approve"}'),)
            ),
        ),
        None,
    )
    assert _parse_response(bad, shared=True).outcome is GatewayOutcome.INVALID_PROPOSAL
    with pytest.raises(TypeError, match="shared guard"):
        OpenAIWorkerServer(
            WorkerServerConfig(tmp_path / "socket", frozenset({os.getuid()})),
            gateway,
            shared_enabled="yes",
        )
    assert request.serialize()


def test_shared_application_enablement_requires_explicit_valid_configuration(
    tmp_path, monkeypatch
):
    events, _ledger, gate, shared, runtime, _requests = _setup(
        tmp_path, monkeypatch, SharedSuggestion("no_change", None, None, None, None)
    )
    with pytest.raises(TypeError, match="shared_proposals_enabled"):
        WorkerConversationRuntime(
            events,
            gate,
            runtime._worker,
            provider_state=runtime._provider,
            shared_intelligence=shared,
            shared_proposals_enabled="yes",
        )
    with pytest.raises(ValueError, match="shared intelligence"):
        WorkerConversationRuntime(
            events,
            gate,
            runtime._worker,
            provider_state=runtime._provider,
            shared_proposals_enabled=True,
        )


def test_shared_worker_permission_is_separate_current_and_bounded(tmp_path):
    path, _ = activation_file(tmp_path)
    activation = load_activation(path)
    stop_path = path.parent / "stop.json"
    _stop(stop_path, False)
    allowance = PilotAllowance.initialize(tmp_path / "allowance.db", activation)
    gate = PilotRequestGate(
        activation, allowance, activation_path=path, stop=PilotStop(stop_path)
    )
    request = InferenceRequest(
        "sharedgen:" + "a" * 64, "corr", SharedProposalRequest("Private input", ())
    )
    assert gate.authorize(request).outcome == "authorization_rejected"
    assert not gate.shared_commit_allowed()
    permission = path.parent / "shared.json"
    permission.write_text(
        json.dumps(
            {
                "version": SHARED_PERMISSION_VERSION,
                "pilot_id": activation.pilot_id,
                "max_operations": 1,
            }
        )
    )
    permission.chmod(0o400)
    gate.shared_permission_path = permission
    assert gate.authorize(request) is None
    assert gate.shared_commit_allowed()
    assert gate.authorize(request).outcome == "authorization_rejected"
    assert (
        gate.authorize(
            InferenceRequest("sharedgen:" + "b" * 64, "corr", request.model_request)
        ).outcome
        == "authorization_rejected"
    )
    _stop(stop_path, True)
    assert not gate.shared_commit_allowed()


def test_real_local_worker_transmits_one_bounded_suggestion_and_accounts_for_it(
    tmp_path,
):
    events = EventStore(tmp_path / "events.db")
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, events, clock=lambda: NOW)
    assert gate.reconcile()
    shared = SharedIntelligence(events, gate, clock=lambda: NOW)
    suggestion = {
        "decision": "new",
        "handle": None,
        "kind": "belief_or_hypothesis",
        "statement": "Compare multiple interpretations of an observation.",
        "confidence": 0.5,
    }
    provider_response = ProviderResponse(
        "response-shared",
        "completed",
        (
            ProviderOutput(
                "message", (ProviderContent("output_text", json.dumps(suggestion)),)
            ),
        ),
        None,
    )
    transport = FakeOpenAITransport([provider_response, reply_response()])
    worker_store = ProviderStateStore(EventStore(tmp_path / "worker-provider.db"))
    gateway = TrustedOpenAIGateway(
        store=worker_store,
        transport=transport,
        config=replace(
            default_openai_provider_config(),
            deployment_stage=DeploymentStage.STAGE_1,
            paid_calls_enabled=True,
            max_attempts=1,
        ),
    )
    socket_path = tmp_path / "worker.sock"
    server = OpenAIWorkerServer(
        WorkerServerConfig(socket_path, frozenset({os.getuid()})),
        gateway,
        shared_enabled=True,
    )
    server.start()
    thread = Thread(target=lambda: server.serve(max_connections=2), daemon=True)
    thread.start()
    try:
        runtime = WorkerConversationRuntime(
            events,
            gate,
            OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
            provider_state=ProviderStateStore(EventStore(tmp_path / "app-provider.db")),
            shared_intelligence=shared,
            shared_proposals_enabled=True,
            shared_proposal_audience=("participant-b",),
            clock=lambda: NOW,
        )
        runtime.submit(
            principal("a"), "first", "Private example of ambiguous evidence."
        )
    finally:
        thread.join(timeout=5)
        server.close()
    assert not thread.is_alive()
    assert len(transport.calls) == 2
    assert transport.calls[0].request.purpose == "shared_abstraction_proposal"
    assert transport.calls[1].request.purpose == "agent"
    with events._open() as db:
        assert db.execute("SELECT status FROM shared_operations").fetchone() == (
            "quarantined",
        )
    with worker_store._event_store._open() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM provider_usage_attempts"
        ).fetchone() == (2,)


def test_worker_rechecks_shared_permission_after_paid_inference(tmp_path):
    response = ProviderResponse(
        "response-shared",
        "completed",
        (
            ProviderOutput(
                "message",
                (
                    ProviderContent(
                        "output_text",
                        json.dumps(
                            {
                                "decision": "no_change",
                                "handle": None,
                                "kind": None,
                                "statement": None,
                                "confidence": None,
                            }
                        ),
                    ),
                ),
            ),
        ),
        None,
    )
    gateway = TrustedOpenAIGateway(
        store=ProviderStateStore(EventStore(tmp_path / "provider.db")),
        transport=FakeOpenAITransport([response]),
        config=replace(
            default_openai_provider_config(),
            deployment_stage=DeploymentStage.STAGE_1,
            paid_calls_enabled=True,
            max_attempts=1,
        ),
    )
    socket_path = tmp_path / "worker.sock"
    server = OpenAIWorkerServer(
        WorkerServerConfig(socket_path, frozenset({os.getuid()})),
        gateway,
        shared_enabled=True,
        shared_commit_guard=lambda: False,
    )
    server.start()
    thread = Thread(target=lambda: server.serve(max_connections=1), daemon=True)
    thread.start()
    try:
        result = OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()).infer(
            SharedProposalRequest("Private input", ()),
            operation_id="sharedgen:" + "a" * 64,
            correlation_id="corr",
        )
    finally:
        thread.join(timeout=5)
        server.close()
    assert not thread.is_alive()
    assert result.outcome == "authorization_rejected" and result.shared is None


def _direct_runtime(tmp_path, monkeypatch, suggestion):
    events, ledger, gate, shared, runtime, requests = _setup(
        tmp_path, monkeypatch, suggestion
    )
    content = "Private initiating evidence of uncertain patterns."
    event_id = append_event(events, principal("a"), "direct", content)
    assert gate.reconcile()
    return (
        events,
        ledger,
        gate,
        shared,
        runtime,
        requests,
        event_id,
        content,
        runtime._operation_id(event_id),
    )


def test_unknown_revision_handle_and_rejected_content_never_promote(
    tmp_path, monkeypatch
):
    events, _ledger, _gate, shared, runtime, _requests, event_id, content, reply = (
        _direct_runtime(
            tmp_path,
            monkeypatch,
            SharedSuggestion(
                "revise",
                "h0123456789abcdef",
                "belief_or_hypothesis",
                "Consider independent evidence.",
                0.5,
            ),
        )
    )
    runtime._propose_shared(principal("a"), event_id, content, "corr", reply)
    assert shared.context(principal("b")) == ()
    with events._open() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_operations").fetchone() == (0,)
    other = tmp_path / "other"
    other.mkdir()
    (
        events2,
        _ledger2,
        _gate2,
        shared2,
        runtime2,
        _requests2,
        source2,
        content2,
        reply2,
    ) = _direct_runtime(
        other,
        monkeypatch,
        SharedSuggestion(
            "new", None, "belief_or_hypothesis", 'A "quoted" private detail.', 0.5
        ),
    )
    runtime2._propose_shared(principal("a"), source2, content2, "corr", reply2)
    assert shared2.context(principal("b")) == ()
    with events2._open() as db:
        assert db.execute("SELECT status FROM shared_operations").fetchone() == (
            "quarantined",
        )


@pytest.mark.parametrize(
    "failure",
    (
        "outcome_uncertain",
        "communication_failure",
        "invalid_proposal",
        "raise",
    ),
)
def test_shared_worker_failures_do_not_retry_or_persist(tmp_path, monkeypatch, failure):
    events, _ledger, _gate, _shared, runtime, requests, source, content, reply = (
        _direct_runtime(
            tmp_path, monkeypatch, SharedSuggestion("no_change", None, None, None, None)
        )
    )
    original = runtime._worker.infer

    def fail(request, *, operation_id, correlation_id):
        if type(request) is SharedProposalRequest:
            if failure == "raise":
                raise ValueError("invalid fake worker result")
            return ClientResult(operation_id, correlation_id, failure, 0, None)
        return original(
            request, operation_id=operation_id, correlation_id=correlation_id
        )

    monkeypatch.setattr(runtime._worker, "infer", fail)
    runtime._propose_shared(principal("a"), source, content, "corr", reply)
    runtime._propose_shared(principal("a"), source, content, "corr", reply)
    with events._open() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_operations").fetchone() == (0,)
    assert requests == []


def test_shared_controls_and_preflight_changes_block_worker_contact(
    tmp_path, monkeypatch
):
    events, _ledger, _gate, _shared, runtime, requests, source, content, reply = (
        _direct_runtime(
            tmp_path, monkeypatch, SharedSuggestion("no_change", None, None, None, None)
        )
    )
    runtime._stop = lambda: True
    runtime._propose_shared(principal("a"), source, content, "corr", reply)
    assert requests == []
    runtime._stop = lambda: False
    runtime._propose_shared(principal("a"), source, "changed input", "corr", reply)
    assert requests == []
    monkeypatch.setattr(runtime._shared_proposal_audit, "valid", lambda *_args: False)
    runtime._propose_shared(principal("a"), source, content, "corr", reply)
    assert requests == []
    with events._open() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_operations").fetchone() == (0,)


def test_received_shared_result_reconciles_without_worker_and_bad_receipt_is_ignored(
    tmp_path, monkeypatch
):
    events, _ledger, _gate, shared, runtime, requests, source, content, reply = (
        _direct_runtime(
            tmp_path,
            monkeypatch,
            SharedSuggestion(
                "new",
                None,
                "belief_or_hypothesis",
                "Compare independent explanations.",
                0.5,
            ),
        )
    )
    op = "sharedgen:" + reply.removeprefix("reply:")
    audit = runtime._shared_proposal_audit
    audit.reserve(op, principal("a"), source, content)
    audit.receive(
        op,
        "participant-a",
        source,
        SharedSuggestion(
            "new",
            None,
            "belief_or_hypothesis",
            "Compare independent explanations.",
            0.5,
        ),
    )
    runtime._propose_shared(principal("a"), source, content, "corr", reply)
    runtime._propose_shared(principal("a"), source, content, "corr", reply)
    assert requests == []
    with events._open() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_operations").fetchone() == (1,)
    monkeypatch.setattr(
        audit, "read", lambda *_args: (_ for _ in ()).throw(ValueError("corrupt"))
    )
    runtime._reconcile_shared_proposal(principal("a"), source, content, reply)
    assert shared.context(principal("b")) == ()


def test_missing_source_or_quarantine_conflict_closes_received_result(
    tmp_path, monkeypatch
):
    events, _ledger, _gate, shared, runtime, _requests, source, content, reply = (
        _direct_runtime(
            tmp_path,
            monkeypatch,
            SharedSuggestion(
                "new", None, "belief_or_hypothesis", "Consider diverse evidence.", 0.5
            ),
        )
    )
    op = "sharedgen:" + reply.removeprefix("reply:")
    audit = runtime._shared_proposal_audit
    audit.reserve(op, principal("a"), source, content)
    audit.receive(
        op,
        "participant-a",
        source,
        SharedSuggestion(
            "new", None, "belief_or_hypothesis", "Consider diverse evidence.", 0.5
        ),
    )
    receipt = audit.read(op, "participant-a", source, content)
    monkeypatch.setattr(audit, "valid", lambda *_args: True)
    runtime._commit_shared_proposal(
        principal("a"), "missing-event", content, op, receipt
    )
    assert audit.read(op, "participant-a", source, content).status == "unavailable"
    other = tmp_path / "other"
    other.mkdir()
    _e, _l, _g, shared2, runtime2, _r, source2, content2, reply2 = _direct_runtime(
        other,
        monkeypatch,
        SharedSuggestion(
            "new", None, "belief_or_hypothesis", "Consider diverse evidence.", 0.5
        ),
    )
    monkeypatch.setattr(
        shared2,
        "quarantine",
        lambda _candidate, **_kwargs: (_ for _ in ()).throw(ValueError("conflict")),
    )
    runtime2._propose_shared(principal("a"), source2, content2, "corr", reply2)
    op2 = "sharedgen:" + reply2.removeprefix("reply:")
    assert (
        runtime2._shared_proposal_audit.read(
            op2, "participant-a", source2, content2
        ).status
        == "unavailable"
    )


def test_duplicate_intent_and_privacy_block_before_shared_worker_contact(
    tmp_path, monkeypatch
):
    _events, _ledger, _gate, _shared, runtime, requests, source, content, reply = (
        _direct_runtime(
            tmp_path, monkeypatch, SharedSuggestion("no_change", None, None, None, None)
        )
    )
    op = "sharedgen:" + reply.removeprefix("reply:")
    runtime._provider.reserve_attempt("ipc:" + op, "corr", NOW)
    runtime._propose_shared(principal("a"), source, content, "corr", reply)
    assert requests == []
    other = tmp_path / "other"
    other.mkdir()
    _e, _l, _g, _s, runtime2, requests2, source2, content2, reply2 = _direct_runtime(
        other, monkeypatch, SharedSuggestion("no_change", None, None, None, None)
    )

    def blocked(*_args):
        raise PrivacyReadBlocked("changed controls")

    monkeypatch.setattr(runtime2._shared_proposal_audit, "valid", blocked)
    runtime2._propose_shared(principal("a"), source2, content2, "corr", reply2)
    assert requests2 == []


@pytest.mark.parametrize("change", ("stop", "receipt_error"))
def test_shared_result_is_rejected_when_controls_or_receipt_change_after_worker(
    tmp_path, monkeypatch, change
):
    events, _ledger, _gate, _shared, runtime, requests, source, content, reply = (
        _direct_runtime(
            tmp_path,
            monkeypatch,
            SharedSuggestion(
                "new", None, "belief_or_hypothesis", "Consider diverse evidence.", 0.5
            ),
        )
    )
    if change == "stop":
        original = runtime._worker.infer

        def stop_after_inference(request, *, operation_id, correlation_id):
            result = original(
                request, operation_id=operation_id, correlation_id=correlation_id
            )
            runtime._stop = lambda: True
            return result

        monkeypatch.setattr(runtime._worker, "infer", stop_after_inference)
    else:
        monkeypatch.setattr(
            runtime._shared_proposal_audit,
            "receive",
            lambda *_args: (_ for _ in ()).throw(ValueError("receipt changed")),
        )
    runtime._propose_shared(principal("a"), source, content, "corr", reply)
    assert len(requests) == 1
    with events._open() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_operations").fetchone() == (0,)


def _direct_audit(tmp_path):
    events = EventStore(tmp_path / "events.db")
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, events, clock=lambda: NOW)
    content = "Private initiating input with uncertain evidence."
    event_id = append_event(events, principal("a"), "source", content)
    assert gate.reconcile()
    shared = SharedIntelligence(events, gate, clock=lambda: NOW)
    provider = ProviderStateStore(EventStore(tmp_path / "provider.db"))
    audit = SharedProposalAuditStore(
        provider, events, shared, ("participant-b",), lambda: NOW
    )
    return events, ledger, gate, shared, provider, audit, event_id, content


def test_shared_receipt_fails_closed_on_loss_and_corruption(tmp_path):
    events, _ledger, _gate, shared, provider, audit, event_id, content = _direct_audit(
        tmp_path
    )
    for bad_audience in ((), ("participant-b", "participant-b"), ("bad audience",)):
        with pytest.raises(ValueError):
            SharedProposalAuditStore(
                provider, events, shared, bad_audience, lambda: NOW
            )
    op = "sharedgen:direct"
    first = audit.reserve(op, principal("a"), event_id, content)
    assert (
        audit.reserve(op, principal("a"), event_id, content).snapshot == first.snapshot
    )
    assert audit.valid(principal("a"), event_id, content, first.snapshot)
    with pytest.raises(ValueError, match="identity conflict"):
        audit.read(op, "participant-b", event_id, content)
    with events._open() as db:
        original = db.execute(
            "SELECT proposal FROM private_extraction_receipts WHERE operation_id=?",
            (op,),
        ).fetchone()[0]
    bad_receipts = (
        "x" * 131073,
        json.dumps({"version": "unknown", "snapshot": {}, "proposal": None}),
        json.dumps(
            {"version": "shared-proposal-receipt-v2", "snapshot": {}, "proposal": None}
        ),
        json.dumps(
            {
                "version": "shared-proposal-receipt-v2",
                "snapshot": {
                    "selected": [],
                    "visible": [],
                    "sources": [],
                    "digest": "bad",
                    "dependencies": first.snapshot.dependencies,
                },
                "proposal": None,
            }
        ),
    )
    for bad in bad_receipts:
        with events._open() as db, db:
            db.execute(
                "UPDATE private_extraction_receipts SET proposal=? WHERE operation_id=?",
                (bad, op),
            )
        with pytest.raises(ValueError, match="invalid shared receipt"):
            audit.read(op, "participant-a", event_id, content)
    with events._open() as db, db:
        db.execute(
            "UPDATE private_extraction_receipts SET proposal=? WHERE operation_id=?",
            (original, op),
        )
        db.execute(
            "DELETE FROM private_extraction_receipts WHERE operation_id=?", (op,)
        )
    assert audit.read(op, "participant-a", event_id, content).status == "unavailable"
    assert audit._receipt(op, "participant-a", event_id) is None


def test_shared_receipt_rejects_duplicate_or_lost_response(tmp_path):
    events, _ledger, _gate, _shared, _provider, audit, event_id, content = (
        _direct_audit(tmp_path)
    )
    op = "sharedgen:direct"
    audit.reserve(op, principal("a"), event_id, content)
    suggestion = SharedSuggestion("no_change", None, None, None, None)
    with pytest.raises(ValueError, match="unavailable"):
        audit.receive("sharedgen:missing", "participant-a", event_id, suggestion)
    audit.receive(op, "participant-a", event_id, suggestion)
    assert audit.read(op, "participant-a", event_id, content).suggestion == suggestion
    with pytest.raises(ValueError, match="conflict"):
        audit.receive(op, "participant-a", event_id, suggestion)
    with events._open() as db, db:
        db.execute(
            "UPDATE private_extraction_receipts SET source_principal_id='participant-b' WHERE operation_id=?",
            (op,),
        )
    with pytest.raises(ValueError, match="receipt conflict"):
        audit.read(op, "participant-a", event_id, content)


def test_shared_snapshot_excludes_ineligible_heads_and_fails_on_changed_sources(
    tmp_path,
):
    events, ledger, gate, _shared, provider, _audit, event_id, content = _direct_audit(
        tmp_path
    )
    source = append_event(
        events, principal("a"), "older", "A distinct private supporting observation."
    )
    assert gate.reconcile()
    first = approval(source, op="existing-one", item="one")
    second = approval(
        event_id,
        op="existing-two",
        item="two",
        value="Consider competing explanations of uncertainty.",
    )
    shared = SharedIntelligence(
        events, gate, approvals=(first, second), clock=lambda: NOW
    )
    shared.commit(first)
    shared.commit(second)
    audit = SharedProposalAuditStore(
        provider, events, shared, ("participant-b",), lambda: NOW
    )
    # A head without current audience consent is never shown to the worker.
    assert audit._snapshot(principal("a"), event_id, content)[1].items == ()
    consent(ledger, source)
    consent(ledger, event_id)
    assert gate.reconcile()
    snapshot, request = audit._snapshot(principal("a"), event_id, content)
    assert len(request.items) == 1
    assert source in snapshot.sources
    assert audit.valid(principal("a"), event_id, content, snapshot)
    assert not audit.valid(
        principal("a"),
        event_id,
        content,
        type(snapshot)(
            (("h0123456789abcdef", "missing", 1, "missing"),),
            snapshot.visible,
            snapshot.sources,
            snapshot.digest,
            snapshot.dependencies,
        ),
    )
    # A changed source payload cannot reconcile a previously received suggestion.
    with events._open() as db, db:
        db.execute(
            "UPDATE private_payloads SET retention_status='deleted' WHERE payload_id=(SELECT payload_id FROM event_envelopes WHERE event_id=?)",
            (source,),
        )
    assert not audit.valid(principal("a"), event_id, content, snapshot)
    with events._open() as db, db:
        db.execute(
            "UPDATE private_payloads SET retention_status='active' WHERE payload_id=(SELECT payload_id FROM event_envelopes WHERE event_id=?)",
            (source,),
        )
        db.execute(
            "UPDATE shared_operations SET revision=32,expected_revision=31 WHERE operation_id='existing-one'"
        )
    assert (
        audit._snapshot(principal("a"), event_id, content)[1].items[0].statement
        == second.proposal.candidate.value
    )
    with events._open() as db, db:
        db.execute(
            "UPDATE shared_operations SET revision=1,expected_revision=0 WHERE operation_id='existing-one'"
        )
    # An audience not recorded on a head cannot select it even with source consent.
    from test_reviewed_shared import consent as grant

    grant(ledger, event_id, audience="participant-c")
    assert gate.reconcile()
    audit_c = SharedProposalAuditStore(
        provider, events, shared, ("participant-c",), lambda: NOW
    )
    assert audit_c._snapshot(principal("a"), event_id, content)[1].items == ()


def test_shared_receipt_fails_if_provider_intent_changed_before_receive(tmp_path):
    events, _ledger, _gate, _shared, provider, audit, event_id, content = _direct_audit(
        tmp_path
    )
    op = "sharedgen:direct"
    audit.reserve(op, principal("a"), event_id, content)
    with provider._event_store._open() as db, db:
        db.execute(
            "UPDATE private_extraction_audit SET status='accepted' WHERE operation_id=?",
            (op,),
        )
    with pytest.raises(ValueError, match="receipt conflict"):
        audit.receive(
            op,
            "participant-a",
            event_id,
            SharedSuggestion("no_change", None, None, None, None),
        )


def test_received_result_reconciles_after_crash_without_second_worker_call(
    tmp_path, monkeypatch
):
    events, ledger, _gate, _shared, runtime, requests = _setup(
        tmp_path,
        monkeypatch,
        SharedSuggestion(
            "new",
            None,
            "belief_or_hypothesis",
            "Consider evidence from multiple explanations.",
            0.5,
        ),
    )

    def crash(point):
        if point == "after_shared_proposal_receipt":
            raise RuntimeError("lost application response")

    runtime._checkpoint = crash
    a = principal("a")
    content = "Private uncertainty in a particular project."
    with pytest.raises(RuntimeError, match="lost application response"):
        runtime.submit(a, "first", content)
    assert len(requests) == 1
    reopened = EventStore(events.database_path)
    gate = PrivacyReadGate(ledger, reopened, clock=lambda: NOW)
    assert gate.reconcile()
    shared = SharedIntelligence(reopened, gate, clock=lambda: NOW)
    restarted = WorkerConversationRuntime(
        reopened,
        gate,
        runtime._worker,
        provider_state=ProviderStateStore(EventStore(tmp_path / "provider.db")),
        shared_intelligence=shared,
        shared_proposals_enabled=True,
        shared_proposal_audience=("participant-b",),
        clock=lambda: NOW,
    )
    restarted.submit(a, "first", content)
    assert len(requests) == 1
    with reopened._open() as db:
        assert db.execute("SELECT status FROM shared_operations").fetchone() == (
            "quarantined",
        )


def test_source_deletion_erases_received_shared_receipt_and_old_backup(
    tmp_path, monkeypatch
):
    events, ledger, _gate, _shared, runtime, requests = _setup(
        tmp_path,
        monkeypatch,
        SharedSuggestion(
            "new",
            None,
            "belief_or_hypothesis",
            "Compare independent explanations.",
            0.5,
        ),
    )

    def crash(point):
        if point == "after_shared_proposal_receipt":
            raise RuntimeError("lost result")

    runtime._checkpoint = crash
    with pytest.raises(RuntimeError, match="lost result"):
        runtime.submit(principal("a"), "first", "Private uncertain project details.")
    with events._open() as db:
        source = db.execute(
            "SELECT source_event_id FROM private_extraction_receipts"
        ).fetchone()[0]
    backup = tmp_path / "old-events.db"
    create_sqlite_backup(events.database_path, backup, 13, NOW)
    outcome = DeletionCoordinator(ledger, EventStoreErasureAdapter(events)).delete(
        deletion_context(), deletion_request(content_reference=source)
    )
    assert outcome.disposition.value == "applied"
    with events._open() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM private_extraction_receipts"
        ).fetchone() == (0,)
    restored = tmp_path / "restored.db"
    assert (
        QuarantinedRestore(ledger, 13, EventStoreRestoreAdapter)
        .restore(backup, restored, NOW)
        .activation_eligible
    )
    with sqlite3.connect(f"file:{restored}?immutable=1", uri=True) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM private_extraction_receipts"
        ).fetchone() == (0,)
    assert len(requests) == 1


def test_revision_inherits_sources_and_competing_history(tmp_path, monkeypatch):
    choices = iter(
        (
            lambda _request: SharedSuggestion(
                "new",
                None,
                "belief_or_hypothesis",
                "Consider independent explanations of uncertainty.",
                0.5,
            ),
            lambda request: SharedSuggestion(
                "revise",
                request.items[0].handle,
                "belief_or_hypothesis",
                "Compare evidence before settling on an explanation.",
                0.6,
            ),
        )
    )
    events, ledger, gate, shared, runtime, requests = _setup(
        tmp_path, monkeypatch, lambda request: next(choices)(request)
    )
    a, b = principal("a"), principal("b")
    runtime.submit(a, "first", "Private data about several uncertainties.")
    with events._open() as db:
        first_op, first_source = db.execute(
            "SELECT operation_id,event_id FROM protected_shared_sources"
        ).fetchone()
    consent(ledger, first_source)
    assert gate.reconcile()
    from app.cognition.review_cli import _inspect

    shared.review(
        _inspect(events, gate, first_op)[1],
        decision_id="decision-first",
        review_version="local-review-v1",
        approve=True,
    )
    runtime.submit(a, "second", "More private data suggests another interpretation.")
    assert len(requests) == 4
    assert len(requests[2].items) == 1
    with events._open() as db:
        rows = db.execute(
            "SELECT operation_id,status,parent_operation FROM shared_operations ORDER BY revision"
        ).fetchall()
        second_source = db.execute(
            "SELECT event_id FROM protected_shared_sources WHERE operation_id=? AND role='supporting'",
            (rows[1][0],),
        ).fetchone()[0]
        lineage = db.execute(
            "SELECT event_id,role FROM protected_shared_sources WHERE operation_id=? ORDER BY event_id",
            (rows[1][0],),
        ).fetchall()
    assert rows[1][1:] == ("quarantined", first_op)
    assert set(lineage) == {(first_source, "inherited"), (second_source, "supporting")}
    assert (
        shared.context(b)[0].text == "Consider independent explanations of uncertainty."
    )
    consent(ledger, second_source)
    assert gate.reconcile()
    shared.review(
        _inspect(events, gate, rows[1][0])[1],
        decision_id="decision-second",
        review_version="local-review-v1",
        approve=True,
    )
    latest = shared.context(b)[0]
    assert latest.text == "Compare evidence before settling on an explanation."
    assert latest.memory.alternatives == (
        "Consider independent explanations of uncertainty.",
    )


def test_changed_controls_after_model_block_quarantine_and_replay(
    tmp_path, monkeypatch
):
    events, _ledger, _gate, shared, runtime, requests = _setup(
        tmp_path,
        monkeypatch,
        SharedSuggestion(
            "new",
            None,
            "belief_or_hypothesis",
            "Compare competing models of evidence.",
            0.5,
        ),
    )
    runtime._checkpoint = (
        lambda point: setattr(runtime, "_stop", lambda: True)
        if point == "after_shared_proposal_receipt"
        else None
    )
    a = principal("a")
    runtime.submit(
        a, "first", "Private uncertain data from the initiating participant."
    )
    assert len(requests) == 1
    with events._open() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_operations").fetchone() == (0,)
    runtime.submit(
        a, "first", "Private uncertain data from the initiating participant."
    )
    assert len(requests) == 1
    assert shared.context(principal("b")) == ()
