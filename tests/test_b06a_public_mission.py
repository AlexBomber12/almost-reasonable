"""Offline two-phase public mission decisions through the real worker and journal."""

from datetime import datetime, timedelta
from contextlib import contextmanager
import json
from threading import Event, Thread
from dataclasses import replace

import pytest

from app.behavior.mission import (
    EvidenceProvenance,
    MISSION_SCHEMA_VERSION,
    MissionEvidenceSource,
    MissionPurpose,
    MissionStateMachine,
    MissionStatus,
    _MISSION_PURPOSE_TEXT,
)
from app.behavior.scheduler import WakeupEvent
from app.behavior.deliberation import DeliberationOutcome, DeliberationResult
from app.behavior.worker_client import PublicBehaviorClient
from app.core.public_behavior_mission import PublicMissionContext
from app.core.public_mission_evolution import PublicMissionEvolution
from app.core.public_behavior import (
    PublicBehaviorRequest,
    PublicMissionChoice,
    parse_public_proposal,
)
from app.core.public_behavior import public_context_digest
from app.core.public_research_client import PublicResearchClient
from app.integration.threads_runtime import ThreadsRuntime
from app.threads.service import PolicyDeniedError
from app.trusted.public_behavior_worker import PublicSDKAdapter
from test_int24_runtime import FakeSDK
from app.threads.domain import InboundKind
from app.threads.domain import InboundEvent
from app.threads.domain import ConversationOutcome
from app.threads.domain import OutboxState
from test_b05b_public_runtime import _attach_fake_research, _profile
from test_int24_runtime import NOW, run_once, setup
import os


def configured(tmp_path, first, *, parent_version=None):
    path = tmp_path / "mission.json"
    path.write_text(
        json.dumps(
            {
                "version": "mission-v1",
                "purpose": "general_inquiry",
                "text": _MISSION_PURPOSE_TEXT[MissionPurpose.GENERAL_INQUIRY],
                "activated_at": (NOW - timedelta(hours=1)).isoformat(),
                "parent_version": parent_version,
            }
        )
    )
    path.chmod(0o600)
    application_profile, _ = _profile(tmp_path)
    return setup(
        tmp_path,
        first,
        mission=PublicMissionContext(path, clock=lambda: NOW + timedelta(days=1)),
        mission_evolution_enabled=True,
        research_client=PublicResearchClient(
            tmp_path / "unused-research.sock", expected_worker_uid=os.geteuid()
        ),
        research_profile_path=application_profile,
        research_scope="public-test",
        research_enabled=True,
    )


def restarted_evolution_runtime(app, tmp_path):
    return ThreadsRuntime(
        tmp_path / "journal.sqlite",
        tmp_path / "scheduler.sqlite",
        tmp_path / "threads.sqlite",
        model=PublicBehaviorClient(tmp_path / "worker.sock", expected_uid=os.getuid()),
        transport=app.simulator,
        policy=app.policy,
        owner=app.owner,
        provider=app.provider,
        ledger=app.ledger,
        clock=app.clock,
        wakeup_policy=app.scheduler.policy,
        operator=app.operator,
        mission=PublicMissionContext(
            app.mission.path, clock=lambda: NOW + timedelta(days=1)
        ),
        mission_evolution_enabled=True,
        research_client=app.research_client,
        research_profile_path=app.research_profile_path,
        research_scope=app.research_scope,
        research_enabled=True,
        enabled=True,
        live_enabled=True,
    )


def test_parented_mission_is_rejected_before_evolution_state_exists(tmp_path):
    decision = {"decision": "no_action", "reason_category": "low_value"}
    with pytest.raises(ValueError, match="requires a trusted root mission"):
        configured(tmp_path, decision, parent_version="earlier-mission")
    assert not (tmp_path / "journal.sqlite").exists()
    mission = PublicMissionContext(
        tmp_path / "mission.json", clock=lambda: NOW + timedelta(days=1)
    )
    _, worker, _, _, _ = setup(tmp_path, decision, mission=mission)
    worker.close()


def test_second_root_snapshot_cannot_persist_parented_evolution_state(
    tmp_path, monkeypatch
):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        with app.journal.transaction() as db:
            db.execute("DROP TABLE public_mission_state")
        root = app.mission.root_mission()
        monkeypatch.setattr(
            app.mission,
            "root_mission",
            lambda: replace(root, parent_version="earlier-mission"),
        )
        with pytest.raises(ValueError, match="trusted root mission"):
            PublicMissionEvolution(app)
        with app.journal.transaction() as db:
            assert (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='public_mission_state'"
                ).fetchone()
                is None
            )
    finally:
        worker.close()


@pytest.mark.parametrize("table_existed", [False, True])
def test_root_replaced_before_binding_rolls_back_initial_evolution_state(
    tmp_path, monkeypatch, table_existed
):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        with app.journal.transaction() as db:
            if table_existed:
                db.execute("DELETE FROM public_mission_state")
            else:
                db.execute("DROP TABLE public_mission_state")
        app.mission = PublicMissionContext(
            app.mission.path, clock=lambda: NOW + timedelta(days=1)
        )
        original_bind = app.mission.bind_evolution

        def replace_root_before_bind(*args, **kwargs):
            root = json.loads(app.mission.path.read_text())
            root["version"] = "mission-v2"
            replacement = tmp_path / "replacement-mission.json"
            replacement.write_text(json.dumps(root))
            replacement.chmod(0o600)
            replacement.replace(app.mission.path)
            return original_bind(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(app.mission, "bind_evolution", replace_root_before_bind)
            with pytest.raises(ValueError):
                PublicMissionEvolution(app)
        with app.journal.transaction() as db:
            assert (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='public_mission_state'"
                ).fetchone()
                is not None
            ) is table_existed
            if table_existed:
                assert (
                    db.execute(
                        "SELECT 1 FROM public_mission_state WHERE id=1"
                    ).fetchone()
                    is None
                )

        restarted = PublicMissionEvolution(app)
        assert restarted.root.version == "mission-v2"
        assert restarted.document().current.version == "mission-v2"
    finally:
        worker.close()


def test_failed_bind_resets_context_and_preserves_existing_state(tmp_path):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        mission = PublicMissionContext(
            app.mission.path, clock=lambda: NOW + timedelta(days=1)
        )
        root = mission.root_mission()
        with app.journal.transaction() as db:
            original = db.execute(
                "SELECT document FROM public_mission_state WHERE id=1"
            ).fetchone()[0]
            db.execute(
                "UPDATE public_mission_state SET document=? WHERE id=1",
                (json.dumps({"schema_version": "mission-store-v2"}),),
            )
        with pytest.raises(ValueError, match="unsupported mission repository schema"):
            mission.bind_evolution(
                app.journal.path, app.mission_evolution.authorized, expected_root=root
            )
        with app.journal.transaction() as db:
            db.execute(
                "UPDATE public_mission_state SET document=? WHERE id=1", (original,)
            )
        mission.bind_evolution(
            app.journal.path, app.mission_evolution.authorized, expected_root=root
        )
        assert mission.snapshot()[1]["version"] == root.version
    finally:
        worker.close()


def test_root_replaced_before_binding_preserves_existing_state(tmp_path, monkeypatch):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        with app.journal.transaction() as db:
            original = db.execute(
                "SELECT document FROM public_mission_state WHERE id=1"
            ).fetchone()[0]
        app.mission = PublicMissionContext(
            app.mission.path, clock=lambda: NOW + timedelta(days=1)
        )
        original_bind = app.mission.bind_evolution

        def replace_root_before_bind(*args, **kwargs):
            root = json.loads(app.mission.path.read_text())
            root["version"] = "mission-v2"
            replacement = tmp_path / "replacement-mission.json"
            replacement.write_text(json.dumps(root))
            replacement.chmod(0o600)
            replacement.replace(app.mission.path)
            return original_bind(*args, **kwargs)

        monkeypatch.setattr(app.mission, "bind_evolution", replace_root_before_bind)
        with pytest.raises(ValueError, match="root changed"):
            PublicMissionEvolution(app)
        with app.journal.transaction() as db:
            assert (
                db.execute(
                    "SELECT document FROM public_mission_state WHERE id=1"
                ).fetchone()[0]
                == original
            )
    finally:
        worker.close()


@pytest.mark.parametrize("schema_version", ["mission-store-v2", None])
def test_public_mission_rejects_unsupported_persisted_schema(tmp_path, schema_version):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        with app.journal.transaction() as db:
            row = db.execute(
                "SELECT document FROM public_mission_state WHERE id=1"
            ).fetchone()
            stored = json.loads(row[0])
            assert stored["schema_version"] == MISSION_SCHEMA_VERSION
            if schema_version is None:
                del stored["schema_version"]
            else:
                stored["schema_version"] = schema_version
                stored["future_metadata"] = {"retain": True}
            encoded = json.dumps(stored)
            db.execute(
                "UPDATE public_mission_state SET document=? WHERE id=1", (encoded,)
            )

        with pytest.raises(ValueError, match="unsupported mission repository schema"):
            app.mission_evolution.document()
        with pytest.raises(ValueError, match="unsupported mission repository schema"):
            restarted_evolution_runtime(app, tmp_path)
        with app.journal.transaction() as db:
            assert (
                db.execute(
                    "SELECT document FROM public_mission_state WHERE id=1"
                ).fetchone()[0]
                == encoded
            )
    finally:
        worker.close()


def decide_inbound(app, worker):
    app.poll(InboundKind.REPLY, correlation_id="poll-mission")
    thread = Thread(target=worker.serve_once)
    thread.start()
    try:
        return app.process_inbound(InboundKind.REPLY)
    finally:
        thread.join(timeout=5)
        assert not thread.is_alive()


def decide_wakeup(app, worker, clock, event_id="wake-2"):
    thread = Thread(target=worker.serve_once)
    thread.start()
    try:
        return app.run((WakeupEvent(event_id, clock.now(), "public reflection"),))
    finally:
        thread.join(timeout=5)
        assert not thread.is_alive()


def research_after_proposal(app, worker, sdk, clock):
    sdk.proposal = {"decision": "research", "topic_id": "ocean"}
    clock.advance(timedelta(minutes=1))
    assert decide_inbound(app, worker)


def test_proposal_waits_for_later_public_confirmation(tmp_path, monkeypatch):
    app, worker, sdk, http, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        record = app.mission_evolution.pending()
        assert record.status is MissionStatus.PENDING
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
        assert http.calls == []
        research_after_proposal(app, worker, sdk, clock)
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "confirm",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        assert (
            app.mission.snapshot()[0] == _MISSION_PURPOSE_TEXT[MissionPurpose.RESEARCH]
        )
        assert app.mission_evolution.pending() is None
        assert (
            sdk.calls[2]["text"]["format"]["name"]
            == "threads_public_mission_proposal_v1"
        )
        assert "pending" in sdk.calls[2]["input"][1]["content"]
    finally:
        worker.close()


def test_rejection_keeps_active_mission(tmp_path, monkeypatch):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "reflection",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "reject",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
        assert (
            app.mission_evolution.document().proposals[0].status
            is MissionStatus.REJECTED
        )
    finally:
        worker.close()


def test_public_instruction_cannot_confirm_without_independent_research(tmp_path):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "dialogue",
        },
    )
    try:
        run_once(app, worker)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "confirm",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        assert decide_inbound(app, worker) == ()
        with app.journal.transaction() as db:
            assert (
                db.execute(
                    "SELECT phase FROM intents ORDER BY rowid DESC LIMIT 1"
                ).fetchone()[0]
                == "rejected"
            )
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
    finally:
        worker.close()


def test_replay_and_duplicate_proposal_are_held(tmp_path):
    app, worker, _, _, _ = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "reflection",
        },
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        payload = app.journal.read(operation)
        with pytest.raises(ValueError, match="not pending"):
            app.mission_evolution.apply(operation, payload)
        duplicate = "duplicate-proposal"
        app.journal.start(duplicate, payload)
        app.journal.finish(duplicate, payload)
        with pytest.raises(ValueError, match="proposal changed"):
            app.mission_evolution.apply(duplicate, payload)
        repeated = {
            **payload,
            "mission_pending_id": app.mission_evolution.pending().proposal.proposal_id,
        }
        _staged_intent(app, "repeated-proposal", repeated)
        with pytest.raises(ValueError, match="already pending"):
            app.mission_evolution.apply("repeated-proposal", repeated)
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
    finally:
        worker.close()


