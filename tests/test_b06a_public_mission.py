"""Offline two-phase public mission decisions through the real worker and journal."""

from datetime import timedelta
import json
from threading import Thread
from dataclasses import replace

import pytest

from app.behavior.mission import (
    EvidenceProvenance,
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
        execute = app.research_client.execute
        replacement = PublicResearchClient(
            tmp_path / "replacement-research.sock", expected_worker_uid=os.geteuid()
        )
        monkeypatch.setattr(replacement, "execute", execute)
        app.research_client = replacement
        assert app.mission_evolution.invariants() != original_invariants
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


@pytest.mark.parametrize(
    "choice",
    [
        {"decision": "mission", "action": "propose", "purpose": "research"},
        {"decision": "mission", "action": "confirm", "purpose": None},
        {"decision": "mission", "action": "reject", "purpose": None},
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


def test_failed_final_research_rejects_resolution_intent(tmp_path, monkeypatch):
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
        sdk.proposal = {
            "decision": "mission",
            "mission_action": "reject",
            "mission_purpose": None,
        }
        clock.advance(timedelta(minutes=1))
        decide_wakeup(app, worker, clock)
        assert app.journal.read(app.wakeup_operation("wake-2"))["phase"] == "rejected"
        assert app.mission_evolution.pending() is not None
        assert app.mission.snapshot()[1]["version"] == "mission-v1"
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


def test_resolution_cannot_target_proposal_created_during_inference(
    tmp_path, monkeypatch
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
                PublicMissionChoice("confirm", None),
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
            "mission_action": "confirm",
            "mission_purpose": None,
            "decided_at": (app.clock.now() + timedelta(minutes=1)).isoformat(),
            "generation": app.ledger.control_generation(),
        }
        _staged_intent(app, "unseen-confirmation", forged)
        with pytest.raises(ValueError, match="proposal changed"):
            app.mission_evolution.apply("unseen-confirmation", forged)
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
