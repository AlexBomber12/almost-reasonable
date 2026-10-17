"""Exact local approval, one-use worker and fail-closed client controls."""

from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import os
import runpy
import signal
import socket
import sys

import pytest

from app.core.context import ContextLimits, PrivateContextAssembler
from app.core.agent_proposals import ProposalValidation, ResearchProposal
from app.core.openai_worker_client import ClientResult, OpenAIWorkerClient
from app.core.provider_spend import ProviderStateStore
from app.core.public_research_authority import load_operator_approval
from app.core.public_research_client import PublicResearchClient
from app.research.brief import ResearchScreeningContext
from app.core.store import EventStore
from app.private_web.conversation import ConversationService
from app.private_web.cli import main as private_web_main
from app.private_web.auth import AuthStore
from app.private_web.public_research import PublicResearchBridge
from app.private_web.runtime import WorkerConversationRuntime
from app.private_web.runtime import Stage0ConversationRuntime
from app.privacy.application import PrivacyReadGate
from app.privacy.ledger import PrivacyLedger
from app.owner_control.store import OwnerStore
from app.trusted.openai_worker_protocol import WorkerProtocolError
from app.trusted.openai_transport import FakeOpenAITransport
from app.trusted.public_research import PublicResearchBoundary
from app.trusted.public_research_transport import PublicResearchTransport
from app.trusted import public_research_worker
from app.trusted.public_research_worker import PublicResearchStop, build_worker
from test_int23_connected import NOW, SDK, _write_approvals, raw_response, QUESTION
from test_private_web_conversation import principal


def _local(tmp_path):
    events = EventStore(tmp_path / "events.db")
    gate = PrivacyReadGate(PrivacyLedger(tmp_path / "ledger.db"), events)
    assert gate.reconcile()
    who = principal("a")
    content = "Приватная CANARY-INT23"
    ConversationService(
        events,
        PrivateContextAssembler(events, ContextLimits(32_000, 20)),
        clock=lambda: NOW,
    ).submit(who, "input", content)
    event = gate.retrieve_private_events(who)[0]
    operation_id = WorkerConversationRuntime._operation_id(event.event_id)
    app_path, worker_path = _write_approvals(
        tmp_path, event, who, operation_id, content
    )
    return events, gate, who, event, app_path, worker_path


def test_unapproved_question_never_crosses_boundary(tmp_path, monkeypatch):
    events, gate, who, event, app_path, _ = _local(tmp_path)
    client = PublicResearchClient(
        tmp_path / "missing.sock", expected_worker_uid=os.geteuid()
    )
    bridge = PublicResearchBridge(
        events, gate, client, enabled=True, clock=lambda: NOW, approval_path=app_path
    )
    approval = load_operator_approval(app_path, role="application")
    bridge.install_approval(approval, who)
    operation = type(
        "Operation",
        (),
        {
            "operation_id": approval.request.operation_id,
            "initiating_event_id": event.event_id,
        },
    )()
    assert bridge.permits(who, operation, QUESTION)
    assert not bridge.permits(who, operation, "Another unrelated public topic?")
    assert not bridge.permits(principal("b"), operation, QUESTION)
    monkeypatch.setattr(
        client, "execute", lambda _request: pytest.fail("worker contacted")
    )
    assert not bridge.permits(who, operation, "Private CANARY-INT23")
    bridge.revoke(operation.operation_id)
    assert not bridge.permits(who, operation, QUESTION)
    assert hashlib.sha256(event.content.encode()).hexdigest() == approval.input_sha256


def test_stage0_cannot_admit_public_research_bridge(tmp_path):
    events, gate, _, _, app_path, _ = _local(tmp_path)
    bridge = PublicResearchBridge(
        events,
        gate,
        PublicResearchClient(
            tmp_path / "unused.sock", expected_worker_uid=os.geteuid()
        ),
        enabled=True,
        clock=lambda: NOW,
        approval_path=app_path,
    )
    with pytest.raises(ValueError, match="Stage 0 cannot admit"):
        Stage0ConversationRuntime(
            events,
            gate,
            FakeOpenAITransport([]),
            research_bridge=bridge,
        )


