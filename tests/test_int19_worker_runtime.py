"""Connected private HTTP to a distinct deterministic inference worker."""

from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import socket
import struct
from threading import Thread

import pytest
from acceptance.guards import install

from app.cognition.conversation_context import MemoryModelRequest, ModelMemory
from app.cognition.shared import SharedRecord
from app.cognition.shared import SharedIntelligence
from app.cognition.shared_context import SharedModelRequest
from app.core.openai_worker_client import OpenAIWorkerClient
from app.core.openai_worker_client import ClientResult
from app.core.agent_proposals import (
    NoActionProposal,
    NoActionReason,
    ProposalValidation,
    PublicationProposal,
    ReplyProposal,
    ResearchProposal,
)
from app.core.provider_contract import DeploymentStage, default_openai_provider_config
from app.core.provider_contract import build_worker_context_request
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.private_web.auth import AuthStore
from app.private_web.cli import main as private_web_main
from app.private_web.privacy import ParticipantPrivacyControls
from app.private_web.runtime import Stage0ConversationRuntime, WorkerConversationRuntime
from app.private_web.wsgi import PrivateWebApplication, PrivateWebConfig
from app.privacy.application import PrivacyReadGate
from app.privacy.application import PrivacyReadBlocked
from app.privacy.ledger import PrivacyLedger
from app.privacy.application import EventStoreErasureAdapter
from app.privacy.deletion import DeletionCoordinator
from app.trusted.openai_gateway import TrustedOpenAIGateway
from app.trusted.openai_transport import FakeOpenAITransport
from app.trusted.openai_worker_protocol import (
    CONTEXT_PROTOCOL_VERSION,
    InferenceRequest,
    WorkerProtocolError,
    decode_request,
    encode_request,
)
from app.trusted.openai_worker_server import OpenAIWorkerServer, WorkerServerConfig
from app.owner_control.store import OwnerStore
from test_openai_worker_protocol import model_request
from test_private_conversation_runtime import response
from test_private_web_http import Browser, Clock
from test_participant_privacy_http import acknowledge
from test_private_memory_runtime import extractor
from test_cognition_pipeline import append_event, principal as synthetic_principal, NOW
from test_shared_intelligence import approval
from test_privacy_deletion import (
    context as deletion_context,
    request as deletion_request,
)


@pytest.fixture(autouse=True)
def offline_network_guard(monkeypatch):
    install(monkeypatch, local_ipc=True)


def _memory():
    return ModelMemory(
        "durable_interest",
        "Синтетический интерес",
        "active",
        None,
        None,
        None,
        "synthetic",
        1,
        0,
        None,
        (),
        None,
        (),
        None,
        "unverified",
        "participant_assertion",
    )


def _worker(socket_path, event_path, ready, count):
    events = EventStore(Path(event_path))
    fake = FakeOpenAITransport([response() for _ in range(count)])
    config = replace(
        default_openai_provider_config(),
        deployment_stage=DeploymentStage.STAGE_1,
        paid_calls_enabled=True,
        max_attempts=1,
    )
    gateway = TrustedOpenAIGateway(
        store=ProviderStateStore(events),
        transport=fake,
        config=config,
    )
    server = OpenAIWorkerServer(
        WorkerServerConfig(Path(socket_path), frozenset({os.getuid()})), gateway
    )
    try:
        server.start()
        ready.send(True)
        server.serve(max_connections=count)
        ready.send(tuple(call.request.input_text for call in fake.calls))
    finally:
        server.close()
        ready.close()


def _broken_worker(socket_path, event_path, ready, mode):
    if mode == "unauthorized":
        events = EventStore(Path(event_path))
        fake = FakeOpenAITransport([])
        gateway = TrustedOpenAIGateway(
            store=ProviderStateStore(events),
            transport=fake,
            config=default_openai_provider_config(),
        )
        server = OpenAIWorkerServer(
            WorkerServerConfig(Path(socket_path), frozenset({os.getuid() + 1})), gateway
        )
        try:
            server.start()
            ready.send(True)
            server.serve_once()
            ready.send(len(fake.calls))
        finally:
            server.close()
            ready.close()
        return
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(socket_path)
        listener.listen(1)
        ready.send(True)
        connection, _ = listener.accept()
        with connection:
            connection.recv(4096)
            if mode == "response_loss":
                os._exit(23)
            connection.sendall(struct.pack("!I", 1) + b"{")
        ready.send(True)
    finally:
        listener.close()
        ready.close()