def test_proposal_stores_the_validated_invariant_snapshot(tmp_path, monkeypatch):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        run_once(app, worker)
        baseline = app.mission_evolution.invariants()
        changed = replace(baseline, tool_permissions_version="e" * 64)
        payload = {
            **app.journal.read(app.wakeup_operation("wake-1")),
            "decision": "revise_mission",
            "mission_action": "propose",
            "mission_purpose": "research",
            "event": "wake-1",
            "correlation": "snapshot-test",
        }
        _staged_intent(app, "snapshot-test", payload)
        calls = []

        def changing_invariants():
            calls.append(None)
            return baseline if len(calls) == 1 else changed

        monkeypatch.setattr(app.mission_evolution, "invariants", changing_invariants)
        with pytest.raises(ValueError, match="source changed"):
            app.mission_evolution.apply("snapshot-test", payload)
        assert app.mission_evolution.document().proposals == ()
        monkeypatch.setattr(app.mission_evolution, "invariants", lambda: baseline)
        app.mission_evolution.apply("snapshot-test", payload)
        assert len(calls) == 2
        assert app.mission_evolution.pending().proposal.invariants == baseline
    finally:
        worker.close()


def test_generation_change_inside_mission_transaction_rolls_back(tmp_path, monkeypatch):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        run_once(app, worker)
        payload = {
            **app.journal.read(app.wakeup_operation("wake-1")),
            "decision": "revise_mission",
            "mission_action": "propose",
            "mission_purpose": "research",
            "correlation": "generation-race",
        }
        _staged_intent(app, "generation-race", payload)
        calls = []

        def generation():
            calls.append(None)
            return (
                payload["generation"] if len(calls) <= 2 else payload["generation"] + 1
            )

        with monkeypatch.context() as patch:
            patch.setattr(app.ledger, "control_generation", generation)
            with pytest.raises(ValueError, match="source changed"):
                app.mission_evolution.apply("generation-race", payload)
        assert len(calls) >= 2
        assert app.mission_evolution.document().proposals == ()
    finally:
        worker.close()


def test_stop_change_inside_mission_transaction_rolls_back(tmp_path, monkeypatch):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        run_once(app, worker)
        payload = {
            **app.journal.read(app.wakeup_operation("wake-1")),
            "decision": "revise_mission",
            "mission_action": "propose",
            "mission_purpose": "research",
            "correlation": "stop-race",
        }
        _staged_intent(app, "stop-race", payload)
        original = MissionStateMachine.propose

        def stop_after_save(machine, proposal):
            result = original(machine, proposal)
            app.policy.emergency_stop = True
            return result

        with monkeypatch.context() as patch:
            patch.setattr(MissionStateMachine, "propose", stop_after_save)
            with pytest.raises(PolicyDeniedError):
                app.mission_evolution.apply("stop-race", payload)
        assert app.mission_evolution.document().proposals == ()
    finally:
        worker.close()


def test_permission_change_holds_confirmation(tmp_path, monkeypatch):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "dialogue",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        app.policy.publication_capability_enabled = False
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "confirm",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        assert app.mission_evolution.pending() is not None
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
    finally:
        worker.close()


def test_research_profile_replacement_holds_confirmation(tmp_path, monkeypatch):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        original_digest = app.current_research_profile().digest
        run_once(app, worker)
        profile_path = app.research_profile_path
        replacement = json.loads(profile_path.read_text())
        replacement["authorization"] = "c" * 64
        profile_path.write_text(json.dumps(replacement))
        assert app.current_research_profile().digest != original_digest
        research_after_proposal(app, worker, sdk, clock)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "confirm",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        assert app.mission_evolution.pending() is not None
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
    finally:
        worker.close()


def test_research_worker_replacement_holds_confirmation(tmp_path, monkeypatch):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        original_invariants = app.mission_evolution.pending().proposal.invariants
        original_client = app.research_client
        execute = app.research_client.execute
        replacement = PublicResearchClient(
            tmp_path / "replacement-research.sock", expected_worker_uid=os.geteuid()
        )
        monkeypatch.setattr(replacement, "execute", execute)
        app.research_client = replacement
        assert app.mission_evolution.invariants() != original_invariants
        research_after_proposal(app, worker, sdk, clock)
        app.research_client = original_client
        assert app.mission_evolution.invariants() == original_invariants
        assert app._public_context_with_source(app.current_research_profile())[1:] == (
            (),
            None,
        )
        with app.journal.transaction() as db:
            row = db.execute(
                "SELECT operation,evidence FROM public_research_ops"
            ).fetchone()
        with pytest.raises(ValueError, match="evidence missing"):
            app.mission_evolution._confirmation_sources(
                {
                    "mission_evidence_operation": row["operation"],
                    "mission_evidence_digest": public_context_digest(
                        (row["evidence"],)
                    ),
                    "generation": app.ledger.control_generation(),
                    "decided_at": (clock.now() + timedelta(minutes=1)).isoformat(),
                },
                app.mission_evolution.pending().proposal,
            )
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "confirm",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        assert app.mission_evolution.pending() is not None
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
        assert app.journal.read(app.wakeup_operation("wake-2"))["phase"] == "rejected"
    finally:
        worker.close()


@pytest.mark.parametrize("change", ["before_handoff", "during_execution"])
@pytest.mark.parametrize("control", ["socket", "worker_uid"])
def test_research_intent_requires_its_worker_invariants_at_execution(
    tmp_path, monkeypatch, change, control
):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    try:
        calls = _attach_fake_research(app, tmp_path, monkeypatch)
        original_client = app.research_client
        original_execute = original_client.execute
        replacement = PublicResearchClient(
            tmp_path
            / (
                "replacement-research.sock"
                if control == "socket"
                else "unused-research.sock"
            ),
            expected_worker_uid=os.geteuid() + (control == "worker_uid"),
        )
        monkeypatch.setattr(replacement, "execute", original_execute)

        if change == "before_handoff":
            original_research = app._research

            def research_after_restart(operation, payload):
                app.research_client = replacement
                return original_research(operation, payload)

            monkeypatch.setattr(app, "_research", research_after_restart)
        else:

            def execute_then_change_worker(request):
                result = original_execute(request)
                app.research_client = replacement
                return result

            monkeypatch.setattr(original_client, "execute", execute_then_change_worker)

        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        payload = app.journal.read(operation)
        assert (
            payload["mission_invariants"]
            != app.mission_evolution.invariants().to_dict()
        )
        with app.journal.transaction() as db:
            row = db.execute(
                "SELECT phase,evidence FROM public_research_ops WHERE operation=?",
                (operation,),
            ).fetchone()
        if change == "before_handoff":
            assert calls == []
            assert row is None
        else:
            assert len(calls) == 1
            assert tuple(row) == ("held", None)
    finally:
        worker.close()


def test_research_completion_rechecks_profile_after_journal_wait(tmp_path, monkeypatch):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        original_execute = app.research_client.execute
        original_transaction = app.journal.transaction
        changed = []

        @contextmanager
        def transaction_after_profile_revocation():
            app.journal.transaction = original_transaction
            app.research_enabled = False
            changed.append(None)
            with original_transaction() as db:
                yield db

        def execute_then_wait(request):
            result = original_execute(request)
            app.journal.transaction = transaction_after_profile_revocation
            return result

        monkeypatch.setattr(app.research_client, "execute", execute_then_wait)
        run_once(app, worker)
        assert len(changed) == 1
        with app.journal.transaction() as db:
            row = db.execute(
                "SELECT phase,evidence FROM public_research_ops"
            ).fetchone()
            completions = db.execute(
                "SELECT COUNT(*) FROM public_research_completions"
            ).fetchone()[0]
        assert tuple(row) == ("held", None)
        assert completions == 0
    finally:
        worker.close()