def test_stop_and_spend_block_contact(tmp_path, monkeypatch):
    _, _, _, _, _, worker_path = _local(tmp_path)
    approval = load_operator_approval(worker_path, role="worker")
    sdk = SDK([raw_response()])
    store = ProviderStateStore(EventStore(tmp_path / "provider.db"))
    stopped = True
    boundary = PublicResearchBoundary(
        store=store,
        transport=PublicResearchTransport(sdk),
        enabled=True,
        emergency_stop=lambda: stopped,
        utcnow=lambda: NOW,
    )
    boundary.install_operator_authorization(approval, approval_path=worker_path)
    with pytest.raises(ValueError, match="one operator approval"):
        boundary.install_operator_authorization(approval, approval_path=worker_path)
    with pytest.raises(ValueError, match="cannot be combined"):
        boundary.install_synthetic_authorization(
            operation_id="synthetic-other",
            question=QUESTION,
            peer_uid=os.geteuid(),
            expires_at=NOW + timedelta(minutes=5),
        )
    synthetic = PublicResearchBoundary(
        store=ProviderStateStore(EventStore(tmp_path / "synthetic.db")),
        transport=PublicResearchTransport(SDK([])),
        enabled=True,
        utcnow=lambda: NOW,
    )
    synthetic.install_synthetic_authorization(
        operation_id="synthetic-other",
        question=QUESTION,
        peer_uid=os.geteuid(),
        expires_at=NOW + timedelta(minutes=5),
    )
    with pytest.raises(ValueError, match="one operator approval"):
        synthetic.install_operator_authorization(approval, approval_path=worker_path)
    assert (
        boundary.execute(approval.request, peer_uid=os.geteuid())["outcome"]
        == "emergency_stop"
    )
    stopped = False
    monkeypatch.setattr(store, "is_spend_suspended", lambda: True)
    assert (
        boundary.execute(approval.request, peer_uid=os.geteuid())["outcome"]
        == "spend_suspended"
    )
    monkeypatch.setattr(store, "is_spend_suspended", lambda: False)
    assert (
        boundary.execute(approval.request, peer_uid=os.geteuid())["outcome"]
        == "completed"
    )
    assert (
        boundary.execute(approval.request, peer_uid=os.geteuid())["outcome"]
        == "duplicate_operation"
    )
    assert len(sdk.calls) == 1
    worker_path.unlink()
    assert (
        boundary._guard(boundary._grants[approval.request.operation_id])
        == "authorization_denied"
    )


@pytest.mark.parametrize(
    "question", ["How do deep ocean currents form?", "Why do rainbows form?"]
)
def test_distinct_operator_approved_public_questions(tmp_path, question):
    _, _, _, _, _, worker_path = _local(tmp_path)
    data = json.loads(worker_path.read_text())
    data["question"] = question
    worker_path.write_text(json.dumps(data))
    approval = load_operator_approval(worker_path, role="worker")
    sdk = SDK([raw_response()])
    boundary = PublicResearchBoundary(
        store=ProviderStateStore(EventStore(tmp_path / "provider.db")),
        transport=PublicResearchTransport(sdk),
        enabled=True,
        utcnow=lambda: NOW,
    )
    boundary.install_operator_authorization(approval, approval_path=worker_path)
    assert (
        boundary.execute(approval.request, peer_uid=os.geteuid())["outcome"]
        == "completed"
    )
    assert question in sdk.calls[0]["input"]
    assert (
        boundary.execute(
            replace(approval.request, question="Different topic?"),
            peer_uid=os.geteuid(),
        )["outcome"]
        == "authorization_denied"
    )
    assert len(sdk.calls) == 1