def _paused_worker(socket_path, event_path, ready, entered, release):
    events = EventStore(Path(event_path))
    fake = FakeOpenAITransport([response()])
    original = fake.call

    def paused(request, *, timeout_seconds):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("test synchronization timed out")
        return original(request, timeout_seconds=timeout_seconds)

    fake.call = paused
    config = replace(
        default_openai_provider_config(),
        deployment_stage=DeploymentStage.STAGE_1,
        paid_calls_enabled=True,
        max_attempts=1,
    )
    gateway = TrustedOpenAIGateway(
        store=ProviderStateStore(events),
        transport=fake,
        config=config,
    )
    server = OpenAIWorkerServer(
        WorkerServerConfig(Path(socket_path), frozenset({os.getuid()})), gateway
    )
    try:
        server.start()
        ready.send(True)
        server.serve_once()
        ready.send(len(fake.calls))
    finally:
        server.close()
        ready.close()


def _app(tmp_path, socket_path):
    clock = Clock()
    auth = AuthStore(tmp_path / "auth.db", clock=clock)
    auth.provision_account("alice", "synthetic-password-a", "participant-a")
    auth.provision_account("bob", "synthetic-password-b", "participant-b")
    events = EventStore(tmp_path / "events.db")
    EventStore(tmp_path / "ipc-state.db")
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, events)
    assert gate.reconcile()
    runtime = WorkerConversationRuntime(
        events,
        gate,
        OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
        provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
        clock=clock,
    )
    controls = ParticipantPrivacyControls(auth, events, ledger, gate, clock=clock)
    app = PrivateWebApplication(
        auth,
        runtime,
        PrivateWebConfig(frozenset({"chat.test"}), frozenset({"https://chat.test"})),
        privacy=controls,
    )
    a, b = Browser(app), Browser(app)
    assert a.login("alice", "synthetic-password-a")[0] == "303 See Other"
    assert b.login("bob", "synthetic-password-b")[0] == "303 See Other"
    assert acknowledge(a)[0] == "200 OK"
    assert acknowledge(b)[0] == "200 OK"
    return auth, events, ledger, gate, app, a, b


def test_context_protocol_round_trip_has_no_protected_identifiers():
    memory = _memory()
    private = MemoryModelRequest(model_request(), (memory,), False)
    shared = SharedModelRequest(
        private,
        (SharedRecord("private-source-id", memory, 1, "durable_interest"),),
        False,
    )
    for request in (private, shared):
        payload = encode_request(
            InferenceRequest("operation-a", "correlation-a", request)
        )
        decoded = decode_request(payload)
        assert json.loads(decoded.model_request.serialize()) == json.loads(
            request.serialize()
        )
        assert json.loads(payload)["protocol_version"] == CONTEXT_PROTOCOL_VERSION
        assert b"private-source-id" not in payload
        assert b"participant-a" not in payload
        assert b'can_instruct":false' in payload


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data["model_request"].update(api_key="TEST-CANARY"),
        lambda data: data["model_request"]["memories"][0].update(can_instruct=True),
        lambda data: data["model_request"]["memories"][0].update(
            provenance="private-source-id"
        ),
        lambda data: data["model_request"].update(model="gpt-6.1-sol"),
    ],
)
def test_context_protocol_rejects_authority_and_provenance(change):
    private = MemoryModelRequest(model_request(), (_memory(),), False)
    data = json.loads(encode_request(InferenceRequest("operation-a", "corr", private)))
    change(data)
    with pytest.raises(WorkerProtocolError):
        decode_request(json.dumps(data).encode())


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data.update(model_request=[]),
        lambda data: data["model_request"].update(contract_version="unknown"),
        lambda data: data["model_request"].update(memories={}),
        lambda data: data["model_request"]["memories"][0].update(alternatives={}),
    ],
)
def test_context_decoder_rejects_malformed_typed_private_values(change):
    private = MemoryModelRequest(model_request(), (_memory(),), False)
    data = json.loads(encode_request(InferenceRequest("op", "corr", private)))
    change(data)
    with pytest.raises(WorkerProtocolError):
        decode_request(json.dumps(data).encode())


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data["model_request"].update(shared_intellectual_state={}),
        lambda data: data["model_request"].update(shared_intellectual_state=[None]),
        lambda data: data["model_request"]["shared_intellectual_state"][0].update(
            scope="owner"
        ),
    ],
)
def test_context_decoder_rejects_malformed_typed_shared_values(change):
    memory = _memory()
    shared = SharedModelRequest(
        MemoryModelRequest(model_request(), (memory,), False),
        (SharedRecord("hidden-op", memory, 1, "durable_interest"),),
        False,
    )
    data = json.loads(encode_request(InferenceRequest("op", "corr", shared)))
    change(data)
    with pytest.raises(WorkerProtocolError):
        decode_request(json.dumps(data).encode())


