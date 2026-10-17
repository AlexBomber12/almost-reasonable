"""Regression fences around the INT-24 coordinator's unsafe transitions."""

from datetime import timedelta
from threading import Thread
import json

import pytest

from app.behavior.deliberation import DeliberationOutcome, DeliberationResult
from app.core.agent_proposals import NoActionProposal, NoActionReason
from app.core.public_behavior import PublicBehaviorRequest
from app.core.public_behavior_mission import PublicMissionContext
from app.integration.threads_runtime import LocalOperator, ThreadsRuntime
from app.threads.domain import InboundKind, OutboxState
from app.threads.service import PolicyDeniedError
from test_int24_runtime import NOW, run_once, setup


def no_action_result():
    return DeliberationResult(
        DeliberationOutcome.NO_ACTION,
        NoActionProposal(NoActionReason.OTHER),
        None,
        1,
        1,
    )


def mission_file(tmp_path, *, version="mission-v1", purpose=None):
    from app.behavior.mission import MissionPurpose, _MISSION_PURPOSE_TEXT

    purpose = purpose or MissionPurpose.GENERAL_INQUIRY
    path = tmp_path / "mission.json"
    path.write_text(
        json.dumps(
            {
                "version": version,
                "purpose": purpose.value,
                "text": _MISSION_PURPOSE_TEXT[purpose],
                "activated_at": NOW.isoformat(),
                "parent_version": None if version == "mission-v1" else "mission-v1",
            }
        )
    )
    path.chmod(0o600)
    return PublicMissionContext(path, clock=lambda: NOW)


def test_constructor_and_identity_guards_reject_untrusted_rebinding(tmp_path):
    app, worker, _, _, _ = setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    try:
        with pytest.raises(ValueError):
            ThreadsRuntime(
                tmp_path / "other-journal",
                tmp_path / "other-scheduler",
                tmp_path / "other-threads",
                model=object(),
                transport=app.simulator,
                policy=app.policy,
                owner=app.owner,
                provider=app.provider,
                ledger=app.ledger,
                clock=app.clock,
                wakeup_policy=app.scheduler.policy,
                operator=app.operator,
            )
        with pytest.raises(ValueError):
            ThreadsRuntime(
                tmp_path / "other-journal",
                tmp_path / "other-scheduler",
                tmp_path / "other-threads",
                model=app.model,
                transport=app.simulator,
                policy=app.policy,
                owner=app.owner,
                provider=app.provider,
                ledger=app.ledger,
                clock=app.clock,
                wakeup_policy=app.scheduler.policy,
                operator=app.operator,
                mission=object(),
            )
        with pytest.raises(PolicyDeniedError):
            LocalOperator("bad", "operator").authenticate()
        assert (
            app.authorization.current_approval(
                account_ref="other-account",
                operation_id="missing",
                approval_reference="missing",
                now=NOW,
            )
            is None
        )
        assert (
            app.attest_worker_request(
                PublicBehaviorRequest(
                    "missing", "missing", "autonomous_wakeup", (), (), 5
                )
            )
            is False
        )
        app.policy.account_authorized = False
        with pytest.raises(PolicyDeniedError):
            app.guard()
    finally:
        worker.close()


@pytest.mark.parametrize(
    "proposal",
    [
        {"decision": "reply", "content": "Нельзя отвечать без события."},
        {
            "decision": "propose_publication",
            "draft": "English publication is forbidden",
        },
    ],
)
def test_route_or_russian_gate_rejects_public_proposal(tmp_path, proposal):
    app, worker, _, http, _ = setup(tmp_path, proposal)
    try:
        run_once(app, worker)
        assert app.journal.read(app.wakeup_operation("wake-1"))["phase"] == "rejected"
        assert http.calls == []
    finally:
        worker.close()


def test_started_identity_is_single_use_and_public_control_change_holds_intent(
    tmp_path,
):
    app, worker, _, _, _ = setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    try:
        operation = app.wakeup_operation("direct-1")
        origin = {"kind": "wake", "event": "direct-1", "correlation": operation}
        app.engine.deliberate = lambda _request: no_action_result()
        values = iter((0, 1))
        app.ledger.control_generation = lambda: next(values)
        app._decide(operation, origin, ())
        assert app.journal.read(operation)["phase"] == "held"
        app._decide(operation, origin, ())
        assert app.journal.read(operation)["phase"] == "held"
        app.journal.start = lambda *_args: False
        app._decide(
            app.wakeup_operation("direct-2"),
            {"kind": "wake", "event": "direct-2", "correlation": "direct-2"},
            (),
        )
    finally:
        worker.close()


