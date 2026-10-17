"""Connected INT-23 exercise with synthetic credentials and no network provider."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import secrets

from app.core.openai_worker_client import OpenAIWorkerClient
from app.core.context import ContextLimits, PrivateContextAssembler
from app.core.provider_contract import DeploymentStage, default_openai_provider_config
from app.core.provider_spend import ProviderStateStore
from app.core.public_research_authority import load_operator_approval
from app.core.public_research_client import PublicResearchClient
from app.core.store import EventStore
from app.private_web.auth import AuthStore
from app.private_web.conversation import ConversationService
from app.private_web.public_research import PublicResearchBridge
from app.private_web.runtime import WorkerConversationRuntime
from app.private_web.wsgi import PrivateWebApplication, PrivateWebConfig
from app.privacy.application import PrivacyReadGate
from app.privacy.ledger import PrivacyLedger
from app.trusted.openai_gateway import TrustedOpenAIGateway
from app.trusted.openai_transport import FakeOpenAITransport
from app.trusted.openai_worker_server import OpenAIWorkerServer, WorkerServerConfig
from app.trusted.public_research import PublicResearchBoundary, PublicResearchServer
from app.trusted.public_research_transport import PublicResearchTransport
from test_int20_public_research import SDK, raw_response
from test_private_conversation_runtime import response
from test_private_web_http import Browser


NOW = datetime(2030, 1, 1, tzinfo=UTC)
QUESTION = "How do deep ocean currents form?"


def _inference_process(socket_path, database, ready):
    fake = FakeOpenAITransport([response("research", QUESTION) for _ in range(2)])
    gateway = TrustedOpenAIGateway(
        store=ProviderStateStore(EventStore(Path(database))),
        transport=fake,
        config=replace(
            default_openai_provider_config(),
            deployment_stage=DeploymentStage.STAGE_1,
            paid_calls_enabled=True,
            max_attempts=1,
        ),
    )
    server = OpenAIWorkerServer(
        WorkerServerConfig(Path(socket_path), frozenset({os.geteuid()})), gateway
    )
    try:
        server.start()
        ready.send(True)
        server.serve(max_connections=2)
        ready.send(len(fake.calls))
    finally:
        server.close()
        ready.close()


def _research_process(socket_path, database, approval_path, ready):
    sdk = SDK([raw_response()])
    boundary = PublicResearchBoundary(
        store=ProviderStateStore(EventStore(Path(database))),
        transport=PublicResearchTransport(sdk),
        enabled=True,
        utcnow=lambda: NOW,
    )
    approval = load_operator_approval(approval_path, role="worker")
    boundary.install_operator_authorization(approval, approval_path=approval_path)
    server = PublicResearchServer(
        WorkerServerConfig(Path(socket_path), frozenset({approval.peer_uid})),
        boundary,
    )
    try:
        server.start()
        ready.send(True)
        server.serve(max_connections=1)
        ready.send(tuple(call["input"] for call in sdk.calls))
    finally:
        server.close()
        ready.close()


def _write_approvals(folder, event, principal, operation_id, content):
    common = {
        "version": "int23-operator-approval-v1",
        "purpose": "private-conversation-public-research-v1",
        "configuration": "public-research-v1",
        "execution_allowance": 1,
        "operation_id": operation_id,
        "question": QUESTION,
        "authorization": secrets.token_hex(32),
        "peer_uid": os.geteuid(),
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "reviewer": "independent-local-operator",
        "reviewed_at": NOW.isoformat(),
    }
    worker_path = folder / "worker-approval.json"
    app_path = folder / "application-approval.json"
    worker_path.write_text(json.dumps({**common, "role": "worker"}))
    app_path.write_text(
        json.dumps(
            {
                **common,
                "role": "application",
                "principal_id": principal.principal_id.value,
                "authenticated_source_ref": principal.authenticated_source.value,
                "event_id": event.event_id,
                "input_sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
        )
    )
    worker_path.chmod(0o600)
    app_path.chmod(0o600)
    return app_path, worker_path


def test_authenticated_wsgi_two_workers_and_durable_reply(tmp_path):
    auth = AuthStore(tmp_path / "auth.db", clock=lambda: NOW)
    auth.provision_account("alice", "synthetic-password-alice", "participant-alice")
    events = EventStore(tmp_path / "events.db")
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, events)
    assert gate.reconcile()
    config = PrivateWebConfig(
        frozenset({"chat.test"}), frozenset({"https://chat.test"})
    )
    browser = Browser(
        PrivateWebApplication(
            auth,
            ConversationService(
                events,
                PrivateContextAssembler(events, ContextLimits(32_000, 20)),
                clock=lambda: NOW,
            ),
            config,
        )
    )
    assert browser.login("alice", "synthetic-password-alice")[0] == "303 See Other"
    content = "Приватный вопрос CANARY-INT23-A"
    assert (
        json.loads(browser.submit("int23-input", content)[2])["model_state"]
        == "unavailable"
    )
    principal = auth.authenticate(browser.cookies["ar_session"]).trusted_context
    event = gate.retrieve_private_events(principal)[0]
    operation_id = WorkerConversationRuntime._operation_id(event.event_id)
    app_path, worker_path = _write_approvals(
        tmp_path, event, principal, operation_id, content
    )
    directory = tmp_path / "ipc"
    directory.mkdir(mode=0o700)
    ctx = multiprocessing.get_context("spawn")
    infer_parent, infer_child = ctx.Pipe()
    research_parent, research_child = ctx.Pipe()
    infer = ctx.Process(
        target=_inference_process,
        args=(directory / "infer.sock", tmp_path / "infer.db", infer_child),
    )
    research = ctx.Process(
        target=_research_process,
        args=(
            directory / "research.sock",
            tmp_path / "research.db",
            worker_path,
            research_child,
        ),
    )
    infer.start()
    research.start()
    try:
        assert infer_parent.recv() and research_parent.recv()
        bridge = PublicResearchBridge(
            events,
            gate,
            PublicResearchClient(
                directory / "research.sock", expected_worker_uid=os.geteuid()
            ),
            enabled=True,
            clock=lambda: NOW,
            approval_path=app_path,
        )
        bridge.install_approval(
            load_operator_approval(app_path, role="application"), principal
        )
        runtime = WorkerConversationRuntime(
            events,
            gate,
            OpenAIWorkerClient(
                directory / "infer.sock", expected_worker_uid=os.geteuid()
            ),
            provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
            research_bridge=bridge,
            clock=lambda: NOW,
        )
        browser.app = PrivateWebApplication(auth, runtime, config)
        first = json.loads(browser.submit("int23-input", content)[2])
        assert first["model_state"] == "completed"
        assert "unverified" in first["response"]
        assert "not_evaluated" in first["response"]
        assert "https://example.org/paper" in first["response"]
        assert (
            json.loads(browser.submit("int23-input", content)[2])["response"]
            == first["response"]
        )
        no_bridge = WorkerConversationRuntime(
            events,
            gate,
            OpenAIWorkerClient(
                directory / "infer.sock", expected_worker_uid=os.geteuid()
            ),
            provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
            clock=lambda: NOW,
        )
        assert (
            no_bridge.submit(principal, "int23-input", content).model_state.value
            == "research_denied"
        )
        no_bridge._research = object()
        assert (
            no_bridge.submit(principal, "int23-input", content).model_state.value
            == "research_denied"
        )
        assert (
            len(json.loads(browser.request("GET", "/api/history")[2])["messages"]) == 2
        )
        denied = json.loads(
            browser.submit("int23-denied", "Приватный CANARY-INT23-B")[2]
        )
        assert denied["model_state"] == "research_denied"
        assert infer_parent.recv() == 2
        sent = research_parent.recv()
        assert len(sent) == 1 and QUESTION in sent[0]
        assert content not in str(sent)
        assert principal.principal_id.value not in str(sent)
        app_path.unlink()
        assert (
            json.loads(browser.submit("int23-input", content)[2])["model_state"]
            == "research_denied"
        )
    finally:
        for process in (infer, research):
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
        infer_parent.close()
        research_parent.close()