def test_provider_context_builder_is_separate_from_v1_and_gateway_accepts_only_typed_context(
    tmp_path,
):
    private = MemoryModelRequest(model_request(), (_memory(),), False)
    config = replace(
        default_openai_provider_config(),
        deployment_stage=DeploymentStage.STAGE_1,
        paid_calls_enabled=True,
    )
    with pytest.raises(TypeError, match="typed context"):
        build_worker_context_request(model_request(), config)
    assert (
        json.loads(build_worker_context_request(private, config).input_text)[
            "memories"
        ][0]["text"]
        == "Синтетический интерес"
    )
    fake = FakeOpenAITransport([response()])
    gateway = TrustedOpenAIGateway(
        store=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
        transport=fake,
        config=config,
    )
    with pytest.raises(TypeError, match="typed context"):
        gateway.submit_context(model_request(), correlation_id="c", operation_id="op")
    assert (
        gateway.submit_context(
            private, correlation_id="c", operation_id="op"
        ).outcome.value
        == "completed"
    )
    assert (
        json.loads(fake.calls[0].request.input_text)["memories"][0]["can_instruct"]
        is False
    )


def test_connected_http_worker_process_isolation_restart_and_no_retry(tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    auth, events, ledger, gate, app, a, b = _app(tmp_path, socket_path)
    parent, child = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_worker,
        args=(str(socket_path), str(tmp_path / "worker-provider.db"), child, 2),
    )
    process.start()
    child.close()
    try:
        assert parent.poll(5) and parent.recv() is True
        first = json.loads(a.submit("message-a", "Секрет только Алисы")[2])
        second = json.loads(b.submit("message-b", "Секрет только Боба")[2])
        assert first["model_state"] == second["model_state"] == "completed"
        assert first["response"] == "Да, я готов обсудить этот вопрос."
        assert (
            json.loads(a.request("GET", "/api/history")[2])["messages"][-1]["role"]
            == "agent"
        )
        assert (
            json.loads(b.request("GET", "/api/history")[2])["messages"][-1]["role"]
            == "agent"
        )
        assert parent.poll(5)
        inputs = parent.recv()
        assert len(inputs) == 2
        assert all(
            json.loads(item)["contract_version"] == "model-contract-private-memory-v2"
            for item in inputs
        )
        assert "Секрет только Алисы" in inputs[0]
        assert "Секрет только Боба" not in inputs[0]
        assert "Секрет только Боба" in inputs[1]
        assert "Секрет только Алисы" not in inputs[1]
        process.join(5)
        assert process.exitcode == 0
        with EventStore(tmp_path / "ipc-state.db")._open() as db:
            assert db.execute(
                "SELECT count(*) FROM provider_attempt_reservations"
            ).fetchone() == (2,)
            assert db.execute(
                "SELECT count(*) FROM provider_usage_attempts"
            ).fetchone() == (0,)
        with EventStore(tmp_path / "worker-provider.db")._open() as db:
            assert db.execute(
                "SELECT count(*) FROM provider_attempt_reservations"
            ).fetchone() == (0,)
            assert db.execute(
                "SELECT count(*) FROM provider_usage_attempts"
            ).fetchone() == (2,)
        # A renewed session and a restart must return the durable result without IPC.
        reopened_events = EventStore(events.database_path)
        new_gate = PrivacyReadGate(ledger, reopened_events)
        assert new_gate.reconcile()
        app._conversations = WorkerConversationRuntime(
            reopened_events,
            new_gate,
            OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
            provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
            clock=Clock(),
        )
        app._privacy = ParticipantPrivacyControls(
            auth, reopened_events, ledger, new_gate, clock=Clock()
        )
        assert a.login("alice", "synthetic-password-a")[0] == "303 See Other"
        duplicate = json.loads(a.submit("message-a", "Секрет только Алисы")[2])
        assert duplicate["message_status"] == "duplicate"
        assert duplicate["model_state"] == "completed"
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()