def test_inbound_research_completion_fences_conversation_control(tmp_path, monkeypatch):
    app, worker, _, _, clock = configured(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        original_research = app._research
        original_execute = app.research_client.execute
        original_fence = app.service.store.conversation_control_fence
        research_payload = []
        changed = []

        def capture_research(operation, payload):
            research_payload.append(payload)
            return original_research(operation, payload)

        @contextmanager
        def fence_after_control():
            app.service.store.conversation_control_fence = original_fence
            payload = research_payload[0]
            app.service.store.record_conversation_outcome(
                outcome_id="research-completion-control",
                correlation_id=payload["correlation"],
                account_ref=app.account,
                stream=InboundKind(payload["kind"]),
                inbound_event_id=payload["event"],
                outcome=ConversationOutcome.TERMINATE_CONVERSATION,
                reason="source-withdrawn",
                resume_condition=None,
                now=clock.now(),
            )
            changed.append(None)
            with original_fence():
                yield

        def execute_then_wait(request):
            result = original_execute(request)
            app.service.store.conversation_control_fence = fence_after_control
            return result

        monkeypatch.setattr(app, "_research", capture_research)
        monkeypatch.setattr(app.research_client, "execute", execute_then_wait)
        decide_inbound(app, worker)
        assert len(changed) == 1
        with app.journal.transaction() as db:
            row = db.execute(
                "SELECT phase,evidence FROM public_research_ops"
            ).fetchone()
            completions = db.execute(
                "SELECT COUNT(*) FROM public_research_completions"
            ).fetchone()[0]
        assert tuple(row) == ("held", None)
        assert completions == 0
    finally:
        worker.close()


def test_research_worker_change_during_inference_holds_intent(tmp_path):
    app, worker, _, _, clock = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:

        def checkpoint(point):
            if point == "after_started":
                app.research_client = PublicResearchClient(
                    tmp_path / "replacement-research.sock",
                    expected_worker_uid=os.geteuid(),
                )

        app.checkpoint = checkpoint
        decide_wakeup(app, worker, clock, "wake-1")
        assert app.journal.read(app.wakeup_operation("wake-1"))["phase"] == "held"
        assert app.journal.recoverable() == ()
    finally:
        worker.close()


def test_privacy_generation_change_holds_pending_proposal(tmp_path, monkeypatch):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        admitted = app.ledger.control_generation()
        with monkeypatch.context() as patch:
            patch.setattr(app.ledger, "control_generation", lambda: admitted + 1)
            research_after_proposal(app, worker, sdk, clock)
            sdk.proposal = {
                "decision": "mission",
                "mission_action": "confirm",
                "mission_purpose": None,
            }
            clock.advance(timedelta(minutes=1))
            decide_wakeup(app, worker, clock)
            assert app.mission_evolution.pending() is not None
            assert app.mission.snapshot()[1]["version"] == "mission-v1"
    finally:
        worker.close()


def test_confirmed_mission_survives_restart_and_withdrawal_fails_closed(
    tmp_path, monkeypatch
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "discontinue_optional_channel",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        initial_policy = repr(app.policy)
        initial_spend = app.provider.is_spend_suspended()
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "confirm",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        assert app.mission.publication_allowed() is False
        assert app.policy.publication_capability_enabled is True
        assert repr(app.policy) == initial_policy
        assert app.provider.is_spend_suspended() is initial_spend
        with pytest.raises(ValueError, match="persisted public mission evolution"):
            ThreadsRuntime(
                tmp_path / "journal.sqlite",
                tmp_path / "scheduler.sqlite",
                tmp_path / "threads.sqlite",
                model=PublicBehaviorClient(
                    tmp_path / "disabled-worker.sock", expected_uid=os.getuid()
                ),
                transport=app.simulator,
                policy=app.policy,
                owner=app.owner,
                provider=app.provider,
                ledger=app.ledger,
                clock=app.clock,
                wakeup_policy=app.scheduler.policy,
                operator=app.operator,
                mission=PublicMissionContext(
                    app.mission.path, clock=lambda: NOW + timedelta(days=1)
                ),
                enabled=True,
                live_enabled=True,
            )
        sdk.proposal = {"decision": "no_action", "reason_category": "low_value"}
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock, "wake-3")
        assert app.journal.read(app.wakeup_operation("wake-3"))["phase"] == "linked"
        sdk.proposal = {
            "decision": "propose_publication",
            "draft": "Публичный черновик после отказа от канала.",
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock, "wake-4")
        assert app.journal.read(app.wakeup_operation("wake-4"))["phase"] == "rejected"
        assert app.mission.publication_allowed() is False
        restarted = ThreadsRuntime(
            tmp_path / "journal.sqlite",
            tmp_path / "scheduler.sqlite",
            tmp_path / "threads.sqlite",
            model=PublicBehaviorClient(
                tmp_path / "worker.sock", expected_uid=os.getuid()
            ),
            transport=app.simulator,
            policy=app.policy,
            owner=app.owner,
            provider=app.provider,
            ledger=app.ledger,
            clock=app.clock,
            wakeup_policy=app.scheduler.policy,
            operator=app.operator,
            mission=PublicMissionContext(
                app.mission.path, clock=lambda: NOW + timedelta(days=1)
            ),
            mission_evolution_enabled=True,
            research_client=app.research_client,
            research_profile_path=app.research_profile_path,
            research_scope=app.research_scope,
            research_enabled=True,
            enabled=True,
            live_enabled=True,
        )
        assert (
            restarted.mission.snapshot()[1]["purpose"] == "discontinue_optional_channel"
        )
        with app.journal.transaction() as db:
            db.execute("UPDATE public_research_ops SET phase='held'")
        with pytest.raises(ValueError, match="evidence withdrawn"):
            restarted.mission.snapshot()
        with pytest.raises(ValueError, match="evidence withdrawn"):
            restarted.mission_evolution.document()
    finally:
        worker.close()


def test_confirmed_mission_survives_research_profile_expiry_and_renewal(
    tmp_path, monkeypatch
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "confirm",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        confirmed = app.mission.snapshot()
        clock.advance(timedelta(days=31))
        assert app.current_research_profile() is None
        assert app.mission.snapshot() == confirmed
        expired_restart = restarted_evolution_runtime(app, tmp_path)
        assert expired_restart.mission.snapshot() == confirmed
        assert expired_restart.mission_evolution.model_context() == ()

        path = app.research_profile_path
        renewed = json.loads(path.read_text())
        renewed.update(
            profile_id="c" * 64,
            authorization="d" * 64,
            reviewed_at=clock.now().isoformat(),
            issued_at=clock.now().isoformat(),
            expires_at=(clock.now() + timedelta(days=29)).isoformat(),
        )
        path.write_text(json.dumps(renewed))
        assert app.current_research_profile() is not None
        assert app.mission.snapshot() == confirmed
        restarted = restarted_evolution_runtime(app, tmp_path)
        assert restarted.mission.snapshot() == confirmed
    finally:
        worker.close()


def test_empty_evolution_state_still_requires_active_profile(tmp_path):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        with app.journal.transaction() as db:
            db.execute("DELETE FROM public_mission_state")
        app.research_profile_path.unlink()
        with pytest.raises(ValueError, match="active research profile"):
            restarted_evolution_runtime(app, tmp_path)
    finally:
        worker.close()


def test_no_profile_offered_allows_nonresearch_inference(tmp_path):
    app, worker, sdk, _, clock = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        run_once(app, worker)
        clock.advance(timedelta(days=31))
        assert app.current_research_profile() is None
        assert app.mission_evolution.model_context() == ()
        previous_calls = len(sdk.calls)

        decide_wakeup(app, worker, clock)

        assert app.journal.read(app.wakeup_operation("wake-2"))["phase"] == "linked"
        assert len(sdk.calls) == previous_calls + 1
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
    finally:
        worker.close()


@pytest.mark.parametrize(
    "choice",
    [
        {"decision": "mission", "action": "propose", "purpose": "research"},
        {"decision": "mission", "action": "confirm", "purpose": None},
        {"decision": "mission", "action": "reject", "purpose": None},
        {"decision": "mission", "action": "abandon", "purpose": None},
    ],
)
def test_closed_public_mission_choices(choice):
    assert parse_public_proposal(choice, evolution=True).valid
    assert not parse_public_proposal(choice).valid
    assert not parse_public_proposal(
        choice | {"source_id": "model"}, evolution=True
    ).valid


@pytest.mark.parametrize(
    "choice",
    [
        {"decision": "mission", "action": "confirm", "purpose": "research"},
        {"decision": "mission", "action": "propose", "purpose": "free text"},
        {"decision": "mission", "action": "abandon", "purpose": "research"},
        {"decision": "mission", "action": "change_policy", "purpose": None},
    ],
)
def test_invalid_public_mission_choices(choice):
    assert not parse_public_proposal(choice, evolution=True).valid


@pytest.mark.parametrize(
    "proposal",
    [
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "free text",
        },
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
            "content": "unexpected",
        },
        {
            "decision": "no_action",
            "reason_category": "low_value",
            "mission_action": "confirm",
        },
    ],
)
def test_worker_rejects_model_mission_authority(proposal):
    adapter = PublicSDKAdapter(FakeSDK(proposal), model="fake-model")
    request = PublicBehaviorRequest(
        "operation-1",
        "operation-1",
        "autonomous_wakeup",
        (),
        (),
        5,
        mission_evolution=("enabled",),
    )
    with pytest.raises(ValueError):
        adapter.complete(request)
    with pytest.raises(ValueError, match="mission evolution context"):
        replace(request, mission_evolution=("pending", "free text")).encode()
    with pytest.raises(ValueError, match="research context"):
        replace(request, catalog=(("invalid topic", "question"),)).encode()


def test_unknown_or_copied_research_cannot_authorize_confirmation(tmp_path):
    app, worker, _, _, _ = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        run_once(app, worker)
        evolution = app.mission_evolution
        source = MissionEvidenceSource(
            "unknown", "independent", EvidenceProvenance.AGENT_OBSERVATION
        )
        assert not evolution.authorized("original", ())
        assert not evolution.authorized("independent", (source,))
        assert not evolution.authorized("original", (source,))
        assert not evolution.authorized(
            "original",
            (
                MissionEvidenceSource(
                    "unknown", "independent", EvidenceProvenance.PUBLIC_SOURCE
                ),
            ),
        )
    finally:
        worker.close()


def _staged_intent(app, operation, payload):
    app.journal.start(operation, payload)
    app.journal.finish(operation, payload)


def _staged_abandonment(app, operation, event):
    payload = {
        **app.journal.read(app.wakeup_operation("wake-1")),
        "event": event,
        "correlation": operation,
        "decided_at": app.clock.now().isoformat(),
        "decision": "revise_mission",
        "mission_action": "abandon",
        "mission_purpose": None,
        "mission_pending_id": app.mission_evolution.pending().proposal.proposal_id,
    }
    _staged_intent(app, operation, payload)
    return payload


def test_cas_stale_event_and_stop_hold_mission_transition(tmp_path, monkeypatch):
    app, worker, _, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        run_once(app, worker)
        payload = app.journal.read(app.wakeup_operation("wake-1"))
        stale = {**payload, "mission": {**payload["mission"], "version": "stale"}}
        _staged_intent(app, "stale-parent", stale)
        with monkeypatch.context() as patch:
            patch.setattr(app, "require_mission", lambda _payload: None)
            with pytest.raises(ValueError, match="parent changed"):
                app.mission_evolution.apply("stale-parent", stale)
        withdrawn = {**payload, "generation": payload["generation"] + 1}
        _staged_intent(app, "withdrawn", withdrawn)
        with pytest.raises(ValueError, match="source changed"):
            app.mission_evolution.apply("withdrawn", withdrawn)
        same_time = {
            **payload,
            "mission_action": "confirm",
            "mission_pending_id": app.mission_evolution.pending().proposal.proposal_id,
        }
        _staged_intent(app, "same-time", same_time)
        with pytest.raises(ValueError, match="independent later"):
            app.mission_evolution.apply("same-time", same_time)
        app.policy.emergency_stop = True
        with pytest.raises(PolicyDeniedError):
            app.mission_evolution.apply("same-time", same_time)
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
    finally:
        worker.close()


def test_missing_public_event_cannot_be_mission_source(tmp_path):
    app, worker, _, _, _ = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        with pytest.raises(ValueError, match="source missing"):
            app.mission_evolution._initiating_source(
                "operation", {"kind": "reply", "event": "missing"}
            )
        with app.journal.transaction() as db:
            db.execute("DELETE FROM public_mission_state")
        with pytest.raises(ValueError, match="state missing"):
            app.mission_evolution.document()
        with pytest.raises(ValueError, match="state missing"):
            app.mission.snapshot()
    finally:
        worker.close()


def test_public_event_proposal_binds_registered_content(tmp_path):
    app, worker, _, _, _ = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "dialogue",
        },
    )
    try:
        assert decide_inbound(app, worker)
        event = app.service.store.inbound_event(
            app.account, InboundKind.REPLY, "reply-1"
        )
        assert app.mission_evolution.pending().proposal.initiating_source_id == (
            app.mission_evolution.source_id(event.content)
        )
        with pytest.raises(ValueError, match="already bound"):
            app.mission.bind_evolution(
                app.journal.path, app.mission_evolution.authorized
            )
        root = json.loads(app.mission.path.read_text())
        root["version"] = "operator-changed-root"
        app.mission.path.write_text(json.dumps(root))
        with pytest.raises(ValueError, match="root changed"):
            app.mission.snapshot()
    finally:
        worker.close()


def test_mission_root_replaced_while_commit_waits_cannot_link(tmp_path, monkeypatch):
    app, worker, _, _, _ = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "dialogue",
        },
    )
    root_path = app.mission.path
    original_root = root_path.read_text()
    replacement = json.loads(original_root)
    replacement["version"] = "replacement-root"
    replacement_path = tmp_path / "replacement-root.json"
    replacement_path.write_text(json.dumps(replacement))
    replacement_path.chmod(0o600)
    original_apply = app.mission_evolution._apply
    original_transaction = app.journal.transaction
    changed = []

    def apply_with_root_swap(operation, payload):
        @contextmanager
        def transaction_after_root_swap():
            app.journal.transaction = original_transaction
            os.replace(replacement_path, root_path)
            changed.append(None)
            with original_transaction() as db:
                yield db

        app.journal.transaction = transaction_after_root_swap
        try:
            return original_apply(operation, payload)
        finally:
            app.journal.transaction = original_transaction

    monkeypatch.setattr(app.mission_evolution, "_apply", apply_with_root_swap)
    try:
        try:
            run_once(app, worker)
        except PolicyDeniedError:
            pass
        assert len(changed) == 1
    finally:
        restored = tmp_path / "restored-root.json"
        restored.write_text(original_root)
        restored.chmod(0o600)
        os.replace(restored, root_path)
        worker.close()
    assert app.journal.read(app.wakeup_operation("wake-1"))["phase"] != "linked"
    assert app.mission_evolution.document().proposals == ()


def test_invalid_research_provenance_holds_confirmation(tmp_path, monkeypatch):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        with app.journal.transaction() as db:
            row = db.execute(
                "SELECT operation,evidence FROM public_research_ops"
            ).fetchone()
            db.execute("UPDATE public_research_ops SET profile_digest='withdrawn'")
        with pytest.raises(ValueError, match="evidence invalid"):
            app.mission_evolution._confirmation_sources(
                {
                    "mission_evidence_operation": row["operation"],
                    "mission_evidence_digest": public_context_digest(
                        (row["evidence"],)
                    ),
                    "generation": app.ledger.control_generation(),
                    "decided_at": (clock.now() + timedelta(minutes=1)).isoformat(),
                },
                app.mission_evolution.pending().proposal,
            )
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "confirm",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        assert app.mission_evolution.pending() is not None
        operation = app.wakeup_operation("wake-2")
        assert app.journal.read(operation)["phase"] == "rejected"
    finally:
        worker.close()


def test_internal_invalid_action_is_rejected(tmp_path):
    app, worker, _, _, _ = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "dialogue",
        },
    )
    try:
        run_once(app, worker)
        payload = {
            **app.journal.read(app.wakeup_operation("wake-1")),
            "mission_action": "unknown",
            "mission_pending_id": app.mission_evolution.pending().proposal.proposal_id,
        }
        _staged_intent(app, "invalid-action", payload)
        with pytest.raises(ValueError, match="invalid public mission action"):
            app.mission_evolution.apply("invalid-action", payload)
    finally:
        worker.close()