def test_mission_changes_after_model_hold_intent(tmp_path):
    mission = mission_file(tmp_path)
    app, worker, _, http, _ = setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}, mission=mission
    )
    try:
        from app.behavior.mission import MissionPurpose

        operation = app.wakeup_operation("direct-1")

        def change(_request):
            mission_file(
                tmp_path, version="mission-v2", purpose=MissionPurpose.REFLECTION
            )
            return no_action_result()

        app.engine.deliberate = change
        app._decide(
            operation,
            {"kind": "wake", "event": "direct-1", "correlation": operation},
            (),
        )
        assert app.journal.read(operation)["phase"] == "held"
        assert http.calls == []
    finally:
        worker.close()


def test_stale_source_policy_and_mission_block_handoff(tmp_path):
    app, worker, _, http, _ = setup(
        tmp_path, {"decision": "propose_publication", "draft": "Проверяю источник."}
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        payload = app.journal.read(operation)
        app.source_valid = lambda _payload: False
        with pytest.raises(PolicyDeniedError):
            app._handoff(operation, payload)
        app.source_valid = lambda _payload: True
        app.policy.scopes = frozenset()
        with pytest.raises(PolicyDeniedError):
            app._handoff(operation, payload)
        assert http.calls == []
    finally:
        worker.close()


def test_discontinued_mission_rejects_new_publication(tmp_path):
    from app.behavior.mission import MissionPurpose

    mission = mission_file(
        tmp_path, purpose=MissionPurpose.DISCONTINUE_OPTIONAL_CHANNEL
    )
    app, worker, _, http, _ = setup(
        tmp_path,
        {
            "decision": "propose_publication",
            "draft": "Не публиковать при остановленной миссии.",
        },
        mission=mission,
    )
    try:
        run_once(app, worker)
        assert app.journal.read(app.wakeup_operation("wake-1"))["phase"] == "intent"
        assert http.calls == []
    finally:
        worker.close()


def test_approval_capacity_revocation_and_missing_approval_guard(tmp_path, monkeypatch):
    import app.integration.threads_runtime as module

    app, worker, _, http, _ = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Проверяю точное одобрение."},
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        with pytest.raises(PolicyDeniedError):
            app.dispatch(operation)
        with pytest.raises(ValueError):
            app.revoke("missing")
        monkeypatch.setattr(module, "MAX_INTENTS", 0)
        with pytest.raises(ValueError):
            app.approve(operation, expires_at=NOW + timedelta(minutes=5))
        monkeypatch.setattr(module, "MAX_INTENTS", 4096)
        app.approve(operation, expires_at=NOW + timedelta(minutes=5))
        item = app.draft(operation)
        app.journal.revoke_source(item.draft_id)
        snapshot = app.authorization.current_publication_authorization(
            item=item, approval_reference=item.approval_reference, now=NOW
        )
        assert snapshot.privacy_authorized is False
        with app.journal.transaction() as db:
            db.execute("DELETE FROM approvals WHERE operation=?", (operation,))
        with pytest.raises(PolicyDeniedError):
            app.dispatch(operation)
        assert http.calls == []
    finally:
        worker.close()


def test_readiness_reports_spend_and_unlinked_draft_cannot_be_approved(tmp_path):
    def crash(point):
        if point == "after_outbox":
            raise SystemExit("simulated handoff interruption")

    app, worker, _, _, _ = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Проверяю ожидание."},
        checkpoint=crash,
    )
    try:
        with pytest.raises(SystemExit):
            run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        assert app.draft(operation).state is OutboxState.PROPOSED
        with pytest.raises(PolicyDeniedError):
            app.approve(operation, expires_at=NOW + timedelta(minutes=5))
        app.provider.is_spend_suspended = lambda: True
        assert "provider_spend_suspended" in app.readiness()
    finally:
        worker.close()