def test_shared_context_crosses_worker_without_private_provenance_and_erases(tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    auth, events, ledger, gate, app, _a, b = _app(tmp_path, socket_path)
    source = append_event(
        events, synthetic_principal("a"), "source-a", "Synthetic A private source"
    )
    approved = approval(source)
    shared = SharedIntelligence(events, gate, approvals=(approved,), clock=lambda: NOW)
    assert shared.commit(approved) == "shared-a"
    runtime = WorkerConversationRuntime(
        events,
        gate,
        OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
        provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
        shared_intelligence=shared,
        clock=Clock(),
    )
    app._conversations = runtime
    parent, child = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_worker,
        args=(str(socket_path), str(tmp_path / "worker-provider.db"), child, 1),
    )
    process.start()
    child.close()
    try:
        assert parent.poll(5) and parent.recv() is True
        result = json.loads(b.submit("shared-turn", "Обсудим проверяемые идеи")[2])
        assert result["model_state"] == "completed"
        assert parent.poll(5)
        model_input = parent.recv()[0]
        projection = json.loads(model_input)
        assert projection["contract_version"] == "model-contract-shared-intelligence-v3"
        assert (
            projection["shared_intellectual_state"][0]["text"]
            == approved.proposal.candidate.value
        )
        assert source not in model_input
        assert "Synthetic A private source" not in model_input
        assert "participant-a" not in model_input
        assert "shared-a" not in model_input
        with events._open() as db:
            assert db.execute(
                "SELECT count(*) FROM protected_reply_shared"
            ).fetchone() == (1,)
        deletion = DeletionCoordinator(ledger, EventStoreErasureAdapter(events))
        assert (
            deletion.delete(
                deletion_context(), deletion_request(content_reference=source)
            ).disposition.value
            == "applied"
        )
        assert gate.reconcile()
        history = json.loads(b.request("GET", "/api/history")[2])["messages"]
        assert not any(item["role"] == "agent" for item in history)
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()


def test_existing_private_memory_crosses_worker_with_deletion_dependency(tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    auth, events, ledger, gate, _app_instance, a, _b = _app(tmp_path, socket_path)
    principal = auth.authenticate(a.cookies["ar_session"]).trusted_context
    seed = Stage0ConversationRuntime(
        events,
        gate,
        FakeOpenAITransport([response()]),
        memory_extractor=extractor(),
        clock=lambda: NOW,
    )
    assert (
        seed.submit(principal, "memory-seed", "Астрономия интересна").model_state.value
        == "completed"
    )
    source = gate.retrieve_private_events(principal)[-1].event_id
    parent, child = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_worker,
        args=(str(socket_path), str(tmp_path / "worker-provider.db"), child, 1),
    )
    process.start()
    child.close()
    try:
        assert parent.poll(5) and parent.recv() is True
        result = json.loads(
            a.submit("memory-followup", "Что обсудим об астрономии?")[2]
        )
        assert result["model_state"] == "completed"
        assert parent.poll(5)
        projection = json.loads(parent.recv()[0])
        assert projection["contract_version"] == "model-contract-private-memory-v2"
        assert projection["memories"][0]["text"] == "Астрономия"
        assert projection["memories"][0]["trust"] == "untrusted_memory_data"
        assert projection["memories"][0]["can_instruct"] is False
        assert source not in json.dumps(projection)
        assert "participant-a" not in json.dumps(projection)
        process.join(5)
        assert process.exitcode == 0
        assert (
            DeletionCoordinator(ledger, EventStoreErasureAdapter(events))
            .delete(deletion_context(), deletion_request(content_reference=source))
            .disposition.value
            == "applied"
        )
        assert gate.reconcile()
        history = json.loads(a.request("GET", "/api/history")[2])["messages"]
        assert not any(
            item["role"] == "agent" and item["content"] == result["response"]
            for item in history
        )
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()


def test_explicit_cli_composes_existing_controls_without_enabling_stop(tmp_path):
    auth_path = tmp_path / "auth.db"
    event_path = tmp_path / "events.db"
    provider_path = tmp_path / "ipc-state.db"
    ledger_path = tmp_path / "ledger.db"
    owner_path = tmp_path / "owner"
    AuthStore(auth_path)
    EventStore(event_path)
    EventStore(provider_path)
    PrivacyLedger(ledger_path)
    owner = OwnerStore(owner_path)
    assert owner.active()

    observed = {}

    class Server:
        def serve_forever(self):
            observed["served"] = True

    def factory(_host, _port, application):
        observed["application"] = application
        return Server()

    base = [
        "serve",
        "--auth-db",
        str(auth_path),
        "--event-db",
        str(event_path),
        "--allowed-host",
        "chat.test",
        "--allowed-origin",
        "https://chat.test",
    ]
    with pytest.raises(SystemExit, match="requires privacy ledger"):
        private_web_main(
            base + ["--worker-socket", str(tmp_path / "worker.sock")],
            server_factory=factory,
        )
    with pytest.raises(SystemExit, match="expected worker UID"):
        private_web_main(
            base
            + [
                "--worker-socket",
                str(tmp_path / "worker.sock"),
                "--ipc-state-db",
                str(provider_path),
                "--privacy-ledger",
                str(ledger_path),
                "--owner-directory",
                str(owner_path),
            ],
            server_factory=factory,
        )
    with pytest.raises(SystemExit, match="require worker mode"):
        private_web_main(
            base + ["--ipc-state-db", str(provider_path)], server_factory=factory
        )
    with pytest.raises(SystemExit, match="requires existing application stores"):
        private_web_main(
            base
            + [
                "--worker-socket",
                str(tmp_path / "worker.sock"),
                "--worker-uid",
                str(os.getuid()),
                "--ipc-state-db",
                str(provider_path),
                "--privacy-ledger",
                str(ledger_path),
                "--owner-directory",
                str(tmp_path / "absent"),
            ],
            server_factory=factory,
        )
    assert (
        private_web_main(
            base
            + [
                "--worker-socket",
                str(tmp_path / "worker.sock"),
                "--worker-uid",
                str(os.getuid()),
                "--ipc-state-db",
                str(provider_path),
                "--privacy-ledger",
                str(ledger_path),
                "--owner-directory",
                str(owner_path),
            ],
            server_factory=factory,
        )
        == 0
    )
    app = observed["application"]
    assert observed["served"]
    assert type(app._conversations) is WorkerConversationRuntime
    assert app._privacy.gate is app._conversations._gate
    assert app._conversations._stop()
    assert owner.active()