def test_worker_operator_approval_validation_and_changed_file(tmp_path):
    _, _, _, _, _, worker_path = _local(tmp_path)
    approval = load_operator_approval(worker_path, role="worker")

    def worker(name, **kwargs):
        return PublicResearchBoundary(
            store=ProviderStateStore(EventStore(tmp_path / f"{name}.db")),
            transport=PublicResearchTransport(SDK([])),
            enabled=True,
            utcnow=lambda: NOW,
            **kwargs,
        )

    fresh = worker("fresh")
    with pytest.raises(TypeError, match="operator worker approval"):
        fresh.install_operator_authorization(object(), approval_path=worker_path)
    with pytest.raises(ValueError, match="file changed"):
        fresh.install_operator_authorization(
            replace(approval, peer_uid=os.geteuid() + 1),
            approval_path=worker_path,
        )

    data = json.loads(worker_path.read_text())
    data["issued_at"] = (NOW - timedelta(minutes=10)).isoformat()
    data["reviewed_at"] = data["issued_at"]
    data["expires_at"] = (NOW - timedelta(minutes=5)).isoformat()
    worker_path.write_text(json.dumps(data))
    expired = load_operator_approval(worker_path, role="worker")
    with pytest.raises(ValueError, match="not current"):
        fresh.install_operator_authorization(expired, approval_path=worker_path)

    data["issued_at"] = NOW.isoformat()
    data["reviewed_at"] = NOW.isoformat()
    data["expires_at"] = (NOW + timedelta(minutes=5)).isoformat()
    worker_path.write_text(json.dumps(data))
    screened = worker(
        "screened", screening=ResearchScreeningContext(private_canaries=(QUESTION,))
    )
    with pytest.raises(ValueError, match="public brief declined"):
        screened.install_operator_authorization(
            load_operator_approval(worker_path, role="worker"),
            approval_path=worker_path,
        )

    approved = worker("approved")
    approved.install_operator_authorization(
        load_operator_approval(worker_path, role="worker"),
        approval_path=worker_path,
    )
    data["question"] = "Why do rainbows form?"
    worker_path.write_text(json.dumps(data))
    grant = approved._grants[approval.request.operation_id]
    assert approved._guard(grant) == "authorization_denied"


def test_operator_files_bind_exact_input_and_public_projection(tmp_path):
    events, gate, who, event, app_path, worker_path = _local(tmp_path)
    app = load_operator_approval(app_path, role="application")
    worker = load_operator_approval(worker_path, role="worker")
    assert app.request == worker.request
    assert "CANARY" not in worker_path.read_text()
    client = PublicResearchClient(
        tmp_path / "missing.sock", expected_worker_uid=os.geteuid()
    )
    bridge = PublicResearchBridge(
        events, gate, client, enabled=True, clock=lambda: NOW, approval_path=app_path
    )
    bridge.install_approval(app, who)
    with pytest.raises(ValueError):
        bridge.install_approval(replace(app, input_sha256="0" * 64), who)
    with pytest.raises(ValueError):
        bridge.install_approval(replace(app, event_id="wrong"), who)
    with pytest.raises(ValueError):
        bridge.install_approval(replace(app, authenticated_source_ref="wrong"), who)
    with pytest.raises(ValueError):
        bridge.install_approval(replace(app, peer_uid=os.geteuid() + 1), who)
    operation = type(
        "Operation",
        (),
        {
            "operation_id": app.request.operation_id,
            "initiating_event_id": event.event_id,
        },
    )()
    modified = json.loads(app_path.read_text())
    modified["question"] = "What causes ocean tides?"
    app_path.write_text(json.dumps(modified))
    assert not bridge.permits(who, operation, QUESTION)
    with pytest.raises(ValueError, match="operator approval file changed"):
        bridge.install_approval(app, who)
    app_path.unlink()
    assert not bridge.permits(who, operation, QUESTION)


def test_original_authenticated_source_binding_is_checked(tmp_path):
    events, gate, who, event, app_path, _ = _local(tmp_path)
    approval = load_operator_approval(app_path, role="application")
    db = events._open()
    try:
        with db:
            db.execute(
                "UPDATE event_envelopes SET authenticated_source_ref=? WHERE event_id=?",
                ("different-source", event.event_id),
            )
    finally:
        db.close()
    bridge = PublicResearchBridge(
        events,
        gate,
        PublicResearchClient(
            tmp_path / "unused.sock", expected_worker_uid=os.geteuid()
        ),
        enabled=True,
        clock=lambda: NOW,
        approval_path=app_path,
    )
    with pytest.raises(ValueError, match="source identity"):
        bridge.install_approval(approval, who)


