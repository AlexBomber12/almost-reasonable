"""One authenticated conversation carries factual memory and fictional continuity."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
import sqlite3
from threading import Thread

import pytest

from acceptance.private_combined_journey import run_combined_journey
from acceptance.private_pilot_live import _proposal_failure

from app.cognition.narrative_store import NarrativeStore
from app.core.context import ContextLimits
from app.core.openai_worker_client import OpenAIWorkerClient
from app.core.provider_contract import DeploymentStage, default_openai_provider_config
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.private_web.runtime import WorkerConversationRuntime
from app.private_web.privacy import ParticipantPrivacyControls
from app.private_web.cli import _narrative_permission_current
from app.privacy.application import EventStoreErasureAdapter, PrivacyReadGate
from app.privacy.deletion import DeletionCoordinator
from app.privacy.recovery import QuarantinedRestore, create_sqlite_backup
from app.privacy.application import EventStoreRestoreAdapter
from app.trusted.openai_gateway import TrustedOpenAIGateway
from app.trusted.openai_transport import FakeOpenAITransport
from app.trusted.openai_worker_server import OpenAIWorkerServer, WorkerServerConfig
from app.trusted.private_pilot import PilotAllowance, load_activation
from app.trusted.private_pilot_worker import PilotRequestGate, PilotStop
from test_cognition_pipeline import NOW, principal
from test_int19_worker_runtime import _app
from test_openai_gateway import response
from test_privacy_deletion import (
    context as deletion_context,
    request as deletion_request,
)
from test_int22_pilot import activation_file, _rewrite


def _result(value):
    if "proposal" not in value:
        value = {"proposal": value}
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


class _B03ETransport(FakeOpenAITransport):
    def call(self, request, *, timeout_seconds):
        if len(self.calls) == 9:
            shown = json.loads(request.input_text)["episodes"]
            assert len(shown) == 1
            self._outcomes[0] = _result(
                {
                    "decision": "revise",
                    "handle": shown[0]["handle"],
                    "episode": "На вымышленном маяке я нашёл медный ключ «Искра», которым открыл дверь «Волна».",
                }
            )
        return super().call(request, timeout_seconds=timeout_seconds)


@pytest.mark.parametrize("rejected_recall", [False, True])
def test_b03e_six_turn_http_worker_journey_uses_real_permissions_and_allowance(
    tmp_path,
    rejected_recall,
    monkeypatch,
):
    activation_path, authority = activation_file(tmp_path)
    authority.update(
        version="private-pilot-activation-b03e-v1",
        task="synthetic-private-combined-memory-b03e",
        max_operations=20,
    )
    authority["expires_at"] = (datetime.now(UTC) + timedelta(minutes=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _rewrite(activation_path, authority)
    activation = load_activation(activation_path, allow_memory_acceptance=True)

    def control(path, version, limit):
        path.write_text(
            json.dumps(
                {
                    "version": version,
                    "pilot_id": activation.pilot_id,
                    "max_operations": limit,
                }
            )
        )
        path.chmod(0o400)
        return path

    evolution = control(
        activation_path.parent / "evolution.json",
        "private-memory-evolution-permission-v1",
        20,
    )
    typed = control(
        activation_path.parent / "typed.json", "private-typed-memory-permission-v1", 20
    )
    narrative = control(
        activation_path.parent / "narrative.json", "private-narrative-permission-v2", 20
    )
    app_control = tmp_path / "app-control"
    app_control.mkdir(mode=0o700)
    app_narrative = control(
        app_control / "narrative.json", "private-narrative-permission-v2", 20
    )
    stop_path = activation_path.parent / "stop.json"
    stop_path.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": False})
    )
    stop_path.chmod(0o400)
    stop = PilotStop(stop_path)
    allowance = PilotAllowance.initialize(tmp_path / "allowance.db", activation)
    provider = ProviderStateStore(EventStore(tmp_path / "provider.db"))
    gate = PilotRequestGate(
        activation,
        allowance,
        activation_path=activation_path,
        stop=stop,
        provider_spend_suspended=provider.is_spend_suspended,
        evolution_permission_path=evolution,
        typed_permission_path=typed,
        narrative_permission_path=narrative,
    )
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir(mode=0o700)
    socket_path = worker_dir / "worker.sock"
    auth, events, ledger, privacy, app, browser, _other = _app(tmp_path, socket_path)
    ipc_path = tmp_path / "ipc.db"
    current = {"events": events}

    def runtime(store, privacy_gate):
        return WorkerConversationRuntime(
            store,
            privacy_gate,
            OpenAIWorkerClient(socket_path, expected_worker_uid=os.getuid()),
            provider_state=ProviderStateStore(EventStore(ipc_path)),
            memory_evolution_enabled=True,
            typed_memory_enabled=True,
            narrative_enabled=True,
            narrative_permission_current=lambda: _narrative_permission_current(
                app_narrative, activation.pilot_id
            ),
            emergency_stop=stop.active,
            context_limits=ContextLimits(32000, 1),
        )

    app._conversations = runtime(events, privacy)
    fake = _B03ETransport(
        [
            _result(
                {
                    "decision": "new_memory",
                    "handles": [],
                    "kind": "candidate_useful_memory",
                    "statement": "Проект Кобальт",
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
                        "topic": "Шестерни серии Ладога",
                        "strength": 80,
                        "status": "active",
                        "reason_for_interest": "Синтетическое предпочтение",
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
                    "episode": "На вымышленном маяке я нашёл медный ключ «Искра».",
                }
            ),
            _reply(),
            _generic_no_change(),
            _typed_no_change(),
            _result({"decision": "no_change", "handle": None, "episode": None}),
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
    if rejected_recall:
        # Synthetic provider response: the unavailable live payload is not inferred.
        fake._outcomes[11] = _result(
            {
                "decision": "revise_memory",
                "handles": [],
                "kind": "candidate_useful_memory",
                "statement": "Synthetic recall",
                "confidence": 0.8,
            }
        )
    server = OpenAIWorkerServer(
        WorkerServerConfig(socket_path, frozenset({os.getuid()})),
        TrustedOpenAIGateway(
            store=provider,
            transport=fake,
            config=replace(
                default_openai_provider_config(),
                deployment_stage=DeploymentStage.STAGE_1,
                paid_calls_enabled=True,
                max_attempts=1,
            ),
        ),
        request_guard=gate.authorize,
        evolution_enabled=True,
        evolution_commit_guard=gate.evolution_commit_allowed,
        typed_enabled=True,
        typed_commit_guard=gate.typed_commit_allowed,
        narrative_enabled=True,
        narrative_commit_guard=gate.narrative_commit_allowed,
    )
    server.start()
    worker = Thread(
        target=lambda: server.serve(max_connections=14 if rejected_recall else 20),
        daemon=True,
    )
    worker.start()
    try:

        def send(step, content):
            return json.loads(browser.submit(step, content)[2])

        def restart():
            reopened = EventStore(events.database_path)
            fresh = PrivacyReadGate(ledger, reopened)
            assert fresh.reconcile()
            current["events"] = reopened
            app._conversations = runtime(reopened, fresh)
            app._privacy = ParticipantPrivacyControls(
                auth, reopened, ledger, fresh, clock=app._privacy.clock
            )

        def records():
            return [
                {"purpose": call.request.purpose, "input": call.request.input_text}
                for call in fake.calls
            ]

        def heads():
            return [
                (episode.item_id, episode.revision)
                for episode in NarrativeStore(current["events"]).current(principal("a"))
            ]

        def delete():
            with current["events"]._open() as db:
                source = db.execute(
                    "SELECT event_id FROM event_envelopes WHERE source_message_id=?",
                    ("combined-story",),
                ).fetchone()[0]
            status, _, body = browser.request(
                "POST",
                "/api/privacy/deletions",
                body=json.dumps(
                    {
                        "operation_id": "combined-delete",
                        "source": source,
                        "confirm": "delete",
                    }
                ).encode(),
                content_type="application/json",
                extra_environ={"HTTP_X_CSRF_TOKEN": browser.cookies["ar_csrf"]},
            )
            assert status == "200 OK"
            return json.loads(body)

        if rejected_recall:
            from acceptance.private_combined_journey import MESSAGES, STEPS

            for step, message in zip(STEPS[:3], MESSAGES[:3]):
                assert send(step, message)["model_state"] == "completed"
            restart()
            assert send(STEPS[3], MESSAGES[3])["model_state"] == "completed"
            assert len(fake.calls) == 14
            assert [call.request.purpose for call in fake.calls[-3:]] == [
                "memory_evolution",
                "typed_memory_evolution",
                "narrative_reply",
            ]
            assert allowance.remaining() == 6
            with provider._open() as db:
                rows = db.execute(
                    "SELECT attempt_id, correlation_id, outcome, rejection_reason, "
                    "input_tokens, output_tokens, total_tokens, retry_count, "
                    "estimated_cost, cost_estimate_status, provider_error_code "
                    "FROM provider_usage_attempts ORDER BY started_at, attempt_id"
                ).fetchall()
                reservations = db.execute(
                    "SELECT COUNT(*) FROM provider_attempt_reservations"
                ).fetchone()[0]
            assert len(rows) == 14
            assert reservations == 0
            assert all(row[7] == 0 for row in rows)
            assert all(row[4:7] == (100, 20, 120) for row in rows)
            failure = _proposal_failure(rows, "combined-recall")
            assert failure.step == "combined-recall"
            assert failure.model_state == "invalid_proposal"
            assert failure.operation_family == "evolution"
            assert failure.diagnostic_code == "evolution_invalid_handle"
            assert sum(row[2] == "invalid_proposal" for row in rows) == 1
            assert all(
                row[2] == "completed"
                for row in rows
                if row[0].startswith(("typed:", "reply:"))
            )
            with sqlite3.connect(allowance.path) as db:
                consumed = [
                    item[0] for item in db.execute("SELECT operation_id FROM consumed")
                ]
            from acceptance import private_pilot_live as live

            monkeypatch.setattr(
                live,
                "_usage",
                lambda *_args, **_kwargs: {"attempts": rows, "reservations": []},
            )
            monkeypatch.setattr(
                live,
                "_allowance",
                lambda *_args, **_kwargs: {"maximum": 20, "consumed": consumed},
            )
            expected = []
            with current["events"]._open() as db:
                for index, step in enumerate(STEPS[:4]):
                    source = db.execute(
                        "SELECT event_id, correlation_id FROM event_envelopes "
                        "WHERE source_message_id=?",
                        (step,),
                    ).fetchone()
                    suffix = hashlib.sha256(source[0].encode()).hexdigest()
                    families = ["evolution", "typed"]
                    if index in {1, 2}:
                        families.append("narrative")
                    families.append("reply")
                    expected.extend(
                        (family + ":" + suffix, source[1]) for family in families
                    )
            _, reconciled, remaining = live._reconcile(
                {},
                {"attempts": set(), "rows": {}, "consumed": set(), "maximum": 20},
                expected,
                {
                    "max_aggregate_input_tokens": 10000,
                    "max_aggregate_output_tokens": 10000,
                },
                required=20,
            )
            assert len(reconciled) == 14 and remaining == 6
        else:
            result = run_combined_journey(send, restart, records, heads, delete)
            assert result["technical_journey"] == "passed"
            assert result["human_semantic_review"] == "pending"
            assert allowance.remaining() == 0
            assert len(fake.calls) == 20
    finally:
        stop_path.chmod(0o600)
        stop_path.write_text(
            json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": True})
        )
        stop_path.chmod(0o400)
        worker.join(10)
        server.close()
    assert stop.active()


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