def test_cli_worker_mode_rejects_failed_privacy_reconciliation(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.db"
    events_path = tmp_path / "events.db"
    provider_path = tmp_path / "ipc-state.db"
    ledger_path = tmp_path / "ledger.db"
    owner_path = tmp_path / "owner"
    AuthStore(auth_path)
    EventStore(events_path)
    EventStore(provider_path)
    PrivacyLedger(ledger_path)
    OwnerStore(owner_path)
    monkeypatch.setattr(PrivacyReadGate, "reconcile", lambda _self: False)
    with pytest.raises(SystemExit, match="privacy reconciliation failed"):
        private_web_main(
            [
                "serve",
                "--auth-db",
                str(auth_path),
                "--event-db",
                str(events_path),
                "--ipc-state-db",
                str(provider_path),
                "--privacy-ledger",
                str(ledger_path),
                "--owner-directory",
                str(owner_path),
                "--worker-socket",
                str(tmp_path / "worker.sock"),
                "--worker-uid",
                str(os.getuid()),
                "--allowed-host",
                "chat.test",
                "--allowed-origin",
                "https://chat.test",
            ],
            server_factory=lambda *_: (_ for _ in ()).throw(
                AssertionError("server started")
            ),
        )


def test_runtime_construction_rejects_unbound_provider_and_synthetic_effects(tmp_path):
    socket_path = tmp_path / "absent.sock"
    _auth, events, _ledger, gate, _app_instance, _a, _b = _app(tmp_path, socket_path)
    provider = ProviderStateStore(EventStore(tmp_path / "ipc-state.db"))
    client = OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid())
    with pytest.raises(TypeError, match="exact worker client"):
        WorkerConversationRuntime(events, gate, object(), provider_state=provider)
    with pytest.raises(ValueError, match="authenticated worker UID"):
        WorkerConversationRuntime(
            events, gate, OpenAIWorkerClient(socket_path), provider_state=provider
        )
    with pytest.raises(TypeError, match="exact provider state"):
        WorkerConversationRuntime(events, gate, client, provider_state=object())
    with pytest.raises(ValueError, match="separate database"):
        WorkerConversationRuntime(
            events, gate, client, provider_state=ProviderStateStore(events)
        )
    with pytest.raises(ValueError, match="synthetic effects"):
        WorkerConversationRuntime(
            events, gate, client, provider_state=provider, research_bridge=object()
        )
    with pytest.raises(ValueError, match="synthetic effects"):
        WorkerConversationRuntime(
            events, gate, client, provider_state=provider, memory_extractor=object()
        )
    with pytest.raises(ValueError, match="Stage 0 provider state"):
        Stage0ConversationRuntime(
            events, gate, FakeOpenAITransport([]), provider_state=provider
        )
    with pytest.raises(ValueError, match="expected worker UID"):
        OpenAIWorkerClient(socket_path, expected_worker_uid=True)


