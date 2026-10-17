"""One offline composition across the private and public authority boundaries."""

from datetime import timedelta
import json
import multiprocessing
import os

import pytest

from acceptance.guards import install
from app.behavior.scheduler import WakeupEvent
from app.core.context import ContextLimits, PrivateContextAssembler
from app.core.openai_worker_client import OpenAIWorkerClient
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
from app.threads.domain import OutboxState
from test_int23_connected import (
    NOW as RESEARCH_NOW,
    _inference_process,
    _research_process,
    _write_approvals,
)
from test_int24_runtime import NOW, run_once, setup
from test_private_web_http import Browser


def _guarded_child(target, *args):
    with pytest.MonkeyPatch.context() as patch:
        install(patch, local_ipc=True)
        target(*args)


def test_private_research_public_draft_stop_and_restart_replay(tmp_path):
    public, public_worker, sdk, http, _ = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Публичный проверяемый черновик."},
    )
    auth = AuthStore(tmp_path / "auth.db", clock=lambda: RESEARCH_NOW)
    auth.provision_account("alice", "synthetic-password-alice", "participant-alice")
    events = EventStore(tmp_path / "events.sqlite")
    gate = PrivacyReadGate(public.ledger, events)
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
                clock=lambda: RESEARCH_NOW,
            ),
            config,
        )
    )
    assert browser.login("alice", "synthetic-password-alice")[0] == "303 See Other"
    canary = "PRIVATE-INT25-RESEARCH-CANARY"
    assert (
        json.loads(browser.submit("private-1", canary)[2])["model_state"]
        == "unavailable"
    )
    principal = auth.authenticate(browser.cookies["ar_session"]).trusted_context
    event = gate.retrieve_private_events(principal)[0]
    operation = WorkerConversationRuntime._operation_id(event.event_id)
    app_path, worker_path = _write_approvals(
        tmp_path, event, principal, operation, canary
    )
    ipc = tmp_path / "ipc"
    ipc.mkdir(mode=0o700)
    context = multiprocessing.get_context("spawn")
    infer_parent, infer_child = context.Pipe()
    research_parent, research_child = context.Pipe()
    infer = context.Process(
        target=_guarded_child,
        args=(
            _inference_process,
            ipc / "infer.sock",
            tmp_path / "infer.db",
            infer_child,
        ),
    )
    research = context.Process(
        target=_guarded_child,
        args=(
            _research_process,
            ipc / "research.sock",
            tmp_path / "research.db",
            worker_path,
            research_child,
        ),
    )
    infer.start()
    research.start()
    infer_child.close()
    research_child.close()
    try:
        assert infer_parent.poll(10) and infer_parent.recv()
        assert research_parent.poll(10) and research_parent.recv()

        def private_runtime():
            bridge = PublicResearchBridge(
                events,
                gate,
                PublicResearchClient(
                    ipc / "research.sock", expected_worker_uid=os.geteuid()
                ),
                enabled=True,
                clock=lambda: RESEARCH_NOW,
                approval_path=app_path,
            )
            bridge.install_approval(
                load_operator_approval(app_path, role="application"), principal
            )
            return WorkerConversationRuntime(
                events,
                gate,
                OpenAIWorkerClient(
                    ipc / "infer.sock", expected_worker_uid=os.geteuid()
                ),
                provider_state=ProviderStateStore(
                    EventStore(tmp_path / "private-provider.db")
                ),
                research_bridge=bridge,
                emergency_stop=public.owner.active,
                clock=lambda: RESEARCH_NOW,
            )

        browser.app = PrivateWebApplication(auth, private_runtime(), config)
        first = json.loads(browser.submit("private-1", canary)[2])
        assert first["model_state"] == "completed"
        assert "unverified" in first["response"]
        assert "https://example.org/paper" in first["response"]

        # Reopen the production composition. The same operation cannot spend a
        # second inference or research grant after the durable reply was released.
        browser.app = PrivateWebApplication(auth, private_runtime(), config)
        assert (
            json.loads(browser.submit("private-1", canary)[2])["response"]
            == first["response"]
        )
        assert (
            json.loads(browser.submit("unapproved-2", "Второй вопрос")[2])[
                "model_state"
            ]
            == "research_denied"
        )
        assert infer_parent.poll(10) and infer_parent.recv() == 2
        assert research_parent.poll(10)
        research_input = research_parent.recv()
        assert len(research_input) == 1
        assert canary not in str(research_input)

        run_once(public, public_worker)
        public_operation = public.wakeup_operation("wake-1")
        assert public.draft(public_operation).state is OutboxState.AWAITING_APPROVAL
        assert http.calls == []
        assert canary not in str(sdk.calls)
        public.approve(public_operation, expires_at=NOW + timedelta(minutes=5))
        assert public.dispatch(public_operation).state is OutboxState.CONFIRMED
        assert canary not in str(http.calls)

        with public.owner._connection() as db:
            db.execute("UPDATE owner_stop SET active=1")
        assert (
            json.loads(browser.submit("private-2", "Новый вопрос")[2])["model_state"]
            == "stopped"
        )
        assert "owner_stop" in public.readiness()
        with pytest.raises(Exception):
            public.run((WakeupEvent("wake-2", NOW, "public initiative"),))
        assert len(sdk.calls) == 1
        assert len(http.calls) == 5
    finally:
        public_worker.close()
        for process in (infer, research):
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
        infer_parent.close()
        research_parent.close()
