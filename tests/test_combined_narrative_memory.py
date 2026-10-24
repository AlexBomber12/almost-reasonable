"""One authenticated conversation carries factual memory and fictional continuity."""

from dataclasses import replace
import json
import os
from threading import Thread

from app.cognition.narrative_store import NarrativeStore
from app.core.context import ContextLimits
from app.core.openai_worker_client import OpenAIWorkerClient
from app.core.provider_contract import DeploymentStage, default_openai_provider_config
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.private_web.runtime import WorkerConversationRuntime
from app.privacy.application import EventStoreErasureAdapter, PrivacyReadGate
from app.privacy.deletion import DeletionCoordinator
from app.privacy.recovery import QuarantinedRestore, create_sqlite_backup
from app.privacy.application import EventStoreRestoreAdapter
from app.trusted.openai_gateway import TrustedOpenAIGateway
from app.trusted.openai_transport import FakeOpenAITransport
from app.trusted.openai_worker_server import OpenAIWorkerServer, WorkerServerConfig
from test_cognition_pipeline import NOW, principal
from test_int19_worker_runtime import _app
from test_openai_gateway import response
from test_privacy_deletion import (
    context as deletion_context,
    request as deletion_request,
)


def _result(value):
    return response(text=json.dumps(value))


def _reply():
    return _result({"proposal": {"decision": "reply", "content": "Synthetic reply."}})


def _generic_no_change():
    return _result(
        {
            "decision": "no_change",
            "handles": [],
            "kind": None,
            "statement": None,
            "confidence": None,
        }
    )


def _typed_no_change():
    return _result(
        {
            "decision": "no_change",
            "handle": None,
            "kind": None,
            "source_role": None,
            "fields": None,
        }
    )


class _ScenarioTransport(FakeOpenAITransport):
    def call(self, request, *, timeout_seconds):
        index = len(self.calls)
        if index == 7:
            data = json.loads(request.input_text)
            self._outcomes[0] = _result(
                {
                    "decision": "revise_memory",
                    "handles": [data["memories"][0]["handle"]],
                    "kind": "candidate_useful_memory",
                    "statement": "Project Azure",
                    "confidence": 0.9,
                }
            )
        elif index == 9:
            data = json.loads(request.input_text)
            self._outcomes[0] = _result(
                {
                    "decision": "revise",
                    "handle": data["episodes"][0]["handle"],
                    "episode": "In the fictional lighthouse, the copper key opened a blue door.",
                }
            )
        return super().call(request, timeout_seconds=timeout_seconds)