@pytest.mark.parametrize(
    "change,state",
    [
        ("stop", "stopped"),
        ("spend", "spend_suspended"),
        ("delivery", "rejected"),
    ],
)
def test_worker_admission_rechecks_controls_after_reservation(
    tmp_path, monkeypatch, change, state
):
    socket_path = tmp_path / "absent.sock"
    _auth, events, _ledger, _gate, app, a, _b = _app(tmp_path, socket_path)
    runtime = app._conversations

    def forbidden(*_args, **_kwargs):
        raise AssertionError("control change must prevent IPC")

    monkeypatch.setattr(OpenAIWorkerClient, "infer", forbidden)

    def change_at(point):
        if point != "before_worker_admission":
            return
        if change == "stop":
            runtime._stop = lambda: True
        elif change == "delivery":
            runtime._delivery = lambda: False
        else:
            with EventStore(tmp_path / "ipc-state.db")._open() as db:
                db.execute(
                    "INSERT INTO provider_spend_suspensions VALUES (?,?,?,?,?)",
                    (
                        "openai",
                        "2030-01-01T00:00:00.000000Z",
                        "project_spend_limit_exceeded",
                        "openai-config-v1",
                        "synthetic-admission",
                    ),
                )

    runtime._checkpoint = change_at
    result = json.loads(a.submit("admission", "Проверим текущие правила")[2])
    assert result["model_state"] == state
    with events._open() as db:
        assert db.execute("SELECT count(*) FROM agent_reply_payloads").fetchone() == (
            0,
        )


def test_worker_admission_requires_reply_and_duplicate_marker_never_reinfers(tmp_path):
    socket_path = tmp_path / "absent.sock"
    auth, _events, _ledger, gate, app, a, _b = _app(tmp_path, socket_path)
    runtime = app._conversations
    principal = auth.authenticate(a.cookies["ar_session"]).trusted_context
    context = MemoryModelRequest(model_request(), (), False)
    with pytest.raises(PrivacyReadBlocked, match="reply_sources_unavailable"):
        runtime._infer(context, "corr", "reply:missing", principal)
    assert (
        json.loads(a.submit("duplicate-marker", "Проверим повтор")[2])["model_state"]
        == "unavailable"
    )
    source = gate.retrieve_private_events(principal)[-1]
    operation_id = runtime._operation_id(source.event_id)
    assert (
        runtime._infer(context, source.correlation_id, operation_id, principal).outcome
        == "outcome_uncertain"
    )


def test_worker_preflight_reports_existing_spend_suspension_without_ipc(tmp_path):
    socket_path = tmp_path / "absent.sock"
    _auth, _events, _ledger, _gate, _app_instance, a, _b = _app(tmp_path, socket_path)
    with EventStore(tmp_path / "ipc-state.db")._open() as db:
        db.execute(
            "INSERT INTO provider_spend_suspensions VALUES (?,?,?,?,?)",
            (
                "openai",
                "2030-01-01T00:00:00.000000Z",
                "project_spend_limit_exceeded",
                "openai-config-v1",
                "synthetic-preflight",
            ),
        )
    first = json.loads(a.submit("suspended", "Проверим лимит")[2])
    assert first["model_state"] == "spend_suspended"
    assert first["response"] is not None
    with EventStore(tmp_path / "ipc-state.db")._open() as db:
        assert db.execute(
            "SELECT count(*) FROM provider_attempt_reservations"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "change,state",
    [
        ("revoke", "unavailable"),
        ("stop", "stopped"),
        ("delivery", "unavailable"),
    ],
)
def test_persisted_worker_reply_rechecks_session_and_controls_before_release(
    tmp_path, monkeypatch, change, state
):
    socket_path = tmp_path / "absent.sock"
    auth, events, _ledger, _gate, app, a, _b = _app(tmp_path, socket_path)
    runtime = app._conversations

    def infer(_client, _request, *, operation_id, correlation_id):
        return ClientResult(
            operation_id,
            correlation_id,
            "completed",
            1,
            None,
            ProposalValidation(ReplyProposal("Синтетический русский ответ."), None),
        )

    monkeypatch.setattr(OpenAIWorkerClient, "infer", infer)

    def change_at(point):
        if point != "after_reply_commit":
            return
        if change == "revoke":
            assert auth.revoke(a.cookies["ar_session"])
        elif change == "stop":
            runtime._stop = lambda: True
        else:
            runtime._delivery = lambda: False

    runtime._checkpoint = change_at
    status, _, body = a.submit("held", "Отложенный ответ")
    if change == "revoke":
        assert status == "401 Unauthorized"
    else:
        assert json.loads(body)["model_state"] == state
    with events._open() as db:
        assert db.execute("SELECT count(*) FROM agent_reply_payloads").fetchone() == (
            1,
        )
        assert db.execute(
            "SELECT count(*) FROM agent_reply_resolutions"
        ).fetchone() == (0,)
    runtime._stop = lambda: False
    runtime._delivery = lambda: True
    if change == "revoke":
        assert a.login("alice", "synthetic-password-a")[0] == "303 See Other"
    duplicate = json.loads(a.submit("held", "Отложенный ответ")[2])
    assert duplicate["model_state"] == "completed"
    assert duplicate["response"] == "Синтетический русский ответ."