def test_public_client_checks_peer_before_request_bytes(tmp_path, monkeypatch):
    directory = tmp_path / "ipc"
    directory.mkdir(mode=0o700)
    path = directory / "research.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(1)
    client = PublicResearchClient(path, expected_worker_uid=os.geteuid())
    _, _, _, _, _, worker_path = _local(tmp_path)
    request = load_operator_approval(worker_path, role="worker").request
    path.chmod(0o666)
    with pytest.raises(WorkerProtocolError, match="unsafe"):
        client.execute(request)
    path.chmod(0o600)
    monkeypatch.setattr(
        "app.core.public_research_client._peer_uid",
        lambda _connection: os.geteuid() + 1,
    )
    with pytest.raises(WorkerProtocolError):
        client.execute(request)
    connection, _ = listener.accept()
    connection.settimeout(1)
    assert connection.recv(1) == b""
    connection.close()
    listener.close()


@pytest.mark.parametrize("failure", ["lost_response", "crash_after_started"])
def test_lost_or_crashed_attempt_is_not_retried(tmp_path, monkeypatch, failure):
    events, gate, who, event, app_path, _ = _local(tmp_path)
    client = PublicResearchClient(
        tmp_path / "unused.sock", expected_worker_uid=os.geteuid()
    )
    bridge = PublicResearchBridge(
        events, gate, client, enabled=True, clock=lambda: NOW, approval_path=app_path
    )
    bridge.install_approval(load_operator_approval(app_path, role="application"), who)
    inference = OpenAIWorkerClient(
        tmp_path / "unused-infer.sock", expected_worker_uid=os.geteuid()
    )
    called = []

    def infer(_request, *, operation_id, correlation_id):
        called.append("infer")
        return ClientResult(
            operation_id,
            correlation_id,
            "completed",
            1,
            None,
            ProposalValidation(ResearchProposal(QUESTION), None),
        )

    monkeypatch.setattr(inference, "infer", infer)
    if failure == "lost_response":

        def lost(_request):
            called.append("research")
            raise OSError("synthetic lost response")

        monkeypatch.setattr(client, "execute", lost)

    def checkpoint(point):
        if failure == "crash_after_started" and point == "after_research_started":
            raise RuntimeError("synthetic crash")

    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    runtime = WorkerConversationRuntime(
        events,
        gate,
        inference,
        provider_state=provider,
        research_bridge=bridge,
        clock=lambda: NOW,
        checkpoint=checkpoint,
    )
    if failure == "crash_after_started":
        with pytest.raises(RuntimeError, match="synthetic crash"):
            runtime.submit(who, "input", event.content)
    else:
        assert (
            runtime.submit(who, "input", event.content).model_state.value
            == "research_uncertain"
        )
    reopened_events = EventStore(events.database_path)
    reopened_gate = PrivacyReadGate(
        PrivacyLedger(tmp_path / "ledger.db"), reopened_events
    )
    assert reopened_gate.reconcile()
    reopened_bridge = PublicResearchBridge(
        reopened_events,
        reopened_gate,
        PublicResearchClient(
            tmp_path / "unused.sock", expected_worker_uid=os.geteuid()
        ),
        enabled=True,
        clock=lambda: NOW,
        approval_path=app_path,
    )
    reopened_bridge.install_approval(
        load_operator_approval(app_path, role="application"), who
    )
    reopened = WorkerConversationRuntime(
        reopened_events,
        reopened_gate,
        inference,
        provider_state=provider,
        research_bridge=reopened_bridge,
        clock=lambda: NOW,
    )
    assert (
        reopened.submit(who, "input", event.content).model_state.value
        == "research_uncertain"
    )
    assert called.count("infer") == 1
    assert called.count("research") <= 1


@pytest.mark.parametrize(
    "change",
    [
        {"purpose": "private"},
        {"execution_allowance": 2},
        {"peer_uid": True},
        {"reviewer": ""},
        {"expires_at": (NOW + timedelta(minutes=11)).isoformat()},
        {"question": "https://private.example/path"},
    ],
)
def test_bad_operator_approval_is_rejected(tmp_path, change):
    _, _, _, _, _, path = _local(tmp_path)
    data = json.loads(path.read_text())
    data.update(change)
    path.write_text(json.dumps(data))
    with pytest.raises((ValueError, WorkerProtocolError)):
        load_operator_approval(path, role="worker")


