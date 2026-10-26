"""Connected, offline reusable public authority through authenticated WSGI and AF_UNIX."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import multiprocessing
import os
from pathlib import Path
import secrets
from types import SimpleNamespace

import pytest

from app.core.context import ContextLimits, PrivateContextAssembler
from app.core.openai_worker_client import OpenAIWorkerClient
from app.core.provider_contract import DeploymentStage, default_openai_provider_config
from app.core.provider_spend import ProviderStateStore
from app.core.public_research_client import PublicResearchClient
from app.core.public_research_profile import (
    load_research_profile,
    provider_configuration,
)
from app.core.store import EventStore
from app.private_web.auth import AuthStore
from app.private_web.conversation import ConversationService
from app.private_web.public_research import PublicResearchBridge
from app.private_web.runtime import WorkerConversationRuntime
from app.private_web.runtime import _without_reviewed_question
from app.private_web.wsgi import PrivateWebApplication, PrivateWebConfig
from app.privacy.application import PrivacyReadGate
from app.privacy.ledger import PrivacyLedger
from app.trusted.openai_gateway import TrustedOpenAIGateway
from app.trusted.openai_transport import FakeOpenAITransport
from app.trusted.openai_worker_server import OpenAIWorkerServer, WorkerServerConfig
from app.trusted.public_research import PublicResearchBoundary, PublicResearchServer
from app.trusted.public_research_transport import PublicResearchTransport
from app.trusted.research_authority_journal import AuthorityJournal
from app.trusted.public_research_worker import build_worker
from test_int20_public_research import SDK, raw_response
from test_private_conversation_runtime import response
from test_private_web_http import Browser
from test_private_web_conversation import principal as make_principal


NOW = datetime(2030, 1, 1, tzinfo=UTC)
QUESTIONS = ("How do deep ocean currents form?", "What causes auroras?")


def _write_profiles(folder, principal, *, aggregate=2, limits=(1, 1)):
    common = {
        "version": "public-research-profile-v1",
        "purpose": "private-conversation-public-research-v1",
        "configuration": "public-research-v1",
        "provider_configuration": provider_configuration(),
        "profile_id": secrets.token_hex(32),
        "authorization": secrets.token_hex(32),
        "application_scope": "test-private-chat",
        "peer_uid": os.geteuid(),
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(days=1)).isoformat(),
        "reviewer": "independent-test-operator",
        "reviewed_at": NOW.isoformat(),
        "aggregate_limit": aggregate,
        "topics": [
            {"id": name, "question": question, "limit": limit}
            for name, question, limit in zip(("ocean", "aurora"), QUESTIONS, limits)
        ],
    }
    application = folder / "application-profile.json"
    worker = folder / "worker-profile.json"
    application.write_text(
        json.dumps(
            {
                **common,
                "role": "application",
                "principal_id": principal.principal_id.value,
                "authenticated_source_ref": principal.authenticated_source.value,
            }
        )
    )
    worker.write_text(json.dumps({**common, "role": "worker"}))
    application.chmod(0o600)
    worker.chmod(0o600)
    return application, worker


def _inference(socket_path, database, ready):
    fake = FakeOpenAITransport(
        [response("research", question) for question in QUESTIONS]
    )
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


def _research(socket_path, database, profile_path, journal_path, ready):
    sdk = SDK([raw_response()])
    boundary = PublicResearchBoundary(
        store=ProviderStateStore(EventStore(Path(database))),
        transport=PublicResearchTransport(sdk),
        enabled=True,
        utcnow=lambda: NOW,
    )
    profile = load_research_profile(profile_path, role="worker")
    boundary.install_profile(
        profile, profile_path=profile_path, journal=AuthorityJournal(Path(journal_path))
    )
    server = PublicResearchServer(
        WorkerServerConfig(Path(socket_path), frozenset({profile.peer_uid})), boundary
    )
    try:
        server.start()
        ready.send(True)
        server.serve(max_connections=1)
        ready.send(sdk.calls)
    finally:
        server.close()
        ready.close()


def test_two_reviewed_questions_survive_worker_restart(tmp_path):
    auth = AuthStore(tmp_path / "auth.db", clock=lambda: NOW)
    auth.provision_account("alice", "synthetic-password-alice", "participant-alice")
    events = EventStore(tmp_path / "events.db")
    gate = PrivacyReadGate(PrivacyLedger(tmp_path / "ledger.db"), events)
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
    inputs = (
        "Пожалуйста, изучи: How do deep ocean currents form? CANARY-OCEAN",
        "Пожалуйста, изучи: What causes auroras? CANARY-AURORA",
    )
    for index, content in enumerate(inputs):
        assert (
            json.loads(browser.submit(f"profile-{index}", content)[2])["model_state"]
            == "unavailable"
        )
    principal = auth.authenticate(browser.cookies["ar_session"]).trusted_context
    app_path, worker_path = _write_profiles(tmp_path, principal)
    journal = tmp_path / "authority.sqlite"
    journal.touch(mode=0o600)
    directory = tmp_path / "ipc"
    directory.mkdir(mode=0o700)
    ctx = multiprocessing.get_context("spawn")
    infer_parent, infer_child = ctx.Pipe()
    infer = ctx.Process(
        target=_inference,
        args=(directory / "infer.sock", tmp_path / "infer.db", infer_child),
    )
    infer.start()
    assert infer_parent.recv()
    bridge = PublicResearchBridge(
        events,
        gate,
        PublicResearchClient(
            directory / "research.sock", expected_worker_uid=os.geteuid()
        ),
        enabled=True,
        clock=lambda: NOW,
        approval_path=app_path,
        application_scope="test-private-chat",
    )
    bridge.install_profile(
        load_research_profile(app_path, role="application"), principal
    )
    runtime = WorkerConversationRuntime(
        events,
        gate,
        OpenAIWorkerClient(directory / "infer.sock", expected_worker_uid=os.geteuid()),
        provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
        research_bridge=bridge,
        clock=lambda: NOW,
    )
    browser.app = PrivateWebApplication(auth, runtime, config)
    calls = []
    try:
        for index, content in enumerate(inputs):
            parent, child = ctx.Pipe()
            research = ctx.Process(
                target=_research,
                args=(
                    directory / "research.sock",
                    tmp_path / "research.db",
                    worker_path,
                    journal,
                    child,
                ),
            )
            research.start()
            try:
                assert parent.recv()
                result = json.loads(browser.submit(f"profile-{index}", content)[2])
                assert result["model_state"] == "completed"
                assert "unverified" in result["response"]
                calls.extend(parent.recv())
            finally:
                research.join(5)
                if research.is_alive():
                    research.terminate()
                    research.join(5)
                parent.close()
        assert infer_parent.recv() == 2
        assert len(calls) == 2
        for call, question in zip(calls, QUESTIONS):
            assert question in call["input"]
            assert "CANARY" not in str(call)
            assert principal.principal_id.value not in str(call)
        restored_bridge = PublicResearchBridge(
            events,
            gate,
            PublicResearchClient(
                directory / "research.sock", expected_worker_uid=os.geteuid()
            ),
            enabled=True,
            clock=lambda: NOW,
            approval_path=app_path,
            application_scope="test-private-chat",
        )
        restored_bridge.install_profile(
            load_research_profile(app_path, role="application"), principal
        )
        browser.app = PrivateWebApplication(
            auth,
            WorkerConversationRuntime(
                events,
                gate,
                OpenAIWorkerClient(
                    directory / "infer.sock", expected_worker_uid=os.geteuid()
                ),
                provider_state=ProviderStateStore(
                    EventStore(tmp_path / "ipc-state.db")
                ),
                research_bridge=restored_bridge,
                clock=lambda: NOW,
            ),
            config,
        )
        assert (
            json.loads(browser.submit("profile-0", inputs[0])[2])["model_state"]
            == "completed"
        )
        history = json.loads(browser.request("GET", "/api/history")[2])
        assert len(history["messages"]) == 4
        replacement = json.loads(app_path.read_text())
        replacement["profile_id"] = secrets.token_hex(32)
        replacement["authorization"] = secrets.token_hex(32)
        replacement["topics"][0]["question"] = "Why do tides change?"
        app_path.write_text(json.dumps(replacement))
        replacement_bridge = PublicResearchBridge(
            events,
            gate,
            PublicResearchClient(
                directory / "research.sock", expected_worker_uid=os.geteuid()
            ),
            enabled=True,
            clock=lambda: NOW,
            approval_path=app_path,
            application_scope="test-private-chat",
        )
        replacement_bridge.install_profile(
            load_research_profile(app_path, role="application"), principal
        )
        browser.app = PrivateWebApplication(
            auth,
            WorkerConversationRuntime(
                events,
                gate,
                OpenAIWorkerClient(
                    directory / "infer.sock", expected_worker_uid=os.geteuid()
                ),
                provider_state=ProviderStateStore(
                    EventStore(tmp_path / "ipc-state.db")
                ),
                research_bridge=replacement_bridge,
                clock=lambda: NOW,
            ),
            config,
        )
        assert (
            json.loads(browser.submit("profile-0", inputs[0])[2])["model_state"]
            == "research_denied"
        )
        assert (
            len(json.loads(browser.request("GET", "/api/history")[2])["messages"]) == 2
        )
    finally:
        infer.join(5)
        if infer.is_alive():
            infer.terminate()
            infer.join(5)
        infer_parent.close()


def _test_profile(tmp_path, *, aggregate=2, limits=(1, 1)):
    principal = SimpleNamespace(
        principal_id=SimpleNamespace(value="test-participant"),
        authenticated_source=SimpleNamespace(value="test-session"),
    )
    app_path, worker_path = _write_profiles(
        tmp_path, principal, aggregate=aggregate, limits=limits
    )
    path = tmp_path / "authority.sqlite"
    path.touch(mode=0o600)
    return app_path, worker_path, path


def test_durable_limits_duplicate_and_concurrent_last_slot(tmp_path):
    _, worker_path, path = _test_profile(tmp_path, aggregate=2, limits=(2, 1))
    profile = load_research_profile(worker_path, role="worker")
    journal = AuthorityJournal(path)
    journal.activate(profile)
    journal.activate(profile)
    assert journal.reserve(profile, "first-operation", "ocean")
    assert not AuthorityJournal(path).reserve(profile, "first-operation", "aurora")
    with ThreadPoolExecutor(max_workers=2) as executor:
        admitted = list(
            executor.map(
                lambda operation: AuthorityJournal(path).reserve(
                    profile, operation, "ocean"
                ),
                ("second-operation", "third-operation"),
            )
        )
    assert admitted.count(True) == 1
    assert not AuthorityJournal(path).reserve(profile, "fourth-operation", "aurora")
    assert not AuthorityJournal(path).reserve(profile, "fifth-operation", "unknown")


def test_replacement_revocation_and_changed_handle_do_not_replenish(tmp_path):
    _, worker_path, path = _test_profile(tmp_path)
    original = load_research_profile(worker_path, role="worker")
    journal = AuthorityJournal(path)
    journal.activate(original)
    assert journal.reserve(original, "first-operation", "ocean")
    raw = json.loads(worker_path.read_text())
    raw["authorization"] = secrets.token_hex(32)
    worker_path.write_text(json.dumps(raw))
    changed = load_research_profile(worker_path, role="worker")
    with pytest.raises(ValueError, match="changed profile"):
        AuthorityJournal(path).activate(changed)
    assert not AuthorityJournal(path).reserve(changed, "second-operation", "aurora")
    raw["profile_id"] = secrets.token_hex(32)
    worker_path.write_text(json.dumps(raw))
    replacement = load_research_profile(worker_path, role="worker")
    AuthorityJournal(path).activate(replacement)
    assert not AuthorityJournal(path).active(original)
    assert not AuthorityJournal(path).reserve(original, "second-operation", "aurora")
    assert not AuthorityJournal(path).reserve(replacement, "first-operation", "aurora")
    assert AuthorityJournal(path).reserve(replacement, "new-operation", "aurora")
    AuthorityJournal(path).revoke(replacement)
    assert not AuthorityJournal(path).active(replacement)
    with pytest.raises(ValueError, match="replayed"):
        AuthorityJournal(path).activate(replacement)


def test_profile_validation_rejects_private_and_forged_catalog(tmp_path):
    app_path, worker_path, _ = _test_profile(tmp_path)
    application = load_research_profile(app_path, role="application")
    worker = load_research_profile(worker_path, role="worker")
    assert application.digest == worker.digest
    assert worker.principal_id is None
    assert worker.topic(["ocean"]) is None
    assert application.request("reply:valid", "ocean").question == QUESTIONS[0]
    with pytest.raises(ValueError, match="unknown reviewed"):
        application.request("reply:valid", QUESTIONS[0] + " for Alice")
    raw = json.loads(worker_path.read_text())
    raw["topics"][0]["question"] = QUESTIONS[0] + " https://private.test"
    worker_path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_research_profile(worker_path, role="worker")


def test_worker_enforces_catalog_peer_revocation_and_expiry(tmp_path):
    _, worker_path, path = _test_profile(tmp_path)
    profile = load_research_profile(worker_path, role="worker")
    sdk = SDK([raw_response()])
    now = [NOW]
    stopped = [False]
    boundary = PublicResearchBoundary(
        store=ProviderStateStore(EventStore(tmp_path / "provider.db")),
        transport=PublicResearchTransport(sdk),
        enabled=True,
        utcnow=lambda: now[0],
        emergency_stop=lambda: stopped[0],
    )
    boundary.install_profile(
        profile, profile_path=worker_path, journal=AuthorityJournal(path)
    )
    valid = profile.request("first-operation", "ocean")
    assert (
        boundary.execute(valid, peer_uid=os.geteuid() + 1)["outcome"]
        == "authorization_denied"
    )
    from app.trusted.public_research import PublicResearchRequest

    altered = PublicResearchRequest(
        valid.operation_id, valid.question + " for Alice", valid.authorization
    )
    assert (
        boundary.execute(altered, peer_uid=os.geteuid())["outcome"]
        == "authorization_denied"
    )
    stopped[0] = True
    assert boundary.execute(valid, peer_uid=os.geteuid())["outcome"] == "emergency_stop"
    stopped[0] = False
    assert boundary.execute(valid, peer_uid=os.geteuid())["outcome"] == "completed"
    assert len(sdk.calls) == 1
    assert (
        boundary.execute(valid, peer_uid=os.geteuid())["outcome"]
        == "duplicate_operation"
    )
    now[0] = profile.expires_at
    assert (
        boundary.execute(
            profile.request("second-operation", "aurora"), peer_uid=os.geteuid()
        )["outcome"]
        == "authorization_denied"
    )
    now[0] = NOW
    worker_path.unlink()
    assert (
        boundary.execute(
            profile.request("third-operation", "aurora"), peer_uid=os.geteuid()
        )["outcome"]
        == "authorization_denied"
    )
    assert not AuthorityJournal(path).active(profile)


def test_worker_rejects_invalid_or_changed_profile_before_provider_contact(tmp_path):
    from app.research.brief import ResearchScreeningContext

    _, worker_path, path = _test_profile(tmp_path)
    profile = load_research_profile(worker_path, role="worker")
    sdk = SDK([])

    def boundary(*, screening=ResearchScreeningContext(), now=NOW):
        return PublicResearchBoundary(
            store=ProviderStateStore(EventStore(tmp_path / "provider.db")),
            transport=PublicResearchTransport(sdk),
            enabled=True,
            screening=screening,
            utcnow=lambda: now,
        )

    worker = boundary()
    with pytest.raises(TypeError, match="worker profile"):
        worker.install_profile(
            None, profile_path=worker_path, journal=AuthorityJournal(path)
        )
    expired = boundary(now=profile.expires_at)
    with pytest.raises(ValueError, match="expired"):
        expired.install_profile(
            profile, profile_path=worker_path, journal=AuthorityJournal(path)
        )
    screened = boundary(
        screening=ResearchScreeningContext(private_canaries=(QUESTIONS[0],))
    )
    with pytest.raises(ValueError, match="question declined"):
        screened.install_profile(
            profile, profile_path=worker_path, journal=AuthorityJournal(path)
        )
    worker.install_profile(
        profile, profile_path=worker_path, journal=AuthorityJournal(path)
    )
    with pytest.raises(ValueError, match="cannot be combined"):
        worker.install_profile(
            profile, profile_path=worker_path, journal=AuthorityJournal(path)
        )
    changed = json.loads(worker_path.read_text())
    changed["reviewer"] = "changed-test-operator"
    worker_path.write_text(json.dumps(changed))
    outcome = worker.execute(
        profile.request("changed-operation", "ocean"), peer_uid=os.geteuid()
    )
    assert outcome["outcome"] == "authorization_denied"
    assert not sdk.calls
    assert not AuthorityJournal(path).active(profile)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "unknown"),
        ("role", "application"),
        ("purpose", "other"),
        ("configuration", "other"),
        ("provider_configuration", {}),
        ("provider_configuration", []),
        ("provider_configuration", {**provider_configuration(), "enabled": 1}),
        ("profile_id", "not-hex"),
        ("authorization", "not-hex"),
        ("application_scope", "invalid scope"),
        ("peer_uid", -1),
        ("reviewer", " "),
        ("aggregate_limit", 0),
        ("issued_at", "2030-01-02T00:00:00+00:00"),
        ("expires_at", "2030-02-02T00:00:00+00:00"),
        ("reviewed_at", "2030-01-02T00:00:00+00:00"),
        ("topics", []),
        ("topics", [{"id": "ocean", "question": QUESTIONS[0], "limit": 0}]),
        (
            "topics",
            [
                {"id": "ocean", "question": QUESTIONS[0], "limit": 1},
                {"id": "ocean", "question": QUESTIONS[1], "limit": 1},
            ],
        ),
        (
            "topics",
            [
                {"id": "ocean", "question": QUESTIONS[0], "limit": 1},
                {"id": "aurora", "question": QUESTIONS[0], "limit": 1},
            ],
        ),
        (
            "topics",
            [
                {"id": "ocean", "question": "aurora", "limit": 1},
                {"id": "aurora", "question": QUESTIONS[1], "limit": 1},
            ],
        ),
        ("topics", [{"id": "ocean", "question": QUESTIONS[0], "limit": 1, "extra": 1}]),
    ],
)
def test_profile_rejects_changed_contract_fields(tmp_path, field, value):
    _, worker_path, _ = _test_profile(tmp_path)
    raw = json.loads(worker_path.read_text())
    raw[field] = value
    worker_path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_research_profile(worker_path, role="worker")


def test_profile_rejects_unsafe_file_and_private_binding(tmp_path):
    app_path, worker_path, _ = _test_profile(tmp_path)
    with pytest.raises(ValueError):
        load_research_profile(worker_path, role="invalid")
    with pytest.raises(ValueError):
        load_research_profile(Path("relative.json"), role="worker")
    linked = tmp_path / "linked-profile.json"
    linked.symlink_to(worker_path)
    with pytest.raises(ValueError):
        load_research_profile(linked, role="worker")
    worker_path.chmod(0o644)
    with pytest.raises(ValueError):
        load_research_profile(worker_path, role="worker")
    worker_path.chmod(0o600)
    worker_path.write_text(
        worker_path.read_text().replace(
            '"version":', '"version": "duplicate", "version":', 1
        )
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_research_profile(worker_path, role="worker")
    raw_worker = json.loads(
        worker_path.read_text().replace('"version": "duplicate", ', "")
    )
    raw_worker["unexpected"] = True
    worker_path.write_text(json.dumps(raw_worker))
    with pytest.raises(ValueError, match="fields"):
        load_research_profile(worker_path, role="worker")
    raw = json.loads(app_path.read_text())
    raw["principal_id"] = ""
    app_path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="private profile"):
        load_research_profile(app_path, role="application")


def test_journal_rejects_unsafe_path_and_tampered_schema(tmp_path):
    _, _, path = _test_profile(tmp_path)
    with pytest.raises(ValueError):
        AuthorityJournal(Path("relative.sqlite"))
    link = tmp_path / "linked.sqlite"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        AuthorityJournal(link)
    path.chmod(0o644)
    with pytest.raises(ValueError):
        AuthorityJournal(path)
    path.chmod(0o600)
    journal = AuthorityJournal(path)
    assert journal is not None
    import sqlite3

    with sqlite3.connect(path) as db:
        db.execute("UPDATE schema_metadata SET version='wrong'")
    with pytest.raises(ValueError, match="schema"):
        AuthorityJournal(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE schema_metadata SET version='public-research-journal-v1'")
        db.execute("CREATE TABLE injected (value TEXT)")
    with pytest.raises(ValueError, match="schema"):
        AuthorityJournal(path)


def test_journal_rejects_runtime_file_replacement(tmp_path):
    _, worker_path, path = _test_profile(tmp_path)
    profile = load_research_profile(worker_path, role="worker")
    journal = AuthorityJournal(path)
    journal.activate(profile)
    path.unlink()
    path.touch(mode=0o600)
    with pytest.raises(RuntimeError, match="identity changed"):
        journal.reserve(profile, "first-operation", "ocean")


def test_journal_rejects_replacement_during_initial_open(tmp_path, monkeypatch):
    _, _, path = _test_profile(tmp_path)
    import app.trusted.research_authority_journal as module

    original_open = module.os.open
    old_descriptor = original_open(path, os.O_RDONLY)

    def replace_at_open(open_path, flags, *args, **kwargs):
        if Path(open_path) == path:
            path.unlink()
            os.close(original_open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600))
        return original_open(open_path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", replace_at_open)
    try:
        with pytest.raises(ValueError, match="identity changed"):
            AuthorityJournal(path)
    finally:
        os.close(old_descriptor)


def test_profile_rejects_growth_between_stat_and_read(tmp_path, monkeypatch):
    _, worker_path, _ = _test_profile(tmp_path)
    import app.core.public_research_profile as profile_module

    real_read = profile_module.os.read
    monkeypatch.setattr(
        profile_module.os,
        "read",
        lambda fd, count: real_read(fd, count) + b" " * 16_385,
    )
    with pytest.raises(ValueError, match="oversized profile"):
        load_research_profile(worker_path, role="worker")


def test_application_profile_binds_current_source_and_scope(tmp_path):
    events = EventStore(tmp_path / "events.db")
    gate = PrivacyReadGate(PrivacyLedger(tmp_path / "ledger.db"), events)
    assert gate.reconcile()
    who = make_principal("a")
    ConversationService(
        events,
        PrivateContextAssembler(events, ContextLimits(32_000, 20)),
        clock=lambda: NOW,
    ).submit(who, "input", "Private CANARY-PROFILE")
    event = gate.retrieve_private_events(who)[0]
    app_path, _ = _write_profiles(tmp_path, who)
    client = PublicResearchClient(
        tmp_path / "unused.sock", expected_worker_uid=os.geteuid()
    )
    bridge = PublicResearchBridge(
        events,
        gate,
        client,
        enabled=True,
        clock=lambda: NOW,
        approval_path=app_path,
        application_scope="test-private-chat",
    )
    profile = load_research_profile(app_path, role="application")
    bridge.install_profile(profile, who)
    operation = SimpleNamespace(
        operation_id=WorkerConversationRuntime._operation_id(event.event_id),
        initiating_event_id=event.event_id,
    )
    assert bridge.permits(who, operation, "ocean")
    assert not bridge.permits(who, operation, QUESTIONS[0] + " for Alice")
    assert not bridge.permits(make_principal("b"), operation, "ocean")
    assert not bridge.permits(
        who,
        SimpleNamespace(operation_id="wrong", initiating_event_id=event.event_id),
        "ocean",
    )
    changed = json.loads(app_path.read_text())
    changed["reviewer"] = "different-test-operator"
    app_path.write_text(json.dumps(changed))
    assert not bridge.permits(who, operation, "ocean")
    changed["reviewer"] = "independent-test-operator"
    app_path.write_text(json.dumps(changed))
    recorded = []
    fake_provider = SimpleNamespace(
        record_attempt=lambda value, **_kwargs: recorded.append(value) or True
    )
    bridge.record(
        fake_provider,
        SimpleNamespace(operation_id=operation.operation_id, core_self_version="v1"),
        "private variation",
        "research_denied",
        NOW,
        require_reservation=False,
    )
    assert recorded[0].model_identifier == "public-research-v1"
    bridge.record(
        fake_provider,
        SimpleNamespace(operation_id=operation.operation_id, core_self_version="v1"),
        "ocean",
        "completed",
        NOW,
        require_reservation=False,
    )
    assert bridge.evidence_choice(recorded[1].model_identifier) == "ocean"
    assert bridge.evidence_choice("research-profile:unknown") is None
    wrong_scope = PublicResearchBridge(
        events,
        gate,
        client,
        enabled=True,
        clock=lambda: NOW,
        approval_path=app_path,
        application_scope="another-app",
    )
    with pytest.raises(ValueError, match="invalid application"):
        wrong_scope.install_profile(profile, who)
    app_path.unlink()
    assert not bridge.permits(who, operation, "ocean")
    from app.trusted.openai_worker_protocol import WorkerProtocolError

    with pytest.raises(WorkerProtocolError, match="profile changed"):
        bridge.execute(operation.operation_id, "ocean")
    bridge.revoke(operation.operation_id)
    assert bridge._profile is None
    assert bridge.evidence_choice(recorded[1].model_identifier) is None


def test_worker_bootstrap_requires_profile_and_durable_journal(tmp_path):
    _, worker_path, journal_path = _test_profile(tmp_path)
    current = datetime.now(UTC)
    profile_data = json.loads(worker_path.read_text())
    profile_data["reviewed_at"] = (current - timedelta(minutes=1)).isoformat()
    profile_data["issued_at"] = (current - timedelta(minutes=1)).isoformat()
    profile_data["expires_at"] = (current + timedelta(minutes=5)).isoformat()
    worker_path.write_text(json.dumps(profile_data))
    provider_path = tmp_path / "provider.sqlite"
    EventStore(provider_path)
    args = {
        "socket_path": tmp_path / "research.sock",
        "provider_database": provider_path,
        "stop_path": tmp_path / "stop.json",
        "project_id": "synthetic-project",
        "organization_id": "synthetic-organization",
        "transport": PublicResearchTransport(SDK([])),
    }
    with pytest.raises(ValueError, match="exactly one"):
        build_worker(**args)
    with pytest.raises(ValueError, match="journal"):
        build_worker(**args, profile_path=worker_path)
    server = build_worker(
        **args, profile_path=worker_path, authority_journal=journal_path
    )
    assert server._boundary._profile is not None


def test_expiry_during_provider_call_suppresses_result_and_consumes_slot(tmp_path):
    _, worker_path, path = _test_profile(tmp_path)
    profile = load_research_profile(worker_path, role="worker")
    now = [NOW]
    sdk = SDK([raw_response()])
    original_create = sdk.create

    def expire_before_result(**kwargs):
        now[0] = profile.expires_at
        return original_create(**kwargs)

    sdk.create = expire_before_result
    boundary = PublicResearchBoundary(
        store=ProviderStateStore(EventStore(tmp_path / "provider.db")),
        transport=PublicResearchTransport(sdk),
        enabled=True,
        utcnow=lambda: now[0],
    )
    boundary.install_profile(
        profile, profile_path=worker_path, journal=AuthorityJournal(path)
    )
    request = profile.request("expiring-operation", "ocean")
    assert (
        boundary.execute(request, peer_uid=os.geteuid())["outcome"]
        == "authorization_denied"
    )
    assert len(sdk.calls) == 1
    now[0] = NOW
    assert (
        boundary.execute(request, peer_uid=os.geteuid())["outcome"]
        == "duplicate_operation"
    )


def test_provider_unknown_outcome_keeps_reserved_capacity(tmp_path):
    _, worker_path, path = _test_profile(tmp_path, aggregate=1, limits=(1, 1))
    profile = load_research_profile(worker_path, role="worker")
    sdk = SDK([RuntimeError("synthetic unknown provider outcome")])
    boundary = PublicResearchBoundary(
        store=ProviderStateStore(EventStore(tmp_path / "provider.db")),
        transport=PublicResearchTransport(sdk),
        enabled=True,
        utcnow=lambda: NOW,
    )
    boundary.install_profile(
        profile, profile_path=worker_path, journal=AuthorityJournal(path)
    )
    result = boundary.execute(
        profile.request("uncertain-operation", "ocean"), peer_uid=os.geteuid()
    )
    assert result["attempts"] == 1
    assert result["outcome"] != "completed"
    assert (
        boundary.execute(
            profile.request("later-operation", "aurora"), peer_uid=os.geteuid()
        )["outcome"]
        == "duplicate_operation"
    )
    assert len(sdk.calls) == 1


def test_application_cli_installs_reviewed_profile(tmp_path):
    from app.private_web.cli import main as private_web_main
    from app.owner_control.store import OwnerStore
    from test_int23_controls import _local

    events, _, who, _, _, _ = _local(tmp_path)
    app_path, _ = _write_profiles(tmp_path, who)
    current = datetime.now(UTC)
    data = json.loads(app_path.read_text())
    data["reviewed_at"] = (current - timedelta(minutes=1)).isoformat()
    data["issued_at"] = (current - timedelta(minutes=1)).isoformat()
    data["expires_at"] = (current + timedelta(minutes=5)).isoformat()
    app_path.write_text(json.dumps(data))
    auth_path = tmp_path / "auth.db"
    ipc_path = tmp_path / "ipc.db"
    owner_path = tmp_path / "owner"
    AuthStore(auth_path)
    EventStore(ipc_path)
    OwnerStore(owner_path)
    observed = {}

    class Server:
        def serve_forever(self):
            observed["served"] = True

    def factory(_host, _port, application, **_options):
        observed["application"] = application
        return Server()

    arguments = [
        "serve",
        "--auth-db",
        str(auth_path),
        "--event-db",
        str(events.database_path),
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
        "--enable-public-research",
        "--public-research-socket",
        str(tmp_path / "research.sock"),
        "--public-research-worker-uid",
        str(os.geteuid()),
        "--public-research-profile",
        str(app_path),
        "--public-research-scope",
        "test-private-chat",
    ]
    assert private_web_main(arguments, server_factory=factory) == 0
    assert observed["served"]
    assert observed["application"]._conversations._research._profile is not None


@pytest.mark.parametrize(
    "private_text",
    [
        "what causes auroras? for Alice",
        "What causes aurorаs? for Alice",
        "WHAT   CAUSES AURORAS? for Alice",
    ],
)
def test_reviewed_question_normalization_preserves_private_variation(private_text):
    from app.research.brief import ResearchScreeningContext, _screen

    residual = _without_reviewed_question(private_text, QUESTIONS[1])
    assert "Alice" in residual
    screening = ResearchScreeningContext(
        private_quotations=(residual,), private_texts=(residual,)
    )
    assert _screen([QUESTIONS[1]], screening) is None
    assert _screen(["for Alice"], screening) is not None


def test_reviewed_question_removal_keeps_unrelated_private_words():
    assert (
        _without_reviewed_question("oceanographer Alice", "ocean")
        == "oceanographer Alice"
    )
    assert _without_reviewed_question("Café? Alice", "Cafe\u0301?").strip() == "Alice"
    assert _without_reviewed_question("x", "") == "x"
    assert (
        _without_reviewed_question("   What causes auroras?   ", QUESTIONS[1]).strip()
        == ""
    )


def test_pilot_stop_contract_selected_explicitly_and_shared(tmp_path):
    from app.trusted.worker_stop import assert_stop
    from app.trusted.private_pilot_worker import PilotStop

    _, worker_path, journal_path = _test_profile(tmp_path)
    current = datetime.now(UTC)
    data = json.loads(worker_path.read_text())
    for field in ("issued_at", "reviewed_at"):
        data[field] = (current - timedelta(minutes=1)).isoformat()
    data["expires_at"] = (current + timedelta(minutes=5)).isoformat()
    worker_path.write_text(json.dumps(data))
    provider = tmp_path / "provider.sqlite"
    EventStore(provider)
    stop = tmp_path / "stop.json"
    stop.write_text('{"version":"private-pilot-emergency-stop-v1","stop":false}')
    stop.chmod(0o400)
    sdk = SDK([raw_response()])
    args = dict(
        socket_path=tmp_path / "research.sock",
        provider_database=provider,
        profile_path=worker_path,
        authority_journal=journal_path,
        project_id="synthetic-project",
        organization_id="synthetic-organization",
        transport=PublicResearchTransport(sdk),
    )
    with pytest.raises(ValueError, match="exactly one Stop"):
        build_worker(**args)
    with pytest.raises(ValueError, match="exactly one Stop"):
        build_worker(**args, stop_path=stop, pilot_stop_path=stop)
    server = build_worker(**args, pilot_stop_path=stop)
    profile = server._boundary._profile
    assert not PilotStop(stop).active()
    assert (
        server._boundary.execute(
            profile.request("first", "ocean"), peer_uid=os.geteuid()
        )["outcome"]
        == "completed"
    )
    assert_stop(stop)
    assert PilotStop(stop).active()
    assert (
        server._boundary.execute(
            profile.request("second", "aurora"), peer_uid=os.geteuid()
        )["outcome"]
        == "emergency_stop"
    )
    assert len(sdk.calls) == 1
    restarted = build_worker(**args, pilot_stop_path=stop)
    assert (
        restarted._boundary.execute(
            profile.request("second", "aurora"), peer_uid=os.geteuid()
        )["outcome"]
        == "emergency_stop"
    )