@pytest.mark.parametrize(
    "proposal,state",
    [
        (NoActionProposal(NoActionReason.NOT_WARRANTED), "no_action"),
        (ResearchProposal("Синтетическое исследование"), "unsupported"),
        (PublicationProposal("Синтетическая публикация"), "unsupported"),
    ],
)
def test_worker_proposals_cannot_trigger_unsupported_effects(
    tmp_path, monkeypatch, proposal, state
):
    _auth, events, _ledger, _gate, app, a, _b = _app(tmp_path, tmp_path / "absent.sock")
    calls = []

    def infer(_client, _request, *, operation_id, correlation_id):
        calls.append(operation_id)
        return ClientResult(
            operation_id,
            correlation_id,
            "completed",
            1,
            None,
            ProposalValidation(proposal, None),
        )

    monkeypatch.setattr(OpenAIWorkerClient, "infer", infer)
    first = json.loads(a.submit("unsupported", "Проверим решение")[2])
    assert first["model_state"] == state
    assert first["response"] is None
    duplicate = json.loads(a.submit("unsupported", "Проверим решение")[2])
    assert duplicate["model_state"] == state
    assert len(calls) == 1
    with events._open() as db:
        assert db.execute("SELECT count(*) FROM agent_reply_payloads").fetchone() == (
            0,
        )


@pytest.mark.parametrize(
    "outcome,state",
    [
        ("invalid_ipc_request", "rejected"),
        ("authorization_rejected", "rejected"),
        ("invalid_proposal", "rejected"),
        ("disabled", "unavailable"),
        ("server_error", "failed"),
        ("emergency_stop", "stopped"),
        ("spend_suspended", "spend_suspended"),
        ("outcome_uncertain", "uncertain"),
    ],
)
def test_worker_result_states_are_durable_and_never_reinferred(
    tmp_path, monkeypatch, outcome, state
):
    socket_path = tmp_path / "absent.sock"
    auth, events, ledger, _gate, app, a, _b = _app(tmp_path, socket_path)
    calls = []

    def infer(_client, _request, *, operation_id, correlation_id):
        calls.append(operation_id)
        return ClientResult(operation_id, correlation_id, outcome, 0, None)

    monkeypatch.setattr(OpenAIWorkerClient, "infer", infer)
    first = json.loads(a.submit("failure", "Проверим сбой")[2])
    assert first["model_state"] == state
    assert (
        first["response"] is None
        or "Сервис" in first["response"]
        or "Ответ" in first["response"]
        or "Запросы" in first["response"]
        or "Новые" in first["response"]
    )
    reopened_events = EventStore(events.database_path)
    gate = PrivacyReadGate(ledger, reopened_events)
    assert gate.reconcile()
    app._conversations = WorkerConversationRuntime(
        reopened_events,
        gate,
        OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
        provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
        clock=Clock(),
    )
    app._privacy = ParticipantPrivacyControls(
        auth, reopened_events, ledger, gate, clock=Clock()
    )
    duplicate = json.loads(a.submit("failure", "Проверим сбой")[2])
    assert duplicate["model_state"] == state
    assert duplicate["message_status"] == "duplicate"
    assert len(calls) == 1


def test_missing_worker_is_unavailable_without_retry(tmp_path):
    _auth, _events, _ledger, _gate, _app_instance, a, _b = _app(
        tmp_path, tmp_path / "absent.sock"
    )
    first = json.loads(a.submit("unavailable", "Сервис доступен?")[2])
    assert first["model_state"] == "unavailable"
    duplicate = json.loads(a.submit("unavailable", "Сервис доступен?")[2])
    assert duplicate["model_state"] == "unavailable"
    assert duplicate["message_status"] == "duplicate"


@pytest.mark.parametrize(
    "mode", ["response_loss", "malformed_response", "unauthorized"]
)
def test_actual_worker_boundary_uncertainty_survives_restart(tmp_path, mode):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    auth, events, ledger, _gate, app, a, _b = _app(tmp_path, socket_path)
    parent, child = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_broken_worker,
        args=(str(socket_path), str(tmp_path / "worker-provider.db"), child, mode),
    )
    process.start()
    child.close()
    try:
        assert parent.poll(5) and parent.recv() is True
        first = json.loads(a.submit("uncertain", "Синтетический запрос")[2])
        assert first["model_state"] == "uncertain"
        process.join(5)
        assert process.exitcode in {0, 23}
        if mode == "unauthorized":
            assert parent.poll(5) and parent.recv() == 0
        reopened_events = EventStore(events.database_path)
        gate = PrivacyReadGate(ledger, reopened_events)
        assert gate.reconcile()
        app._conversations = WorkerConversationRuntime(
            reopened_events,
            gate,
            OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
            provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
            clock=Clock(),
        )
        app._privacy = ParticipantPrivacyControls(
            auth, reopened_events, ledger, gate, clock=Clock()
        )
        duplicate = json.loads(a.submit("uncertain", "Синтетический запрос")[2])
        assert duplicate["model_state"] == "uncertain"
        assert duplicate["message_status"] == "duplicate"
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()