def test_operator_file_shape_permissions_and_binding_fail_closed(tmp_path, monkeypatch):
    events, gate, who, _, app_path, worker_path = _local(tmp_path)
    with pytest.raises(ValueError):
        load_operator_approval(worker_path, role="wrong")
    with pytest.raises(ValueError):
        load_operator_approval("relative.json", role="worker")
    link = tmp_path / "linked.json"
    link.symlink_to(worker_path)
    with pytest.raises(ValueError):
        load_operator_approval(link, role="worker")
    worker_path.chmod(0o644)
    with pytest.raises(ValueError):
        load_operator_approval(worker_path, role="worker")
    worker_path.chmod(0o600)
    worker_path.write_text('{"role":"worker","role":"worker"}')
    with pytest.raises(ValueError, match="duplicate"):
        load_operator_approval(worker_path, role="worker")
    with pytest.raises(ValueError):
        load_operator_approval(app_path, role="worker")
    data = json.loads(app_path.read_text())
    data["input_sha256"] = "bad"
    app_path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_operator_approval(app_path, role="application")
    client = PublicResearchClient(
        tmp_path / "missing.sock", expected_worker_uid=os.geteuid()
    )
    with pytest.raises(ValueError):
        PublicResearchClient("relative.sock", expected_worker_uid=os.geteuid())
    with pytest.raises(TypeError):
        PublicResearchBridge(
            events, gate, object(), clock=lambda: NOW, approval_path=app_path
        )
    with pytest.raises(ValueError):
        PublicResearchBridge(
            events, gate, client, enabled=1, clock=lambda: NOW, approval_path=app_path
        )
    with pytest.raises(ValueError):
        PublicResearchBridge(
            events, gate, client, clock=lambda: NOW, approval_path=None
        )
    bridge = PublicResearchBridge(
        events, gate, client, enabled=False, clock=lambda: NOW, approval_path=app_path
    )
    with pytest.raises(ValueError):
        bridge.install_approval(object(), who)
    operation = type(
        "Operation", (), {"operation_id": "missing", "initiating_event_id": "missing"}
    )()
    assert not bridge.permits(who, operation, QUESTION)


def test_operator_file_bounds_and_parent_policy(tmp_path, monkeypatch):
    _, _, _, _, _, worker_path = _local(tmp_path)
    worker_path.write_bytes(b"x" * 8193)
    with pytest.raises(ValueError):
        load_operator_approval(worker_path, role="worker")
    worker_path.write_text("{}")
    monkeypatch.setattr(
        "app.core.public_research_authority.os.read", lambda *_args: b"x" * 8193
    )
    with pytest.raises(ValueError):
        load_operator_approval(worker_path, role="worker")
    monkeypatch.undo()
    tmp_path.chmod(0o777)
    try:
        with pytest.raises(ValueError):
            load_operator_approval(worker_path, role="worker")
    finally:
        tmp_path.chmod(0o700)


def test_worker_bootstrap_uses_local_stop_and_reviewed_file(tmp_path):
    _, _, _, _, _, worker_path = _local(tmp_path)
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    data = json.loads(worker_path.read_text())
    data["issued_at"] = now.isoformat()
    data["reviewed_at"] = now.isoformat()
    data["expires_at"] = (now + timedelta(minutes=5)).isoformat()
    worker_path.write_text(json.dumps(data))
    stop_path = tmp_path / "stop.json"
    stop = PublicResearchStop(stop_path)
    assert stop.active()
    stop_path.write_text('{"version":"public-research-stop-v1","stop":false}')
    stop_path.chmod(0o400)
    assert not stop.active()
    with pytest.raises(ValueError, match="existing research provider database"):
        build_worker(
            socket_path=tmp_path / "research.sock",
            provider_database=tmp_path / "worker.db",
            approval_path=worker_path,
            stop_path=stop_path,
            project_id="proj_synthetic",
            organization_id="org-synthetic",
            transport=PublicResearchTransport(SDK([])),
        )
    EventStore(tmp_path / "worker.db")
    server = build_worker(
        socket_path=tmp_path / "research.sock",
        provider_database=tmp_path / "worker.db",
        approval_path=worker_path,
        stop_path=stop_path,
        project_id="proj_synthetic",
        organization_id="org-synthetic",
        transport=PublicResearchTransport(SDK([raw_response()])),
    )
    assert (
        server._boundary._guard(server._boundary._grants[data["operation_id"]]) is None
    )
    stop_path.unlink()
    assert (
        server._boundary._guard(server._boundary._grants[data["operation_id"]])
        == "emergency_stop"
    )