def test_changed_mission_blocks_current_authorization_and_handoff(tmp_path):
    from app.behavior.mission import MissionPurpose

    mission = mission_file(tmp_path)
    app, worker, _, http, _ = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Проверяю смену миссии."},
        mission=mission,
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        app.approve(operation, expires_at=NOW + timedelta(minutes=5))
        item = app.draft(operation)
        mission_file(tmp_path, version="mission-v2", purpose=MissionPurpose.REFLECTION)
        snapshot = app.authorization.current_publication_authorization(
            item=item, approval_reference=item.approval_reference, now=NOW
        )
        assert snapshot.privacy_authorized is False
        with pytest.raises(PolicyDeniedError, match="mission"):
            app._handoff(operation, app.journal.read(operation))
        assert http.calls == []
    finally:
        worker.close()


def test_mission_swap_during_policy_read_denies_publication(tmp_path, monkeypatch):
    from app.behavior.mission import MissionPurpose

    mission = mission_file(tmp_path)
    app, worker, _, http, _ = setup(
        tmp_path,
        {
            "decision": "propose_publication",
            "draft": "Проверяю смену миссии при проверке.",
        },
        mission=mission,
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        app.approve(operation, expires_at=NOW + timedelta(minutes=5))
        item = app.draft(operation)
        original_allowed = mission.publication_allowed

        def swap_mission():
            mission_file(
                tmp_path, version="mission-v2", purpose=MissionPurpose.REFLECTION
            )
            return original_allowed()

        monkeypatch.setattr(mission, "publication_allowed", swap_mission)
        snapshot = app.authorization.current_publication_authorization(
            item=item, approval_reference=item.approval_reference, now=NOW
        )
        assert snapshot.privacy_authorized is False
        mission_file(tmp_path)
        with pytest.raises(PolicyDeniedError, match="mission"):
            app._handoff(operation, app.journal.read(operation))
        mission_file(tmp_path)
        monkeypatch.setattr(mission, "publication_allowed", lambda: False)
        snapshot = app.authorization.current_publication_authorization(
            item=item, approval_reference=item.approval_reference, now=NOW
        )
        assert snapshot.privacy_authorized is False
        assert http.calls == []
    finally:
        worker.close()


def test_revoked_source_and_existing_outbox_are_rechecked_at_handoff(tmp_path):
    app, worker, _, http, _ = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Проверяю повторную передачу."},
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        item = app.draft(operation)
        payload = app.journal.read(operation)
        app.journal.revoke_source(item.draft_id)
        with pytest.raises(PolicyDeniedError, match="revoked"):
            app._handoff(operation, payload)
        with app.journal.transaction() as db:
            db.execute("UPDATE sources SET active=1 WHERE draft=?", (item.draft_id,))
        app._handoff(operation, payload)
        assert app.draft(operation).state is OutboxState.AWAITING_APPROVAL
        assert http.calls == []
    finally:
        worker.close()


def test_reply_source_disappearing_after_proposal_denies_handoff(tmp_path):
    app, worker, _, http, _ = setup(
        tmp_path, {"decision": "reply", "content": "Спасибо за ответ."}
    )
    try:
        app.poll(InboundKind.REPLY, correlation_id="poll-1")
        thread = Thread(target=worker.serve_once)
        thread.start()
        operations = app.process_inbound(InboundKind.REPLY)
        thread.join(timeout=5)
        operation = operations[0]
        app.source_valid = lambda _payload: True
        app.service.store.inbound_event = lambda *_args: None
        with pytest.raises(PolicyDeniedError, match="source unavailable"):
            app._handoff(operation, app.journal.read(operation))
        assert len(http.calls) == 1
    finally:
        worker.close()


def test_concurrent_oversized_claim_never_contacts_worker(tmp_path):
    app, worker, sdk, http, _ = setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    try:
        app.journal.start = lambda *_args: False
        operation = app.wakeup_operation("large-1")
        app._decide(
            operation,
            {"kind": "wake", "event": "large-1", "correlation": operation},
            ("Большой текст. " * 300,),
        )
        assert sdk.calls == http.calls == []
    finally:
        worker.close()


def test_stage0_journal_and_foreign_intents_cannot_authorize_public_runtime(tmp_path):
    from app.integration.threads_stage0 import Stage0Journal

    Stage0Journal(tmp_path / "journal.sqlite", "account-1")
    with pytest.raises(ValueError, match="Stage 0 journal"):
        setup(tmp_path, {"decision": "no_action", "reason_category": "other"})


def test_runtime_marker_and_intent_version_are_verified_on_restart(tmp_path):
    app, worker, _, http, _ = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Проверяю границу схемы."},
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        app.approve(operation, expires_at=NOW + timedelta(minutes=5))
        item = app.draft(operation)
        payload = app.journal.read(operation)
        with pytest.raises(PolicyDeniedError, match="foreign"):
            app._handoff(operation, payload | {"runtime_version": "stage0"})
        with app.journal.transaction() as db:
            db.execute(
                "UPDATE intents SET payload=? WHERE operation=?",
                (json.dumps({**payload, "runtime_version": "stage0"}), operation),
            )
        assert (
            app.authorization.current_publication_authorization(
                item=item, approval_reference=item.approval_reference, now=NOW
            ).privacy_authorized
            is False
        )
        assert http.calls == []
        with app.journal.transaction() as db:
            db.execute("UPDATE public_runtime_schema SET version='foreign'")
        with pytest.raises(ValueError, match="invalid public runtime journal"):
            setup(tmp_path, {"decision": "no_action", "reason_category": "other"})
    finally:
        worker.close()


def test_privacy_generation_change_during_authorization_snapshot_denies_effect(
    tmp_path,
):
    app, worker, _, http, _ = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Проверяю отзыв согласия."},
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        app.approve(operation, expires_at=NOW + timedelta(minutes=5))
        item = app.draft(operation)
        generation = app.ledger.control_generation()
        values = iter((generation, generation + 1))
        app.ledger.control_generation = lambda: next(values)
        snapshot = app.authorization.current_publication_authorization(
            item=item, approval_reference=item.approval_reference, now=NOW
        )
        assert snapshot.privacy_authorized is False
        assert http.calls == []
    finally:
        worker.close()


@pytest.mark.parametrize("control", ["owner", "spend"])
def test_control_change_during_authorization_snapshot_denies_effect(
    tmp_path, monkeypatch, control
):
    app, worker, _, http, _ = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Проверяю поздний стоп."},
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        app.approve(operation, expires_at=NOW + timedelta(minutes=5))
        item = app.draft(operation)
        original_source_valid = app.source_valid

        def activate_control(payload):
            if control == "owner":
                with app.owner._connection() as db:
                    db.execute("UPDATE owner_stop SET active=1")
            else:
                monkeypatch.setattr(app.provider, "is_spend_suspended", lambda: True)
            return original_source_valid(payload)

        monkeypatch.setattr(app, "source_valid", activate_control)
        with pytest.raises(PolicyDeniedError, match="controls"):
            app.authorization.current_publication_authorization(
                item=item, approval_reference=item.approval_reference, now=NOW
            )
        assert http.calls == []
    finally:
        worker.close()


def test_expired_ready_approval_can_be_renewed_for_exact_same_draft(tmp_path):
    app, worker, _, http, clock = setup(
        tmp_path,
        {
            "decision": "propose_publication",
            "draft": "Обновляю срок точного одобрения.",
        },
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        app.approve(operation, expires_at=NOW + timedelta(minutes=1))
        old_reference = app.draft(operation).approval_reference
        clock.advance(timedelta(minutes=2))
        renewed = app.approve(operation, expires_at=clock.now() + timedelta(minutes=5))
        assert renewed.state is OutboxState.READY
        assert renewed.approval_reference != old_reference
        assert app.dispatch(operation).state is OutboxState.CONFIRMED
        assert app.draft(operation).approval_reference == renewed.approval_reference
        assert len([call for call in http.calls if call[0] == "POST"]) == 2
    finally:
        worker.close()


def test_renewed_approval_reference_can_be_revoked(tmp_path):
    app, worker, _, http, clock = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Отзываю новое одобрение."},
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        app.approve(operation, expires_at=NOW + timedelta(minutes=1))
        clock.advance(timedelta(minutes=2))
        renewed = app.approve(operation, expires_at=clock.now() + timedelta(minutes=5))
        app.revoke(renewed.approval_reference)
        with pytest.raises(PolicyDeniedError, match="approval unavailable"):
            app.dispatch(operation)
        assert http.calls == []
    finally:
        worker.close()


def test_interrupted_sending_is_recoverable_without_retransmission(
    tmp_path, monkeypatch
):
    app, worker, _, http, _ = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Восстанавливаю отправку."},
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        app.approve(operation, expires_at=NOW + timedelta(minutes=5))
        original = app.service.store.update_sending_evidence

        def crash_after_marker(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[1] == "publish_dispatch_started":
                raise SystemExit("simulated process crash")
            return result

        monkeypatch.setattr(
            app.service.store, "update_sending_evidence", crash_after_marker
        )
        with pytest.raises(SystemExit, match="process crash"):
            app.dispatch(operation)
        assert app.draft(operation).state is OutboxState.SENDING
        writes = len([call for call in http.calls if call[0] == "POST"])
        assert app.recover_sending(operation).state is OutboxState.UNKNOWN
        assert len([call for call in http.calls if call[0] == "POST"]) == writes
    finally:
        worker.close()


def test_interrupted_reconciliation_claim_has_purpose_bound_operator_recovery(
    tmp_path,
):
    app, worker, _, http, _ = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Восстанавливаю сверку."},
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        app.approve(operation, expires_at=NOW + timedelta(minutes=5))
        http.fail_readback = True
        assert app.dispatch(operation).state is OutboxState.UNKNOWN
        writes = len([call for call in http.calls if call[0] == "POST"])
        app.service.store.claim_reconciliation(
            operation_id=operation,
            correlation_id="interrupted-claim",
            account_ref=app.account,
            actor_reference="local-operator",
            now=NOW,
        )
        with app.owner._connection() as db:
            db.execute("UPDATE owner_stop SET active=1")
        recovery = app.approve_reconciliation_recovery(
            operation, expires_at=NOW + timedelta(minutes=5)
        )
        assert recovery.state is OutboxState.UNKNOWN
        assert (
            app.recover_reconciliation_claim(
                operation, recovery.approval_reference
            ).state
            is OutboxState.UNKNOWN
        )
        assert len([call for call in http.calls if call[0] == "POST"]) == writes
    finally:
        worker.close()


def test_public_recovery_rejects_foreign_target_and_requires_unknown_state(
    tmp_path, monkeypatch
):
    import app.integration.threads_runtime as module

    app, worker, _, http, _ = setup(
        tmp_path,
        {
            "decision": "propose_publication",
            "draft": "Проверяю границы восстановления.",
        },
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        original_read = app.journal.read
        monkeypatch.setattr(
            app.journal,
            "read",
            lambda value: {**original_read(value), "runtime_version": "foreign"},
        )
        with pytest.raises(PolicyDeniedError, match="not linked"):
            app.recover_sending(operation)
        monkeypatch.setattr(app.journal, "read", original_read)
        with pytest.raises(PolicyDeniedError, match="unknown state"):
            app.approve_reconciliation_recovery(
                operation, expires_at=NOW + timedelta(minutes=5)
            )
        http.fail_readback = True
        app.approve(operation, expires_at=NOW + timedelta(minutes=5))
        assert app.dispatch(operation).state is OutboxState.UNKNOWN
        monkeypatch.setattr(module, "MAX_INTENTS", 0)
        with pytest.raises(ValueError, match="capacity"):
            app.approve_reconciliation_recovery(
                operation, expires_at=NOW + timedelta(minutes=5)
            )
        with pytest.raises(PolicyDeniedError, match="approval unavailable"):
            app.recover_reconciliation_claim(operation, "missing-reference")
    finally:
        worker.close()


def test_approval_rejects_completed_draft_and_missing_current_record(
    tmp_path, monkeypatch
):
    app, worker, _, http, _ = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Проверяю запись одобрения."},
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        app.approve(operation, expires_at=NOW + timedelta(minutes=5))
        monkeypatch.setattr(app.authorization, "current_approval", lambda **_: None)
        with pytest.raises(PolicyDeniedError, match="approval unavailable"):
            app.dispatch(operation)
        monkeypatch.undo()
        assert app.dispatch(operation).state is OutboxState.CONFIRMED
        with pytest.raises(PolicyDeniedError, match="cannot be approved"):
            app.approve(operation, expires_at=NOW + timedelta(minutes=10))
        assert len([call for call in http.calls if call[0] == "POST"]) == 2
    finally:
        worker.close()