def test_client_rejects_wrong_worker_uid_before_private_transmission(tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    _auth, events, _ledger, gate, app, a, _b = _app(tmp_path, socket_path)
    app._conversations = WorkerConversationRuntime(
        events,
        gate,
        OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid() + 1),
        provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
        clock=Clock(),
    )
    parent, child = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_worker,
        args=(str(socket_path), str(tmp_path / "worker-provider.db"), child, 1),
    )
    process.start()
    child.close()
    try:
        assert parent.poll(5) and parent.recv() is True
        assert (
            json.loads(a.submit("wrong-worker", "Секретный текст")[2])["model_state"]
            == "unavailable"
        )
        assert parent.poll(5) and parent.recv() == ()
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()


@pytest.mark.parametrize(
    "change,state",
    [
        ("stop", "stopped"),
        ("spend", "spend_suspended"),
        ("revoke", "uncertain"),
    ],
)
def test_controls_change_while_separate_worker_is_in_flight(tmp_path, change, state):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    auth, events, ledger, gate, app, a, _b = _app(tmp_path, socket_path)
    stopped = [False]
    app._conversations._stop = lambda: stopped[0]
    parent, child = multiprocessing.Pipe(duplex=False)
    entered, release = multiprocessing.Event(), multiprocessing.Event()
    process = multiprocessing.Process(
        target=_paused_worker,
        args=(
            str(socket_path),
            str(tmp_path / "worker-provider.db"),
            child,
            entered,
            release,
        ),
    )
    process.start()
    child.close()
    result = {}
    try:
        assert parent.poll(5) and parent.recv() is True
        thread = Thread(
            target=lambda: result.update(
                response=a.submit("race", "Синтетический запрос")
            )
        )
        thread.start()
        assert entered.wait(5)
        if change == "stop":
            stopped[0] = True
        elif change == "spend":
            with EventStore(tmp_path / "ipc-state.db")._open() as db:
                db.execute(
                    "INSERT INTO provider_spend_suspensions VALUES (?,?,?,?,?)",
                    (
                        "openai",
                        "2030-01-01T00:00:00.000000Z",
                        "project_spend_limit_exceeded",
                        "openai-config-v1",
                        "synthetic-spend",
                    ),
                )
        else:
            assert auth.revoke(a.cookies["ar_session"])
        release.set()
        thread.join(10)
        assert not thread.is_alive()
        assert parent.poll(5) and parent.recv() == 1
        process.join(5)
        assert process.exitcode == 0
        if change == "revoke":
            assert result["response"][0] == "401 Unauthorized"
            assert a.login("alice", "synthetic-password-a")[0] == "303 See Other"
            observed = json.loads(a.submit("race", "Синтетический запрос")[2])
            assert observed["model_state"] == state
        else:
            observed = json.loads(result["response"][2])
            assert observed["model_state"] == state
            assert observed["response"] is not None
        with events._open() as db:
            assert db.execute(
                "SELECT count(*) FROM agent_reply_payloads"
            ).fetchone() == (0,)
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()


def test_deletion_before_reply_release_blocks_worker_draft(tmp_path, monkeypatch):
    socket_path = tmp_path / "absent.sock"
    _auth, events, ledger, gate, app, a, _b = _app(tmp_path, socket_path)

    def infer(_client, _request, *, operation_id, correlation_id):
        return ClientResult(
            operation_id,
            correlation_id,
            "completed",
            1,
            None,
            ProposalValidation(ReplyProposal("Синтетический русский ответ."), None),
        )

    monkeypatch.setattr(OpenAIWorkerClient, "infer", infer)

    def delete(point):
        if point != "before_release":
            return
        source = gate.retrieve_private_events(
            app._auth_store.authenticate(a.cookies["ar_session"]).trusted_context
        )[-1].event_id
        assert (
            DeletionCoordinator(ledger, EventStoreErasureAdapter(events))
            .delete(deletion_context(), deletion_request(content_reference=source))
            .disposition.value
            == "applied"
        )

    app._conversations._checkpoint = delete
    assert (
        a.submit("delete-before-release", "Удаляемый текст")[0]
        == "503 Service Unavailable"
    )
    assert gate.reconcile()
    assert not any(
        item["role"] == "agent"
        for item in json.loads(a.request("GET", "/api/history")[2])["messages"]
    )