def test_worker_cli_lifecycle_and_module_entry(tmp_path, monkeypatch):
    observed = []

    class Server:
        def start(self):
            observed.append("start")

        def serve(self, *, should_stop):
            observed.append("serve")
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            assert should_stop()

        def close(self):
            observed.append("close")

    def fake_builder(**kwargs):
        assert kwargs["socket_path"] == tmp_path / "sock"
        observed.append("build")
        return Server()

    monkeypatch.setattr(public_research_worker, "build_worker", fake_builder)
    assert (
        public_research_worker.main(
            [
                "--socket",
                str(tmp_path / "sock"),
                "--provider-database",
                str(tmp_path / "provider.db"),
                "--approval",
                str(tmp_path / "approval.json"),
                "--stop-file",
                str(tmp_path / "stop.json"),
                "--project-id",
                "proj_synthetic",
                "--organization-id",
                "org-synthetic",
            ]
        )
        == 0
    )
    assert observed == ["build", "start", "serve", "close"]
    monkeypatch.setattr(sys, "argv", ["public_research_worker"])
    with pytest.raises(SystemExit):
        runpy.run_module("app.trusted.public_research_worker", run_name="__main__")


def test_application_cli_requires_explicit_public_composition(tmp_path):
    _, _, _, _, app_path, _ = _local(tmp_path)
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    data = json.loads(app_path.read_text())
    data["issued_at"] = now.isoformat()
    data["reviewed_at"] = now.isoformat()
    data["expires_at"] = (now + timedelta(minutes=5)).isoformat()
    app_path.write_text(json.dumps(data))
    auth_path = tmp_path / "auth.db"
    ipc_path = tmp_path / "ipc.db"
    owner_path = tmp_path / "owner"
    AuthStore(auth_path)
    EventStore(ipc_path)
    OwnerStore(owner_path)
    base = [
        "serve",
        "--auth-db",
        str(auth_path),
        "--event-db",
        str(tmp_path / "events.db"),
        "--ipc-state-db",
        str(ipc_path),
        "--privacy-ledger",
        str(tmp_path / "ledger.db"),
        "--owner-directory",
        str(owner_path),
        "--worker-socket",
        str(tmp_path / "infer.sock"),
        "--worker-uid",
        str(os.geteuid()),
        "--allowed-host",
        "chat.test",
        "--allowed-origin",
        "https://chat.test",
    ]
    research = [
        "--enable-public-research",
        "--public-research-socket",
        str(tmp_path / "research.sock"),
        "--public-research-worker-uid",
        str(os.geteuid()),
        "--public-research-approval",
        str(app_path),
    ]
    with pytest.raises(SystemExit, match="explicit enablement"):
        private_web_main(base + research[:-2])
    with pytest.raises(SystemExit, match="require worker mode"):
        private_web_main(
            [
                "serve",
                "--auth-db",
                str(auth_path),
                "--event-db",
                str(tmp_path / "events.db"),
                "--allowed-host",
                "chat.test",
                "--allowed-origin",
                "https://chat.test",
                *research,
            ]
        )
    observed = {}

    class Server:
        def serve_forever(self):
            observed["served"] = True

    def factory(_host, _port, application):
        observed["application"] = application
        return Server()

    assert private_web_main(base + research, server_factory=factory) == 0
    assert observed["served"]
    assert (
        type(observed["application"]._conversations._research) is PublicResearchBridge
    )