def test_public_mission_runtime_rejects_invalid_configuration(tmp_path):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        common = dict(
            model=app.model,
            transport=app.simulator,
            policy=app.policy,
            owner=app.owner,
            provider=app.provider,
            ledger=app.ledger,
            clock=app.clock,
            wakeup_policy=app.scheduler.policy,
            operator=app.operator,
        )
        paths = (
            tmp_path / "other-journal.sqlite",
            tmp_path / "other-scheduler.sqlite",
            tmp_path / "other-threads.sqlite",
        )
        with pytest.raises(ValueError, match="invalid public mission evolution"):
            ThreadsRuntime(*paths, **common, mission_evolution_enabled=True)
        with pytest.raises(ValueError, match="invalid public mission evolution"):
            ThreadsRuntime(
                *paths, **common, mission=app.mission, mission_evolution_enabled=1
            )
        with pytest.raises(ValueError, match="invalid public mission evolution"):
            ThreadsRuntime(
                *paths, **common, mission=app.mission, mission_evolution_enabled=True
            )
        with pytest.raises(ValueError, match="active research profile"):
            ThreadsRuntime(
                *paths,
                **common,
                mission=PublicMissionContext(
                    app.mission.path, clock=lambda: NOW + timedelta(days=1)
                ),
                research_client=PublicResearchClient(
                    tmp_path / "unused-research.sock", expected_worker_uid=os.geteuid()
                ),
                research_profile_path=tmp_path / "missing-profile.json",
                research_scope="public-test",
                research_enabled=True,
                mission_evolution_enabled=True,
            )
    finally:
        worker.close()


def test_stale_worker_context_and_repeated_proposal_are_held(tmp_path):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        run_once(app, worker)
        assert (
            app.inference_mission_evolution_context(app.wakeup_operation("wake-1"))
            == ()
        )
        app.journal.start("stale-worker", {"mission_evolution": ["enabled"]})
        with pytest.raises(ValueError, match="changed during inference"):
            app.inference_mission_evolution_context("stale-worker")
        clock.advance(timedelta(minutes=1))
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "dialogue",
        }
        decide_wakeup(app, worker, clock)
        assert app.journal.read(app.wakeup_operation("wake-2"))["phase"] == "rejected"
        assert len(app.mission_evolution.document().proposals) == 1
        app.mission_evolution = None
        payload = app.journal.read(app.wakeup_operation("wake-1"))
        with pytest.raises(PolicyDeniedError, match="evolution disabled"):
            app._handoff(app.wakeup_operation("wake-1"), payload)
    finally:
        worker.close()