def test_http_worker_combines_memory_and_fiction_across_restart_and_deletion(tmp_path):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    _auth, events, ledger, gate, app, a, b = _app(tmp_path, socket_path)
    ipc_path = tmp_path / "ipc.db"

    def runtime(store, privacy):
        return WorkerConversationRuntime(
            store,
            privacy,
            OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
            provider_state=ProviderStateStore(EventStore(ipc_path)),
            memory_extraction_enabled=True,
            memory_evolution_enabled=True,
            typed_memory_enabled=True,
            narrative_enabled=True,
            narrative_permission_current=lambda: True,
            context_limits=ContextLimits(32000, 1),
        )

    app._conversations = runtime(events, gate)
    fake = _ScenarioTransport(
        [
            _result(
                {
                    "decision": "new_memory",
                    "handles": [],
                    "kind": "candidate_useful_memory",
                    "statement": "Project Cobalt",
                    "confidence": 0.8,
                }
            ),
            _result(
                {
                    "decision": "new_memory",
                    "handle": None,
                    "kind": "durable_interest",
                    "source_role": "supporting",
                    "fields": {
                        "topic": "silent gears",
                        "strength": 70,
                        "status": "active",
                        "reason_for_interest": "Design preference",
                    },
                }
            ),
            _reply(),
            _generic_no_change(),
            _typed_no_change(),
            _result(
                {
                    "decision": "create",
                    "handle": None,
                    "episode": "In a fictional lighthouse, I found a copper key.",
                }
            ),
            _reply(),
            # Revision: the transport fills the current generic and episode handles.
            _generic_no_change(),
            _typed_no_change(),
            _generic_no_change(),
            _reply(),
            # Restart query, other participant, and two deletion checks.
            _generic_no_change(),
            _typed_no_change(),
            _reply(),
            _generic_no_change(),
            _typed_no_change(),
            _reply(),
            _generic_no_change(),
            _typed_no_change(),
            _reply(),
            _generic_no_change(),
            _typed_no_change(),
            _reply(),
        ]
    )
    server = OpenAIWorkerServer(
        WorkerServerConfig(socket_path, frozenset({os.getuid()})),
        TrustedOpenAIGateway(
            store=ProviderStateStore(EventStore(tmp_path / "provider.db")),
            transport=fake,
            config=replace(
                default_openai_provider_config(),
                deployment_stage=DeploymentStage.STAGE_1,
                paid_calls_enabled=True,
                max_attempts=1,
            ),
        ),
        evolution_enabled=True,
        typed_enabled=True,
        narrative_enabled=True,
    )
    server.start()
    worker = Thread(target=lambda: server.serve(max_connections=23), daemon=True)
    worker.start()
    try:

        def send(browser, key, content):
            result = json.loads(browser.submit(key, content)[2])
            assert result["model_state"] == "completed"

        send(a, "project", "My current project is Cobalt and I prefer silent gears.")
        send(a, "story", "Tell a fictional story about a lighthouse and copper key.")
        send(
            a,
            "revision",
            "My project is now Azure. Continue the fictional lighthouse story.",
        )
        assert len(NarrativeStore(events).current(principal("a"))) == 1
        assert NarrativeStore(events).current(principal("a"))[0].revision == 2
        reopened = EventStore(events.database_path)
        fresh_gate = PrivacyReadGate(ledger, reopened)
        assert fresh_gate.reconcile()
        app._conversations = runtime(reopened, fresh_gate)
        send(
            a,
            "question",
            "What is my current project, preference, and lighthouse story?",
        )
        request = json.loads(fake.calls[-1].request.input_text)
        assert request["task"] == "private_conversation"
        assert {memory["text"] for memory in request["memories"]} >= {
            "Project Azure",
            "silent gears",
        }
        episode = request["fictional_episodes"][0]
        assert "copper key" in episode["text"]
        assert episode["classification"] == "fictional_autobiography"
        assert episode["trust"] == "untrusted_fiction_data"
        assert episode["can_instruct"] is False
        assert episode["verified_action"] is False
        assert episode["evidence"] is False
        episode_text = episode["text"]
        assert all(
            episode_text not in call.request.input_text
            for call in fake.calls
            if json.loads(call.request.input_text)["task"]
            in {"evolve_private_memory", "evolve_private_typed_memory"}
        )
        before_duplicate = len(fake.calls)
        send(
            a,
            "question",
            "What is my current project, preference, and lighthouse story?",
        )
        assert len(fake.calls) == before_duplicate
        send(b, "other", "What is my project and story?")
        other = json.loads(fake.calls[-1].request.input_text)
        assert other["memories"] == [] and other["fictional_episodes"] == []
        tasks = [json.loads(call.request.input_text)["task"] for call in fake.calls]
        assert tasks == [
            "evolve_private_memory",
            "evolve_private_typed_memory",
            "private_conversation",
            "evolve_private_memory",
            "evolve_private_typed_memory",
            "propose_private_narrative",
            "private_conversation",
            "evolve_private_memory",
            "evolve_private_typed_memory",
            "propose_private_narrative",
            "private_conversation",
            "evolve_private_memory",
            "evolve_private_typed_memory",
            "private_conversation",
            "evolve_private_memory",
            "evolve_private_typed_memory",
            "private_conversation",
        ]
        with reopened._open() as db:
            story_source = db.execute(
                "SELECT event_id FROM event_envelopes WHERE source_message_id='story'"
            ).fetchone()[0]
        backup = tmp_path / "before-deletion.db"
        create_sqlite_backup(reopened.database_path, backup, 13, NOW)
        assert (
            DeletionCoordinator(ledger, EventStoreErasureAdapter(reopened))
            .delete(
                deletion_context(), deletion_request(content_reference=story_source)
            )
            .disposition.value
            == "applied"
        )
        assert fresh_gate.reconcile()
        assert NarrativeStore(reopened).current(principal("a")) == ()
        send(a, "after-deletion", "What is my current project and preference?")
        after = json.loads(fake.calls[-1].request.input_text)
        assert after["fictional_episodes"] == []
        assert {memory["text"] for memory in after["memories"]} >= {
            "Project Azure",
            "silent gears",
        }
        with reopened._open() as db:
            project_source = db.execute(
                "SELECT event_id FROM event_envelopes WHERE source_message_id='project'"
            ).fetchone()[0]
        assert (
            DeletionCoordinator(ledger, EventStoreErasureAdapter(reopened))
            .delete(
                deletion_context(),
                replace(
                    deletion_request(content_reference=project_source),
                    operation_id="delete-operation-002",
                ),
            )
            .disposition.value
            == "applied"
        )
        assert fresh_gate.reconcile()
        send(a, "after-project-deletion", "What is my current project and preference?")
        assert json.loads(fake.calls[-1].request.input_text)["memories"] == []
        assert (
            QuarantinedRestore(ledger, 13, EventStoreRestoreAdapter)
            .restore(backup, tmp_path / "restored.db", NOW)
            .activation_eligible
        )
        assert len(fake.calls) == 23
    finally:
        worker.join(5)
        server.close()