@pytest.mark.parametrize("action", ["confirm", "reject"])
def test_changed_proposal_controls_reject_resolution_before_intent(
    tmp_path, monkeypatch, action
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        original = app.mission_evolution.invariants()
        changed = replace(original, provider_budget_version="renewed-budget")
        sdk.proposal = {
            "decision": "mission",
            "mission_action": action,
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        with monkeypatch.context() as patch:
            patch.setattr(app.mission_evolution, "invariants", lambda: changed)
            decide_wakeup(app, worker, clock)
            rejected = app.journal.read(app.wakeup_operation("wake-2"))
            persisted = {
                **rejected,
                "decision": "revise_mission",
                "mission_action": action,
                "mission_purpose": None,
                "correlation": f"stale-{action}",
            }
            _staged_intent(app, f"stale-{action}", persisted)
            with pytest.raises(ValueError, match="controls changed"):
                app.mission_evolution.apply(f"stale-{action}", persisted)
            with pytest.raises(PolicyDeniedError, match="transition unavailable"):
                app._handoff(f"stale-{action}", persisted)
        assert app.journal.read(app.wakeup_operation("wake-2"))["phase"] == "rejected"
        assert app.mission_evolution.pending() is not None
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
    finally:
        worker.close()


def test_late_worker_attestation_context_change_holds_started_operation(
    tmp_path, monkeypatch
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        run_once(app, worker)
        sdk.proposal = {"decision": "no_action", "reason_category": "low_value"}
        clock.advance(timedelta(minutes=1))
        original_attest = worker.attest
        original_context = app.mission_evolution.model_context
        changed = False

        def late_attestation(request):
            nonlocal changed
            changed = True
            return original_attest(request)

        monkeypatch.setattr(worker, "attest", late_attestation)
        monkeypatch.setattr(
            app.mission_evolution,
            "model_context",
            lambda: () if changed else original_context(),
        )
        decide_wakeup(app, worker, clock)
        operation = app.wakeup_operation("wake-2")
        assert changed
        assert app.journal.read(operation)["phase"] == "held"
        assert app.mission_evolution.pending() is not None
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
    finally:
        worker.close()


def test_transient_worker_attestation_rejection_holds_started_operation(
    tmp_path, monkeypatch
):
    app, worker, _, _, clock = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        original_attest = worker.attest
        original_context = app.mission_evolution.model_context
        in_attestation = False
        changed = []

        def transient_context():
            if in_attestation:
                changed.append(None)
                return ("pending", MissionPurpose.RESEARCH.value)
            return original_context()

        def transient_attestation(request):
            nonlocal in_attestation
            in_attestation = True
            try:
                return original_attest(request)
            finally:
                in_attestation = False

        monkeypatch.setattr(app.mission_evolution, "model_context", transient_context)
        monkeypatch.setattr(worker, "attest", transient_attestation)
        run_once(app, worker)

        operation = app.wakeup_operation("wake-1")
        assert changed
        assert app.mission_evolution.model_context() == ("enabled",)
        assert app.journal.read(operation)["phase"] == "held"
        assert app.recover() == ()
    finally:
        worker.close()


def test_preworker_value_error_holds_changed_inference_context(tmp_path, monkeypatch):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:

        def fail_before_worker(_input):
            monkeypatch.setattr(
                app.mission_evolution,
                "model_context",
                lambda: ("pending", MissionPurpose.RESEARCH.value),
            )
            raise ValueError("public worker request unavailable")

        monkeypatch.setattr(app.engine, "deliberate", fail_before_worker)
        app.run((WakeupEvent("wake-1", NOW, "public initiative"),))
        assert app.journal.read(app.wakeup_operation("wake-1"))["phase"] == "held"
    finally:
        worker.close()


def test_exhausted_research_catalog_withholds_new_mission_proposals(
    tmp_path, monkeypatch
):
    app, worker, sdk, _, clock = configured(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        assert app.mission_evolution.model_context() == ()
        with monkeypatch.context() as patch:
            patch.setattr(app, "current_research_profile", lambda: None)
            assert app.mission_evolution.model_context() == ()
        patch_choice = PublicMissionChoice("propose", MissionPurpose.RESEARCH)
        monkeypatch.setattr(
            app.engine,
            "deliberate",
            lambda _input: DeliberationResult(
                DeliberationOutcome.PROPOSED, patch_choice, None, 1, 1
            ),
        )
        clock.advance(timedelta(minutes=1))
        app.run((WakeupEvent("wake-2", clock.now(), "public reflection"),))
        assert app.journal.read(app.wakeup_operation("wake-2"))["phase"] == "rejected"
        assert app.mission_evolution.document().proposals == ()
    finally:
        worker.close()


def test_full_mission_history_withholds_new_proposals(tmp_path, monkeypatch):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        monkeypatch.setattr(
            "app.core.public_mission_evolution.MAX_MISSION_HISTORY_RECORDS", 1
        )
        assert app.mission_evolution.model_context() == ("pending", "research")
        research_after_proposal(app, worker, sdk, clock)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "reject",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        assert app.mission_evolution.pending() is None
        assert app.mission_evolution.model_context() == ()
    finally:
        worker.close()


@pytest.mark.parametrize(
    ("research_result", "research_phase"),
    [
        ({"outcome": "authorization_denied", "attempts": 0}, "failed"),
        ({"outcome": "timeout", "attempts": 1}, "uncertain"),
    ],
)
def test_failed_final_research_can_abandon_without_evidence_or_retry(
    tmp_path, monkeypatch, research_result, research_phase
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        calls = []

        def fail_research(request):
            calls.append(request)
            return research_result

        monkeypatch.setattr(app.research_client, "execute", fail_research)
        initial_policy = repr(app.policy)
        initial_publication = app.mission.publication_allowed()
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        with app.journal.transaction() as db:
            assert (
                db.execute("SELECT phase FROM public_research_ops").fetchone()[0]
                == research_phase
            )
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "abandon",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        assert app.journal.read(app.wakeup_operation("wake-2"))["phase"] == "linked"
        assert app.mission_evolution.pending() is None
        assert (
            app.mission_evolution.document().proposals[0].status
            is MissionStatus.ABANDONED
        )
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
        assert app.mission.publication_allowed() is initial_publication
        assert repr(app.policy) == initial_policy
        assert app.available_topics(app.current_research_profile()) == ()
        assert len(calls) == 1
        assert app.mission_evolution.model_context() == ()
    finally:
        worker.close()


def test_fresh_abandonment_after_control_generation_change(tmp_path, monkeypatch):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        monkeypatch.setattr(
            app.research_client,
            "execute",
            lambda _request: {"outcome": "authorization_denied", "attempts": 0},
        )
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        original_generation = app.ledger.control_generation()
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "abandon",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        with monkeypatch.context() as patch:
            patch.setattr(
                app.ledger, "control_generation", lambda: original_generation + 1
            )
            decide_wakeup(app, worker, clock)
            operation = app.wakeup_operation("wake-2")
            assert app.journal.read(operation)["phase"] == "linked"
            assert app.mission_evolution.pending() is None
            abandoned = app.mission_evolution.document().proposals[0]
            assert abandoned.status is MissionStatus.ABANDONED
            assert abandoned.abandonment.invariants != abandoned.proposal.invariants
            assert app.mission.snapshot()[1]["version"] == "mission-v1"
    finally:
        worker.close()


@pytest.mark.parametrize("crash_stage", ["before_commit", "after_commit"])
def test_abandonment_reconciles_across_restart_without_retry(
    tmp_path, monkeypatch, crash_stage
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        research_calls = []

        def fail_research(request):
            research_calls.append(request)
            return {"outcome": "authorization_denied", "attempts": 0}

        monkeypatch.setattr(app.research_client, "execute", fail_research)
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "abandon",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))

        def crash(point):
            if crash_stage == "before_commit" and point == "after_scheduler":
                raise SystemExit("before mission commit")

        app.checkpoint = crash
        if crash_stage == "after_commit":
            monkeypatch.setattr(
                app,
                "_record_mission_outcome",
                lambda _operation, _payload: (_ for _ in ()).throw(
                    SystemExit("after mission commit")
                ),
            )
        with pytest.raises(SystemExit):
            decide_wakeup(app, worker, clock)
        operation = app.wakeup_operation("wake-2")
        assert app.journal.read(operation)["phase"] == (
            "intent" if crash_stage == "before_commit" else "linked"
        )
        model_calls = len(sdk.calls)
        restarted = restarted_evolution_runtime(app, tmp_path)
        assert restarted.recover() == ()
        assert restarted.recover() == ()
        assert restarted.journal.read(operation)["phase"] == "linked"
        assert restarted.mission_evolution.pending() is None
        assert len(restarted.mission_evolution.document().proposals) == 1
        assert (
            restarted.mission_evolution.document().proposals[0].status
            is MissionStatus.ABANDONED
        )
        assert restarted.mission.snapshot()[1]["version"] == "mission-v1"
        assert len(sdk.calls) == model_calls
        assert len(research_calls) == 1
    finally:
        worker.close()


def test_abandonment_only_reopens_proposals_with_real_research_capacity(
    tmp_path, monkeypatch
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        for role in ("application", "worker"):
            path = tmp_path / f"{role}.json"
            profile = json.loads(path.read_text())
            profile["aggregate_limit"] = 2
            profile["topics"][0]["limit"] = 2
            path.write_text(json.dumps(profile))
        monkeypatch.setattr(
            app.research_client,
            "execute",
            lambda _request: {"outcome": "authorization_denied", "attempts": 0},
        )
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "abandon",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        assert app.mission_evolution.model_context() == ("enabled",)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "dialogue",
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock, "wake-3")
        assert (
            app.mission_evolution.pending().proposal.purpose is MissionPurpose.DIALOGUE
        )
        assert len(app.mission_evolution.document().proposals) == 2
    finally:
        worker.close()


def test_research_arriving_after_decision_cannot_confirm(tmp_path, monkeypatch):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        original = app.journal.read(app.wakeup_operation("wake-1"))
        stale_confirmation = {
            **original,
            "event": "wake-2",
            "correlation": "stale-confirm",
            "mission_action": "confirm",
            "mission_purpose": None,
            "mission_pending_id": app.mission_evolution.pending().proposal.proposal_id,
            "decided_at": (NOW + timedelta(minutes=1)).isoformat(),
        }
        _staged_intent(app, "stale-confirm", stale_confirmation)
        app.journal.start(
            "inference-before-research",
            {
                "kind": "wake",
                "event": "inference-before-research",
                "offered_profile_digest": app.current_research_profile().digest,
                "mission_evidence_operation": None,
                "mission_evidence_digest": public_context_digest(()),
            },
        )
        clock.advance(timedelta(minutes=2))
        research_after_proposal(app, worker, sdk, clock)
        with pytest.raises(ValueError, match="evidence changed during inference"):
            app.inference_public_context("inference-before-research")
        with pytest.raises(ValueError, match="not offered"):
            app.mission_evolution.apply("stale-confirm", stale_confirmation)
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
    finally:
        worker.close()


def test_context_race_finishes_started_operation(tmp_path, monkeypatch):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        original = app._public_context_with_source
        calls = []

        def changing_context(profile):
            calls.append(None)
            if len(calls) == 1:
                return original(profile)
            return (), ("new research evidence",), "new-research-operation"

        monkeypatch.setattr(app, "_public_context_with_source", changing_context)
        monkeypatch.setattr(
            app.engine,
            "deliberate",
            lambda _request: app.model.public_context("context-race"),
        )
        app._decide(
            "context-race",
            {"kind": "wake", "event": "wake-race", "correlation": "context-race"},
            (),
        )
        assert app.journal.read("context-race")["phase"] == "held"
        assert len(calls) == 2
    finally:
        worker.close()


@pytest.mark.parametrize("skip_public_context", [False, True])
def test_withdrawn_evidence_during_inference_holds_operation(
    tmp_path, monkeypatch, skip_public_context
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "confirm",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        if skip_public_context:
            app.model.public_context = None

        def withdraw(point):
            if point == "after_started":
                with app.journal.transaction() as db:
                    db.execute("UPDATE public_research_ops SET phase='held'")

        app.checkpoint = withdraw
        app._decide(
            "withdrawn-context",
            {
                "kind": "wake",
                "event": "wake-withdrawn",
                "correlation": "withdrawn-context",
            },
            (),
        )
        assert app.journal.read("withdrawn-context")["phase"] == "held"
    finally:
        worker.close()


def test_withdrawn_evidence_after_inference_holds_operation(tmp_path, monkeypatch):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        for role in ("application", "worker"):
            profile_path = tmp_path / f"{role}.json"
            profile = json.loads(profile_path.read_text())
            profile["aggregate_limit"] = 2
            profile["topics"][0]["limit"] = 2
            profile_path.write_text(json.dumps(profile))
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "confirm",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        assert app.mission_evolution.pending() is None

        def withdraw_after_inference(request):
            app.model.public_context(request.correlation_id)
            assert app.model.mission_evolution_context(request.correlation_id) == (
                "enabled",
            )
            with app.journal.transaction() as db:
                db.execute("UPDATE public_research_ops SET phase='held'")
            return DeliberationResult(
                DeliberationOutcome.PROPOSED,
                PublicMissionChoice("propose", MissionPurpose.REFLECTION),
                None,
                1,
                1,
            )

        monkeypatch.setattr(app.engine, "deliberate", withdraw_after_inference)
        clock.advance(timedelta(minutes=1))
        app._decide(
            "after-inference",
            {
                "kind": "wake",
                "event": "wake-3",
                "correlation": "after-inference",
            },
            (),
        )
        assert app.journal.read("after-inference")["phase"] == "held"
    finally:
        worker.close()


@pytest.mark.parametrize("profile_change", ["missing", "replaced"])
def test_transient_profile_change_during_inference_holds_confirmation(
    tmp_path, monkeypatch, profile_change
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        proposal_id = app.mission_evolution.pending().proposal.proposal_id
        original_profile = app.current_research_profile()

        def unavailable_during_callback(request):
            with monkeypatch.context() as patcher:
                patcher.setattr(
                    app,
                    "current_research_profile",
                    lambda: None
                    if profile_change == "missing"
                    else replace(original_profile, digest="0" * 64),
                )
                app.model.public_context(request.correlation_id)
            app.model.mission_evolution_context(request.correlation_id)
            return DeliberationResult(
                DeliberationOutcome.PROPOSED,
                PublicMissionChoice("confirm", None),
                None,
                1,
                1,
            )

        monkeypatch.setattr(app.engine, "deliberate", unavailable_during_callback)
        clock.advance(timedelta(minutes=1))
        app._decide(
            "transient-profile-loss",
            {
                "kind": "wake",
                "event": "wake-profile-loss",
                "correlation": "transient-profile-loss",
            },
            (),
        )
        assert app.journal.read("transient-profile-loss")["phase"] == "held"
        assert app.mission_evolution.pending().proposal.proposal_id == proposal_id
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
    finally:
        worker.close()


@pytest.mark.parametrize("stage", ["attestation", "after_result", "before_finish"])
def test_pending_evidence_withdrawal_holds_at_each_inference_boundary(
    tmp_path, monkeypatch, stage
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "confirm",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))

        def withdraw():
            with app.journal.transaction() as db:
                db.execute("UPDATE public_research_ops SET phase='held'")

        if stage == "attestation":
            original_attest = worker.attest

            def attest(request):
                withdraw()
                return original_attest(request)

            monkeypatch.setattr(worker, "attest", attest)
        elif stage == "after_result":
            original_deliberate = app.engine.deliberate

            def deliberate(value):
                result = original_deliberate(value)
                withdraw()
                return result

            monkeypatch.setattr(app.engine, "deliberate", deliberate)
        else:
            original_finish = app.journal.finish

            def finish(operation, payload, **kwargs):
                withdraw()
                return original_finish(operation, payload, **kwargs)

            monkeypatch.setattr(app.journal, "finish", finish)

        decide_wakeup(app, worker, clock)
        operation = app.wakeup_operation("wake-2")
        assert app.journal.read(operation)["phase"] == "held"
        assert app.mission_evolution.pending() is not None
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
    finally:
        worker.close()


def test_late_pending_authorization_failure_holds_started_operation(
    tmp_path, monkeypatch
):
    app, worker, _, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        run_once(app, worker)
        clock.advance(timedelta(minutes=1))
        original_context = app.inference_public_context
        completed = False

        def deliberate(_value):
            nonlocal completed
            completed = True
            return DeliberationResult(
                DeliberationOutcome.PROPOSED,
                PublicMissionChoice("abandon", None),
                None,
                1,
                1,
            )

        def context(operation):
            value = original_context(operation)
            if completed:
                monkeypatch.setattr(
                    app.mission_evolution,
                    "pending",
                    lambda: (_ for _ in ()).throw(ValueError("authority withdrawn")),
                )
            return value

        monkeypatch.setattr(app.engine, "deliberate", deliberate)
        monkeypatch.setattr(app, "inference_public_context", context)
        app._decide(
            "late-pending-failure",
            {
                "kind": "wake",
                "event": "wake-late-pending",
                "correlation": "late-pending-failure",
            },
            (),
        )
        assert app.journal.read("late-pending-failure")["phase"] == "held"
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
    finally:
        worker.close()


def test_public_inference_context_excludes_finished_operation(tmp_path):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        payload = {"kind": "wake", "event": "finished-event"}
        assert app.journal.start("finished-context", payload)
        app.journal.finish("finished-context", payload, held=True)
        assert app.inference_public_context("finished-context") == ((), ())
    finally:
        worker.close()


def test_confirmation_checks_exact_offered_research_record(tmp_path, monkeypatch):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        proposal = app.mission_evolution.pending().proposal
        with app.journal.transaction() as db:
            row = db.execute(
                "SELECT operation,evidence FROM public_research_ops"
            ).fetchone()
        payload = {
            "mission_evidence_operation": row["operation"],
            "mission_evidence_digest": public_context_digest((row["evidence"],)),
            "generation": app.ledger.control_generation(),
            "decided_at": (clock.now() + timedelta(minutes=1)).isoformat(),
        }
        with app.journal.transaction() as db:
            assert app.mission_evolution.offered_evidence_in(
                db, {"mission_evidence_operation": None}
            )
            assert not app.mission_evolution.offered_evidence_in(
                db,
                payload
                | {
                    "mission_evidence_operation": "unknown",
                    "offered_profile_digest": app.current_research_profile().digest,
                },
            )
        with pytest.raises(ValueError, match="independent later"):
            app.mission_evolution._confirmation_sources(
                payload | {"mission_evidence_operation": "unknown"}, proposal
            )
        with pytest.raises(ValueError, match="independent later"):
            app.mission_evolution._confirmation_sources(
                payload | {"mission_evidence_digest": "copied"}, proposal
            )
        with pytest.raises(ValueError, match="independent later"):
            app.mission_evolution._confirmation_sources(
                payload | {"generation": payload["generation"] + 1}, proposal
            )
        with pytest.raises(ValueError, match="independent later"):
            app.mission_evolution._confirmation_sources(
                payload, replace(proposal, decided_at=clock.now())
            )
        with pytest.raises(ValueError, match="independent later"):
            app.mission_evolution._confirmation_sources(
                payload | {"decided_at": clock.now().isoformat()}, proposal
            )
        source = MissionEvidenceSource(
            row["operation"],
            app.mission_evolution.source_id(row["evidence"]),
            EvidenceProvenance.AGENT_OBSERVATION,
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                app.ledger,
                "control_generation",
                lambda: payload["generation"] + 1,
            )
            assert not app.mission_evolution.authorized(
                proposal.initiating_source_id, (source,)
            )
    finally:
        worker.close()


def test_latest_qualifying_research_completion_is_offered(tmp_path, monkeypatch):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        proposal_at = app.mission_evolution.pending().proposal.decided_at
        research_after_proposal(app, worker, sdk, clock)
        with app.journal.transaction() as db:
            valid = db.execute(
                "SELECT r.operation,i.payload FROM public_research_ops r "
                "JOIN intents i ON i.operation=r.operation"
            ).fetchone()
        older_operation = "older-completion"
        _staged_intent(app, older_operation, json.loads(valid["payload"]))
        with app.journal.transaction() as db:
            db.execute(
                "INSERT INTO public_research_ops VALUES(?,?,?,?,?,?)",
                (
                    older_operation,
                    "older-research",
                    "ocean",
                    app.current_research_profile().digest,
                    "completed",
                    "older evidence",
                ),
            )
            db.execute(
                "INSERT INTO public_research_completions VALUES(?,?)",
                (older_operation, (proposal_at - timedelta(seconds=1)).isoformat()),
            )
        _, evidence, source = app._public_context_with_source(
            app.current_research_profile()
        )
        assert source == valid["operation"]
        assert len(evidence) == 1
        with app.journal.transaction() as db:
            db.execute(
                "DELETE FROM public_research_completions WHERE operation=?",
                (valid["operation"],),
            )
        assert app._public_context_with_source(app.current_research_profile())[1:] == (
            (),
            None,
        )
    finally:
        worker.close()


def test_confirmed_mission_holds_old_approved_publication(tmp_path, monkeypatch):
    app, worker, sdk, http, clock = configured(
        tmp_path,
        {
            "decision": "propose_publication",
            "draft": "Публичный исследовательский черновик.",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        old_operation = app.wakeup_operation("wake-1")
        assert app.draft(old_operation).state is OutboxState.AWAITING_APPROVAL
        assert (
            app.approve(
                old_operation, expires_at=clock.now() + timedelta(minutes=10)
            ).state
            is OutboxState.READY
        )
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "reflection",
        }
        clock.advance(timedelta(minutes=1))
        assert decide_inbound(app, worker)
        sdk.proposal = {"decision": "research", "topic_id": "ocean"}
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "confirm",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock, "wake-3")
        assert app.mission.snapshot()[1]["purpose"] == "reflection"
        with pytest.raises(ValueError, match="public mission changed"):
            app.approve(old_operation, expires_at=clock.now() + timedelta(minutes=5))
        with pytest.raises(PolicyDeniedError):
            app.dispatch(old_operation)
        assert not any(call[0] == "POST" for call in http.calls)
    finally:
        worker.close()


@pytest.mark.parametrize("action", ["confirm", "abandon"])
def test_resolution_cannot_target_proposal_created_during_inference(
    tmp_path, monkeypatch, action
):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:

        def overlap(_input):
            started = app.journal.read("first-decision")
            other = {
                **started,
                "event": "other-event",
                "correlation": "other-decision",
                "generation": app.ledger.control_generation(),
                "decided_at": app.clock.now().isoformat(),
                "decision": "revise_mission",
                "mission_action": "propose",
                "mission_purpose": "research",
            }
            _staged_intent(app, "other-decision", other)
            app.mission_evolution.apply("other-decision", other)
            return DeliberationResult(
                DeliberationOutcome.PROPOSED,
                PublicMissionChoice(action, None),
                None,
                1,
                1,
            )

        monkeypatch.setattr(app.engine, "deliberate", overlap)
        app._decide(
            "first-decision",
            {"kind": "wake", "event": "first-event", "correlation": "first-decision"},
            (),
        )
        first = app.journal.read("first-decision")
        assert first["phase"] == "held"
        assert (
            app.mission_evolution.pending().proposal.initiating_event_id
            == "other-event"
        )
        forged = {
            **first,
            "mission_action": action,
            "mission_purpose": None,
            "decided_at": (app.clock.now() + timedelta(minutes=1)).isoformat(),
            "generation": app.ledger.control_generation(),
        }
        _staged_intent(app, "unseen-confirmation", forged)
        with pytest.raises(ValueError, match="proposal changed"):
            app.mission_evolution.apply("unseen-confirmation", forged)
    finally:
        worker.close()


@pytest.mark.parametrize("first_action", ["confirm", "abandon"])
def test_confirmation_and_abandonment_race_has_one_terminal_outcome(
    tmp_path, monkeypatch, first_action
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        clock.advance(timedelta(minutes=1))

        def choose(request):
            return DeliberationResult(
                DeliberationOutcome.PROPOSED,
                PublicMissionChoice(request.correlation_id.split("-")[1], None),
                None,
                1,
                1,
            )

        monkeypatch.setattr(app.engine, "deliberate", choose)
        for action in ("confirm", "abandon"):
            app._decide(
                f"race-{action}",
                {
                    "kind": "wake",
                    "event": f"event-{action}",
                    "correlation": f"race-{action}",
                },
                (),
            )
            assert app.journal.read(f"race-{action}")["phase"] == "intent"
        second_action = "abandon" if first_action == "confirm" else "confirm"
        first_operation = f"race-{first_action}"
        second_operation = f"race-{second_action}"
        app.mission_evolution.apply(first_operation, app.journal.read(first_operation))
        app._materialize(second_operation, app.journal.read(second_operation))
        assert app.journal.read(second_operation)["phase"] == "held"
        assert app.mission_evolution.pending() is None
        records = app.mission_evolution.document().proposals
        assert len(records) == 1
        assert records[0].status is (
            MissionStatus.CONFIRMED
            if first_action == "confirm"
            else MissionStatus.ABANDONED
        )
        with pytest.raises(ValueError, match="not pending|mission changed"):
            app.mission_evolution.apply(
                first_operation, app.journal.read(first_operation)
            )
    finally:
        worker.close()


def test_pending_change_before_intent_commit_holds_operation(tmp_path, monkeypatch):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        monkeypatch.setattr(
            app.engine,
            "deliberate",
            lambda _input: DeliberationResult(
                DeliberationOutcome.PROPOSED,
                PublicMissionChoice("propose", MissionPurpose.RESEARCH),
                None,
                1,
                1,
            ),
        )
        original_finish = app.journal.finish
        injected = []

        def finish_with_overlap(operation, payload, **kwargs):
            if operation == "first-proposal" and kwargs.get("admissible"):
                other = {
                    **payload,
                    "event": "other-event",
                    "correlation": "other-proposal",
                }
                _staged_intent(app, "other-proposal", other)
                app.mission_evolution.apply("other-proposal", other)
                injected.append(None)
            return original_finish(operation, payload, **kwargs)

        monkeypatch.setattr(app.journal, "finish", finish_with_overlap)
        app._decide(
            "first-proposal",
            {
                "kind": "wake",
                "event": "first-event",
                "correlation": "first-proposal",
            },
            (),
        )
        assert len(injected) == 1
        assert app.journal.read("first-proposal")["phase"] == "held"
        assert (
            app.mission_evolution.pending().proposal.initiating_event_id
            == "other-event"
        )
    finally:
        worker.close()


def test_abandonment_rejects_wrong_identity_parent_and_duplicate(tmp_path):
    app, worker, _, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        run_once(app, worker)
        clock.advance(timedelta(minutes=1))
        payload = _staged_abandonment(app, "abandon-valid", "event-abandon")
        wrong = {**payload, "mission_pending_id": "wrong-proposal"}
        _staged_intent(app, "abandon-wrong", wrong)
        with pytest.raises(ValueError, match="proposal changed"):
            app.mission_evolution.apply("abandon-wrong", wrong)
        stale_parent = {
            **payload,
            "mission": {**payload["mission"], "version": "other"},
        }
        _staged_intent(app, "abandon-parent", stale_parent)
        with pytest.raises(ValueError, match="mission changed"):
            app.mission_evolution.apply("abandon-parent", stale_parent)
        app.mission_evolution.apply("abandon-valid", payload)
        duplicate = {
            **payload,
            "event": "event-duplicate",
            "correlation": "abandon-duplicate",
        }
        _staged_intent(app, "abandon-duplicate", duplicate)
        with pytest.raises(ValueError, match="proposal changed"):
            app.mission_evolution.apply("abandon-duplicate", duplicate)
        assert len(app.mission_evolution.document().proposals) == 1
        assert (
            app.mission_evolution.document().proposals[0].status
            is MissionStatus.ABANDONED
        )
    finally:
        worker.close()


def test_stale_abandonment_cannot_touch_replacement_proposal(tmp_path):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        run_once(app, worker)
        clock.advance(timedelta(minutes=1))
        first = _staged_abandonment(app, "abandon-first", "event-first")
        stale = _staged_abandonment(app, "abandon-stale", "event-stale")
        app.mission_evolution.apply("abandon-first", first)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "reflection",
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        replacement = app.mission_evolution.pending().proposal.proposal_id
        app._materialize("abandon-stale", stale)
        assert app.journal.read("abandon-stale")["phase"] == "held"
        assert app.mission_evolution.pending().proposal.proposal_id == replacement
        assert len(app.mission_evolution.document().proposals) == 2
    finally:
        worker.close()


@pytest.mark.parametrize("control", ["research_worker", "privacy_generation", "stop"])
def test_control_change_before_intent_commit_holds_operation(
    tmp_path, monkeypatch, control
):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        monkeypatch.setattr(
            app.engine,
            "deliberate",
            lambda _input: DeliberationResult(
                DeliberationOutcome.PROPOSED,
                PublicMissionChoice("propose", MissionPurpose.RESEARCH),
                None,
                1,
                1,
            ),
        )
        original_finish = app.journal.finish
        changed = []

        def finish_after_control_change(operation, payload, **kwargs):
            if operation == "late-controls" and kwargs.get("admissible"):
                if control == "research_worker":
                    app.research_client = PublicResearchClient(
                        tmp_path / "replacement-research.sock",
                        expected_worker_uid=os.geteuid(),
                    )
                elif control == "privacy_generation":
                    generation = app.ledger.control_generation()
                    monkeypatch.setattr(
                        app.ledger, "control_generation", lambda: generation + 1
                    )
                else:
                    app.policy.emergency_stop = True
                changed.append(None)
            return original_finish(operation, payload, **kwargs)

        monkeypatch.setattr(app.journal, "finish", finish_after_control_change)
        app._decide(
            "late-controls",
            {
                "kind": "wake",
                "event": "wake-late-controls",
                "correlation": "late-controls",
            },
            (),
        )
        assert len(changed) == 1
        assert app.journal.read("late-controls")["phase"] == "held"
        assert app.mission_evolution.document().proposals == ()
    finally:
        worker.close()


@pytest.mark.parametrize("drift", ["conversation", "profile", "generation"])
def test_offered_research_authorization_change_before_intent_commit_holds_operation(
    tmp_path, monkeypatch, drift
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        with app.journal.transaction() as db:
            research_operation = db.execute(
                "SELECT operation FROM public_research_ops"
            ).fetchone()[0]
        research = app.journal.read(research_operation)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "confirm",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        original_finish = app.journal.finish
        changed = []

        def finish_after_source_change(operation, payload, **kwargs):
            if operation == app.wakeup_operation("wake-2") and kwargs.get("admissible"):
                if drift == "conversation":
                    app.service.store.record_conversation_outcome(
                        outcome_id="late-research-control",
                        correlation_id=research["correlation"],
                        account_ref=app.account,
                        stream=InboundKind(research["kind"]),
                        inbound_event_id=research["event"],
                        outcome=ConversationOutcome.TERMINATE_CONVERSATION,
                        reason="source-withdrawn",
                        resume_condition=None,
                        now=clock.now(),
                    )
                else:
                    research[
                        "profile_digest" if drift == "profile" else "generation"
                    ] = (
                        "withdrawn"
                        if drift == "profile"
                        else research["generation"] + 1
                    )
                    with app.journal.transaction() as db:
                        db.execute(
                            "UPDATE intents SET payload=? WHERE operation=?",
                            (json.dumps(research), research_operation),
                        )
                changed.append(None)
            return original_finish(operation, payload, **kwargs)

        monkeypatch.setattr(app.journal, "finish", finish_after_source_change)
        decide_wakeup(app, worker, clock)
        assert len(changed) == 1
        if drift == "conversation":
            assert not app.source_valid(research)
        assert app.journal.read(app.wakeup_operation("wake-2"))["phase"] == "held"
        assert app.mission_evolution.pending() is not None
    finally:
        worker.close()


@pytest.mark.parametrize("initial_evidence", [False, True])
def test_newer_research_completion_before_decision_commit_holds_operation(
    tmp_path, monkeypatch, initial_evidence
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        if initial_evidence:
            research_after_proposal(app, worker, sdk, clock)
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "confirm" if initial_evidence else "abandon",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        original_finish = app.journal.finish
        inserted = []

        def finish_after_newer_research(operation, payload, **kwargs):
            if kwargs.get("admissible"):
                research = {
                    **payload,
                    "decision": "research",
                    "topic": "ocean",
                    "profile_digest": payload["offered_profile_digest"],
                }
                with app.journal.transaction() as db:
                    db.execute(
                        "INSERT INTO intents VALUES(?,?,?)",
                        ("newer-research", "linked", json.dumps(research)),
                    )
                    db.execute(
                        "INSERT INTO public_research_ops VALUES(?,?,?,?,?,?)",
                        (
                            "newer-research",
                            "newer-research-id",
                            "ocean",
                            research["profile_digest"],
                            "completed",
                            "newer evidence",
                        ),
                    )
                    db.execute(
                        "INSERT INTO public_research_completions VALUES(?,?)",
                        ("newer-research", clock.now().isoformat()),
                    )
                inserted.append(None)
            return original_finish(operation, payload, **kwargs)

        monkeypatch.setattr(app.journal, "finish", finish_after_newer_research)
        decide_wakeup(app, worker, clock)
        assert len(inserted) == 1
        assert app.journal.read(app.wakeup_operation("wake-2"))["phase"] == "held"
        assert app.mission_evolution.pending() is not None
    finally:
        worker.close()


def test_proposal_rechecks_research_capacity_before_intent_commit(
    tmp_path, monkeypatch
):
    app, worker, _, _, _ = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        original_finish = app.journal.finish
        consumed = []

        def finish_after_capacity_consumed(operation, payload, **kwargs):
            if kwargs.get("admissible"):
                with app.journal.transaction() as db:
                    db.execute(
                        "INSERT INTO public_research_ops VALUES(?,?,?,?,?,NULL)",
                        (
                            "competing-research",
                            "competing-research-id",
                            "ocean",
                            app.current_research_profile().digest,
                            "started",
                        ),
                    )
                consumed.append(None)
            return original_finish(operation, payload, **kwargs)

        monkeypatch.setattr(app.journal, "finish", finish_after_capacity_consumed)
        run_once(app, worker)
        assert len(consumed) == 1
        assert app.available_topics(app.current_research_profile()) == ()
        assert app.journal.read(app.wakeup_operation("wake-1"))["phase"] == "held"
        assert app.mission_evolution.document().proposals == ()
    finally:
        worker.close()


def test_proposal_rechecks_history_capacity_before_intent_commit(tmp_path, monkeypatch):
    app, worker, _, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        monkeypatch.setattr(
            "app.core.public_mission_evolution.MAX_MISSION_HISTORY_RECORDS", 1
        )
        original_finish = app.journal.finish
        filled = []

        def finish_after_history_filled(operation, payload, **kwargs):
            if kwargs.get("admissible"):
                app.journal.finish = original_finish
                try:
                    competing = {
                        **payload,
                        "event": "competing-propose",
                        "correlation": "competing-propose",
                    }
                    _staged_intent(app, "competing-propose", competing)
                    clock.advance(timedelta(seconds=1))
                    app.mission_evolution.apply("competing-propose", competing)
                    abandoned = {
                        **competing,
                        "event": "competing-abandon",
                        "correlation": "competing-abandon",
                        "mission_action": "abandon",
                        "mission_pending_id": (
                            app.mission_evolution.pending().proposal.proposal_id
                        ),
                    }
                    _staged_intent(app, "competing-abandon", abandoned)
                    clock.advance(timedelta(seconds=1))
                    app.mission_evolution.apply("competing-abandon", abandoned)
                    filled.append(None)
                finally:
                    app.journal.finish = finish_after_history_filled
            return original_finish(operation, payload, **kwargs)

        monkeypatch.setattr(app.journal, "finish", finish_after_history_filled)
        run_once(app, worker)
        assert len(filled) == 1
        assert app.mission_evolution.pending() is None
        assert len(app.mission_evolution.document().proposals) == 1
        assert app.journal.read(app.wakeup_operation("wake-1"))["phase"] == "held"
    finally:
        worker.close()


@pytest.mark.parametrize("pending", [False, True])
def test_legacy_research_remains_visible_without_pending_proposal(tmp_path, pending):
    decision = (
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        }
        if pending
        else {"decision": "no_action", "reason_category": "low_value"}
    )
    app, worker, _, _, _ = configured(tmp_path, decision)
    try:
        run_once(app, worker)
        profile = app.current_research_profile()
        research = {
            **app.journal.read(app.wakeup_operation("wake-1")),
            "decision": "research",
            "topic": "ocean",
            "profile_digest": profile.digest,
        }
        with app.journal.transaction() as db:
            db.execute(
                "INSERT INTO intents VALUES(?,?,?)",
                ("legacy-research", "linked", json.dumps(research)),
            )
            db.execute(
                "INSERT INTO public_research_ops VALUES(?,?,?,?,?,?)",
                (
                    "legacy-research",
                    "legacy-research-id",
                    "ocean",
                    profile.digest,
                    "completed",
                    "legacy evidence",
                ),
            )
        _, evidence, source = app._public_context_with_source(profile)
        assert (evidence, source) == (
            ((), None) if pending else (("legacy evidence",), "legacy-research")
        )
    finally:
        worker.close()


def test_recovery_holds_preupgrade_research_intent_without_invariants(tmp_path):
    app, worker, _, _, _ = configured(
        tmp_path, {"decision": "no_action", "reason_category": "low_value"}
    )
    try:
        assert decide_inbound(app, worker)
        with app.journal.transaction() as db:
            row = db.execute(
                "SELECT payload FROM intents WHERE json_extract(payload,'$.kind')='reply' "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        legacy = json.loads(row[0])
        legacy.update(
            decision="research",
            topic="ocean",
            profile_digest=app.current_research_profile().digest,
        )
        for field in (
            "mission_invariants",
            "mission_evolution",
            "mission_pending_id",
            "mission_evidence_operation",
            "mission_evidence_digest",
        ):
            legacy.pop(field, None)
        operation = "preupgrade-research-intent"
        _staged_intent(app, operation, legacy)

        app.recover()

        assert app.journal.read(operation)["phase"] == "held"
        with app.journal.transaction() as db:
            assert (
                db.execute(
                    "SELECT 1 FROM public_research_ops WHERE operation=?", (operation,)
                ).fetchone()
                is None
            )
        app.recover()
        assert app.journal.read(operation)["phase"] == "held"
    finally:
        worker.close()


def test_controlled_inbound_conversation_before_mission_intent_is_held(
    tmp_path, monkeypatch
):
    app, worker, _, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "dialogue",
        },
    )
    try:
        original_finish = app.journal.finish
        changed = []

        def finish_after_conversation_control(operation, payload, **kwargs):
            if payload.get("decision") == "revise_mission" and kwargs.get("admissible"):
                app.service.store.record_conversation_outcome(
                    outcome_id="late-inbound-control",
                    correlation_id=payload["correlation"],
                    account_ref=app.account,
                    stream=InboundKind(payload["kind"]),
                    inbound_event_id=payload["event"],
                    outcome=ConversationOutcome.TERMINATE_CONVERSATION,
                    reason="conversation-closed",
                    resume_condition=None,
                    now=clock.now(),
                )
                changed.append(operation)
            return original_finish(operation, payload, **kwargs)

        monkeypatch.setattr(app.journal, "finish", finish_after_conversation_control)
        assert decide_inbound(app, worker) == ()
        assert len(changed) == 1
        assert app.journal.read(changed[0])["phase"] == "held"
        assert app.mission_evolution.document().proposals == ()
    finally:
        worker.close()


@pytest.mark.parametrize(
    "control",
    [ConversationOutcome.TERMINATE_CONVERSATION, ConversationOutcome.COOLDOWN],
)
@pytest.mark.parametrize("decision_offset", [-1, 1])
def test_later_conversation_control_reconciles_committed_mission(
    tmp_path, monkeypatch, control, decision_offset
):
    app, worker, _, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "dialogue",
        },
    )
    try:
        original_record = app._record_mission_outcome
        changed = []

        def record_after_later_control(operation, payload):
            if changed:
                return original_record(operation, payload)
            decided_at = datetime.fromisoformat(payload["decided_at"])
            clock.advance(timedelta(seconds=1))
            stream = InboundKind(payload["kind"])
            source = app.service.store.inbound_event(
                app.account, stream, payload["event"]
            )
            later = InboundEvent(
                "later-control-event",
                "later-control-correlation",
                app.account,
                app.simulator.source_ref,
                stream,
                source.conversation_id,
                None,
                decided_at - timedelta(seconds=1),
                "Публичное завершение.",
            )
            app.service.store.commit_poll_page(
                correlation_id="poll-later-control",
                account_ref=app.account,
                stream=stream,
                verified_source_ref=app.simulator.source_ref,
                expected_cursor=None,
                next_cursor=None,
                events=(later,),
                now=clock.now(),
                precommit_check=lambda: None,
            )
            app.service.store.record_conversation_outcome(
                outcome_id="later-conversation-control",
                correlation_id=later.correlation_id,
                account_ref=app.account,
                stream=stream,
                inbound_event_id=later.event_id,
                outcome=control,
                reason="conversation-closed",
                resume_condition=None,
                now=decided_at + timedelta(seconds=decision_offset),
                control_expires_at=(
                    clock.now() + timedelta(minutes=1)
                    if control is ConversationOutcome.COOLDOWN
                    else None
                ),
            )
            changed.append(operation)
            return original_record(operation, payload)

        monkeypatch.setattr(app, "_record_mission_outcome", record_after_later_control)
        decide_inbound(app, worker)
        assert len(changed) == 1
        assert app.journal.read(changed[0])["phase"] == "linked"
        assert app.service.store.conversation_outcome_recorded(changed[0])
        assert app.mission_evolution.pending() is not None
        source = app.service.store.inbound_event(
            app.account, InboundKind.REPLY, "reply-1"
        )
        assert (
            app.service.store.conversation_control(
                app.account, InboundKind.REPLY, source.conversation_id, now=clock.now()
            ).source_outcome_id
            == "later-conversation-control"
        )
        clock.advance(timedelta(minutes=2))
        assert app.recover() == ()
    finally:
        worker.close()


@pytest.mark.parametrize("action", ["confirm", "reject"])
def test_wake_resolution_fences_control_of_inbound_research(
    tmp_path, monkeypatch, action
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        with app.journal.transaction() as db:
            research_row = db.execute(
                "SELECT operation,evidence FROM public_research_ops"
            ).fetchone()
        research_payload = app.journal.read(research_row["operation"])
        assert research_payload["kind"] == "reply"
        clock.advance(timedelta(minutes=1))
        operation = f"wake-{action}-using-inbound-research"
        payload = {
            **app.journal.read(app.wakeup_operation("wake-1")),
            "event": operation,
            "correlation": operation,
            "mission_action": action,
            "mission_purpose": None,
            "mission_pending_id": app.mission_evolution.pending().proposal.proposal_id,
            "mission_evidence_operation": research_row["operation"],
            "mission_evidence_digest": public_context_digest(
                (research_row["evidence"],)
            ),
            "offered_profile_digest": app.current_research_profile().digest,
            "decided_at": clock.now().isoformat(),
        }
        _staged_intent(app, operation, payload)
        original_sources = app.mission_evolution._confirmation_sources
        control_finished = Event()
        observed = []
        control_phase = []
        control_thread = None

        def commit_control():
            app.service.store.record_conversation_outcome(
                outcome_id=f"control-{operation}",
                correlation_id=research_payload["correlation"],
                account_ref=app.account,
                stream=InboundKind.REPLY,
                inbound_event_id=research_payload["event"],
                outcome=ConversationOutcome.TERMINATE_CONVERSATION,
                reason="conversation-closed",
                resume_condition=None,
                now=clock.now(),
            )
            control_phase.append(app.journal.read(operation)["phase"])
            control_finished.set()

        def sources_then_control(*args):
            nonlocal control_thread
            sources = original_sources(*args)
            control_thread = Thread(target=commit_control)
            control_thread.start()
            observed.append(control_finished.wait(0.5))
            return sources

        monkeypatch.setattr(
            app.mission_evolution, "_confirmation_sources", sources_then_control
        )
        try:
            app._materialize(operation, payload)
        finally:
            if control_thread is not None:
                control_thread.join(timeout=5)
                assert not control_thread.is_alive()
        assert observed == [False]
        assert app.journal.read(operation)["phase"] == "linked"
        assert control_phase == ["linked"]
    finally:
        worker.close()


def test_inbound_control_write_waits_for_mission_commit(tmp_path, monkeypatch):
    app, worker, _, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "dialogue",
        },
    )
    try:
        original_apply = app.mission_evolution.apply
        observed = []
        control_operation = "racing-conversation-control"

        def apply_with_competing_control(operation, payload):
            stream = InboundKind(payload["kind"])
            source = app.service.store.inbound_event(
                app.account, stream, payload["event"]
            )
            later = InboundEvent(
                "racing-control-event",
                "racing-control-correlation",
                app.account,
                app.simulator.source_ref,
                stream,
                source.conversation_id,
                None,
                clock.now(),
                "Публичное завершение.",
            )
            app.service.store.commit_poll_page(
                correlation_id="poll-racing-control",
                account_ref=app.account,
                stream=stream,
                verified_source_ref=app.simulator.source_ref,
                expected_cursor=None,
                next_cursor=None,
                events=(later,),
                now=clock.now(),
                precommit_check=lambda: None,
            )
            started = Event()
            finished = Event()

            def commit_control():
                started.set()
                app.service.store.record_conversation_outcome(
                    outcome_id=control_operation,
                    correlation_id=later.correlation_id,
                    account_ref=app.account,
                    stream=stream,
                    inbound_event_id=later.event_id,
                    outcome=ConversationOutcome.TERMINATE_CONVERSATION,
                    reason="conversation-closed",
                    resume_condition=None,
                    now=clock.now(),
                )
                finished.set()

            original_transaction = app.journal.transaction
            injected = False
            control_thread = None

            @contextmanager
            def journal_after_control_started():
                nonlocal injected, control_thread
                if not injected:
                    injected = True
                    control_thread = Thread(target=commit_control)
                    control_thread.start()
                    assert started.wait(5)
                    observed.append(finished.wait(0.5))
                with original_transaction() as db:
                    yield db

            monkeypatch.setattr(
                app.journal, "transaction", journal_after_control_started
            )
            try:
                original_apply(operation, payload)
            finally:
                monkeypatch.setattr(app.journal, "transaction", original_transaction)
                if control_thread is not None:
                    control_thread.join(timeout=5)
                    assert not control_thread.is_alive()

        monkeypatch.setattr(
            app.mission_evolution, "apply", apply_with_competing_control
        )
        decide_inbound(app, worker)
        assert observed == [False]
        assert app.mission_evolution.pending() is not None
        assert app.service.store.conversation_outcome_recorded(control_operation)
    finally:
        worker.close()


def test_control_committed_before_mission_fence_holds_intent(tmp_path, monkeypatch):
    app, worker, _, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "dialogue",
        },
    )
    try:
        original_fence = app.service.store.conversation_control_fence
        changed = []

        @contextmanager
        def fence_after_control():
            event = app.service.store.inbound_event(
                app.account, InboundKind.REPLY, "reply-1"
            )
            app.service.store.record_conversation_outcome(
                outcome_id="control-before-mission-fence",
                correlation_id=event.correlation_id,
                account_ref=app.account,
                stream=event.kind,
                inbound_event_id=event.event_id,
                outcome=ConversationOutcome.TERMINATE_CONVERSATION,
                reason="conversation-closed",
                resume_condition=None,
                now=clock.now(),
            )
            changed.append(None)
            with original_fence():
                yield

        monkeypatch.setattr(
            app.service.store, "conversation_control_fence", fence_after_control
        )
        decide_inbound(app, worker)
        assert len(changed) == 1
        with app.journal.transaction() as db:
            assert (
                db.execute(
                    "SELECT phase FROM intents ORDER BY rowid DESC LIMIT 1"
                ).fetchone()[0]
                == "held"
            )
        assert app.mission_evolution.document().proposals == ()
    finally:
        worker.close()


def test_ordinary_inbound_handoff_finishes_before_concurrent_mission_commit(
    tmp_path, monkeypatch
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        with app.journal.transaction() as db:
            research_row = db.execute(
                "SELECT operation,evidence FROM public_research_ops"
            ).fetchone()
        research_payload = app.journal.read(research_row["operation"])
        clock.advance(timedelta(minutes=1))
        no_action_operation = "ordinary-no-action"
        no_action = {
            **research_payload,
            "decision": "no_action",
            "reason": "low_value",
            "decided_at": clock.now().isoformat(),
        }
        _staged_intent(app, no_action_operation, no_action)
        confirm_operation = "concurrent-confirm"
        confirm = {
            **app.journal.read(app.wakeup_operation("wake-1")),
            "event": "concurrent-confirm",
            "correlation": confirm_operation,
            "mission_action": "confirm",
            "mission_purpose": None,
            "mission_pending_id": app.mission_evolution.pending().proposal.proposal_id,
            "mission_evidence_operation": research_row["operation"],
            "mission_evidence_digest": public_context_digest(
                (research_row["evidence"],)
            ),
            "offered_profile_digest": app.current_research_profile().digest,
            "decided_at": clock.now().isoformat(),
        }
        _staged_intent(app, confirm_operation, confirm)
        original_record = app.service.store.record_conversation_outcome
        in_effect = Event()
        release_effect = Event()
        confirm_finished = Event()
        errors = []

        def paused_record(*args, **kwargs):
            if kwargs.get("outcome_id") == no_action_operation:
                in_effect.set()
                assert release_effect.wait(5)
            return original_record(*args, **kwargs)

        def handoff(operation):
            try:
                app._materialize(operation, app.journal.read(operation))
            except Exception as error:
                errors.append(error)
            finally:
                if operation == confirm_operation:
                    confirm_finished.set()

        monkeypatch.setattr(
            app.service.store, "record_conversation_outcome", paused_record
        )
        ordinary_thread = Thread(target=handoff, args=(no_action_operation,))
        confirm_thread = Thread(target=handoff, args=(confirm_operation,))
        ordinary_thread.start()
        try:
            assert in_effect.wait(5)
            confirm_thread.start()
            assert not confirm_finished.wait(0.5)
        finally:
            release_effect.set()
            ordinary_thread.join(timeout=5)
            if confirm_thread.ident is not None:
                confirm_thread.join(timeout=5)
        assert not ordinary_thread.is_alive()
        assert not confirm_thread.is_alive()
        assert errors == []
        assert app.journal.read(no_action_operation)["phase"] == "linked"
        assert app.journal.read(confirm_operation)["phase"] == "linked"
        assert app.mission.snapshot()[1]["version"] != "mission-v1"
    finally:
        worker.close()


@pytest.mark.parametrize("decision", ["reply", "no_action", "research"])
def test_ordinary_intent_is_held_when_mission_commit_wins_handoff(
    tmp_path, monkeypatch, decision
):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        research_after_proposal(app, worker, sdk, clock)
        with app.journal.transaction() as db:
            research_row = db.execute(
                "SELECT operation,evidence FROM public_research_ops"
            ).fetchone()
        research_payload = app.journal.read(research_row["operation"])
        clock.advance(timedelta(minutes=1))
        ordinary_operation = f"stale-{decision}"
        ordinary = {
            **research_payload,
            "decision": decision,
            "event": ordinary_operation,
            "correlation": ordinary_operation,
            "decided_at": clock.now().isoformat(),
        }
        _staged_intent(app, ordinary_operation, ordinary)
        confirm_operation = "winning-confirm"
        confirm = {
            **app.journal.read(app.wakeup_operation("wake-1")),
            "event": confirm_operation,
            "correlation": confirm_operation,
            "mission_action": "confirm",
            "mission_purpose": None,
            "mission_pending_id": app.mission_evolution.pending().proposal.proposal_id,
            "mission_evidence_operation": research_row["operation"],
            "mission_evidence_digest": public_context_digest(
                (research_row["evidence"],)
            ),
            "offered_profile_digest": app.current_research_profile().digest,
            "decided_at": clock.now().isoformat(),
        }
        _staged_intent(app, confirm_operation, confirm)

        app._materialize(confirm_operation, confirm)
        assert app.journal.read(confirm_operation)["phase"] == "linked"
        app._materialize(ordinary_operation, ordinary)
        assert app.journal.read(ordinary_operation)["phase"] == "held"
        app.recover()
        assert app.journal.read(ordinary_operation)["phase"] == "held"
    finally:
        worker.close()


def test_abandonment_race_after_precheck_holds_losing_intent(tmp_path, monkeypatch):
    app, worker, _, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        run_once(app, worker)
        clock.advance(timedelta(minutes=1))
        winner = _staged_abandonment(app, "abandon-winner", "event-winner")
        loser = _staged_abandonment(app, "abandon-loser", "event-loser")
        original_apply = app.mission_evolution.apply

        def competing_apply(operation, payload):
            original_apply("abandon-winner", winner)
            original_apply(operation, payload)

        monkeypatch.setattr(app.mission_evolution, "apply", competing_apply)
        app._materialize("abandon-loser", loser)
        assert app.journal.read("abandon-loser")["phase"] == "held"
        assert app.journal.read("abandon-winner")["phase"] == "linked"
        assert len(app.mission_evolution.document().proposals) == 1
        assert (
            app.mission_evolution.document().proposals[0].status
            is MissionStatus.ABANDONED
        )
    finally:
        worker.close()


def test_stop_and_generation_revoke_abandonment(tmp_path, monkeypatch):
    app, worker, _, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        run_once(app, worker)
        clock.advance(timedelta(minutes=1))
        payload = _staged_abandonment(app, "abandon-control", "event-control")
        app.policy.emergency_stop = True
        with pytest.raises(PolicyDeniedError):
            app.mission_evolution.apply("abandon-control", payload)
        app.policy.emergency_stop = False
        with monkeypatch.context() as patch:
            patch.setattr(
                app.ledger,
                "control_generation",
                lambda: payload["generation"] + 1,
            )
            with pytest.raises(ValueError, match="source changed"):
                app.mission_evolution.apply("abandon-control", payload)
        assert app.mission_evolution.pending() is not None
        assert (
            app.mission_evolution.document().proposals[0].status
            is MissionStatus.PENDING
        )
    finally:
        worker.close()


def test_stop_during_abandonment_commit_rolls_back(tmp_path, monkeypatch):
    app, worker, _, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        run_once(app, worker)
        clock.advance(timedelta(minutes=1))
        payload = _staged_abandonment(app, "abandon-stop", "event-stop")
        original = MissionStateMachine.abandon

        def stop_after_save(machine, abandonment):
            result = original(machine, abandonment)
            app.policy.emergency_stop = True
            return result

        with monkeypatch.context() as patch:
            patch.setattr(MissionStateMachine, "abandon", stop_after_save)
            with pytest.raises(PolicyDeniedError):
                app.mission_evolution.apply("abandon-stop", payload)
        app.policy.emergency_stop = False
        assert app.mission_evolution.pending() is not None
        assert app.journal.read("abandon-stop")["phase"] == "intent"
    finally:
        worker.close()


def test_research_completion_must_follow_durable_proposal(tmp_path, monkeypatch):
    app, worker, sdk, _, clock = configured(
        tmp_path,
        {
            "decision": "mission",
            "mission_action": "propose",
            "mission_purpose": "research",
        },
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        proposal_at = app.mission_evolution.pending().proposal.decided_at
        research_after_proposal(app, worker, sdk, clock)
        with app.journal.transaction() as db:
            db.execute(
                "UPDATE public_research_completions SET completed_at=?",
                ((proposal_at - timedelta(seconds=1)).isoformat(),),
            )
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "confirm",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        assert app.mission_evolution.pending() is not None
        operation = app.wakeup_operation("wake-2")
        assert app.journal.read(operation)["phase"] == "rejected"
    finally:
        worker.close()


def test_noncanonical_root_binding_survives_enabling_evolution(tmp_path):
    mission_path = tmp_path / "mission.json"
    mission_path.write_text(
        json.dumps(
            {
                "version": "mission-v1",
                "purpose": "general_inquiry",
                "text": _MISSION_PURPOSE_TEXT[MissionPurpose.GENERAL_INQUIRY],
                "activated_at": "2026-10-17T11:00:00Z",
                "parent_version": None,
            }
        )
    )
    mission_path.chmod(0o600)
    root = PublicMissionContext(mission_path, clock=lambda: NOW + timedelta(days=1))
    root_binding = root.snapshot()[1]
    app, worker, _, _, _ = setup(
        tmp_path,
        {"decision": "no_action", "reason_category": "low_value"},
        mission=root,
    )
    try:
        run_once(app, worker)
        payload = app.journal.read(app.wakeup_operation("wake-1"))
        application_profile, _ = _profile(tmp_path)
        enabled = ThreadsRuntime(
            tmp_path / "journal.sqlite",
            tmp_path / "scheduler.sqlite",
            tmp_path / "threads.sqlite",
            model=PublicBehaviorClient(
                tmp_path / "next-worker.sock", expected_uid=os.getuid()
            ),
            transport=app.simulator,
            policy=app.policy,
            owner=app.owner,
            provider=app.provider,
            ledger=app.ledger,
            clock=app.clock,
            wakeup_policy=app.scheduler.policy,
            operator=app.operator,
            mission=PublicMissionContext(
                mission_path, clock=lambda: NOW + timedelta(days=1)
            ),
            research_client=PublicResearchClient(
                tmp_path / "unused-research.sock", expected_worker_uid=os.geteuid()
            ),
            research_profile_path=application_profile,
            research_scope="public-test",
            research_enabled=True,
            mission_evolution_enabled=True,
            enabled=True,
            live_enabled=True,
        )
        assert enabled.mission.snapshot()[1] == root_binding
        enabled.require_mission(payload)
    finally:
        worker.close()
