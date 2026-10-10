from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from app.threads.domain import (
    AccountPolicy,
    Approval,
    OutboxState,
    PublicationType,
    SourceProvenance,
    Stage,
    issue_approval,
)
from app.threads.service import PolicyDeniedError, ThreadsService
from app.threads.simulator import (
    Fault,
    ReconciliationResult,
    SimulatedCrash,
    ThreadsSimulator,
)
from app.threads.store import (
    DuplicateOperationError,
    ThreadsStore,
    ThreadsStoreError,
    UnknownPublicationRiskError,
)


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def make_policy(**changes):
    result = AccountPolicy(
        account_ref="account-1",
        stage=Stage.SIMULATION,
        simulation_enabled=True,
        account_authorized=True,
        scopes=frozenset(
            {
                "threads_basic",
                "threads_content_publish",
                "threads_read_replies",
                "threads_manage_mentions",
            }
        ),
        privacy_gate_available=True,
        russian_output_gate_available=True,
        publication_capability_enabled=True,
    )
    for name, value in changes.items():
        setattr(result, name, value)
    return result


def make_service(tmp_path, **policy_changes):
    policy = make_policy(**policy_changes)
    simulator = ThreadsSimulator(account_ref=policy.account_ref)
    return ThreadsService(ThreadsStore(tmp_path / "threads.sqlite3"), simulator, policy)


def approve(operation_id="operation-1", **changes):
    values = {
        "reference": f"approval-{operation_id}",
        "account_ref": "account-1",
        "operation_id": operation_id,
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(changes)
    return issue_approval(**values)


def propose(service, operation_id="operation-1", **changes):
    values = {
        "operation_id": operation_id,
        "publication_type": PublicationType.POST,
        "draft_id": f"draft-{operation_id}",
        "content": f"Проверяем публикацию {operation_id}",
        "source_provenance": SourceProvenance.PUBLIC,
        "privacy_authorized": True,
        "russian_output_authorized": True,
        "correlation_id": f"correlation-{operation_id}",
        "now": NOW,
    }
    values.update(changes)
    return service.propose(**values)


def ready(service, operation_id="operation-1", **changes):
    item, created = propose(service, operation_id, **changes)
    assert created
    service.submit_for_approval(operation_id, now=NOW)
    approval = approve(operation_id)
    return service.authorize(operation_id, approval, now=NOW), approval


def test_database_is_versioned_wal_and_reopens_durably(tmp_path):
    path = tmp_path / "nested" / "threads.sqlite3"
    store = ThreadsStore(path)
    service = ThreadsService(
        store, ThreadsSimulator(account_ref="account-1"), make_policy()
    )
    item, created = propose(service)
    assert created and item.state is OutboxState.PROPOSED
    reopened = ThreadsStore(path)
    assert reopened.get("operation-1") == item
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        connection.execute("UPDATE threads_schema_metadata SET version=99")
    with pytest.raises(ThreadsStoreError, match="unsupported"):
        ThreadsStore(path)


def test_proposal_identity_is_idempotent_but_conflicts_fail(tmp_path):
    service = make_service(tmp_path)
    first, created = propose(service)
    repeated, repeated_created = propose(service)
    assert created is True
    assert repeated_created is False
    assert repeated == first
    with pytest.raises(DuplicateOperationError):
        propose(service, content="Другое содержание")


@pytest.mark.parametrize(
    "changes",
    [
        {"operation_id": "bad value"},
        {"publication_type": "post"},
        {"source_provenance": "public"},
        {"privacy_authorized": False},
        {"privacy_authorized": 1},
        {"russian_output_authorized": False},
        {"russian_output_authorized": 1},
        {"source_provenance": SourceProvenance.PRIVATE_UNAUTHORIZED},
        {"now": datetime(2026, 1, 1)},
        {"publication_type": PublicationType.REPLY},
        {"parent_message_id": "parent-1"},
        {"replacement_of": "operation-old"},
        {"duplicate_risk_approval_ref": "approval-risk"},
    ],
)
def test_proposal_boundary_rejects_malformed_or_unauthorized_input(tmp_path, changes):
    service = make_service(tmp_path)
    if changes == {"replacement_of": "operation-old"}:
        with pytest.raises(ThreadsStoreError):
            propose(service, **changes)
    else:
        with pytest.raises((TypeError, ValueError, PolicyDeniedError)):
            propose(service, **changes)


def test_reply_requires_parent_and_enters_same_outbox(tmp_path):
    service = make_service(tmp_path)
    item, _ = propose(
        service,
        publication_type=PublicationType.REPLY,
        parent_message_id="thread-parent-1",
    )
    assert item.publication_type is PublicationType.REPLY
    assert item.parent_message_id == "thread-parent-1"


def test_approval_is_separate_and_rechecked_for_mismatch_revocation_and_expiry(
    tmp_path,
):
    service = make_service(tmp_path)
    propose(service)
    service.submit_for_approval("operation-1", now=NOW)
    forged = Approval(
        "approval-forged",
        "account-1",
        "operation-1",
        NOW + timedelta(hours=1),
        False,
        False,
        object(),
    )
    cases = [
        forged,
        approve(account_ref="account-2"),
        approve(revoked=True),
        approve(expires_at=NOW),
    ]
    for approval in cases:
        with pytest.raises(ValueError):
            service.authorize("operation-1", approval, now=NOW)


@pytest.mark.parametrize(
    "policy_change, message",
    [
        ({"stage": Stage.DISABLED}, "stage"),
        ({"russian_output_gate_available": False}, "Russian"),
        ({"privacy_gate_available": False}, "privacy"),
        ({"publication_capability_enabled": False}, "capability"),
        ({"emergency_stop": True}, "stop"),
    ],
)
def test_policy_gates_block_readiness(tmp_path, policy_change, message):
    service = make_service(tmp_path, **policy_change)
    propose(service)
    service.submit_for_approval("operation-1", now=NOW)
    with pytest.raises(PolicyDeniedError, match=message):
        service.authorize("operation-1", approve(), now=NOW)


def test_outbound_account_mismatch_fails_closed(tmp_path):
    service = make_service(tmp_path)
    propose(service)
    service.submit_for_approval("operation-1", now=NOW)
    service.policy.account_ref = "account-2"
    with pytest.raises(PolicyDeniedError, match="account mismatch"):
        service.authorize("operation-1", approve(), now=NOW)


def test_happy_path_preserves_multistep_provider_evidence(tmp_path):
    service = make_service(tmp_path)
    item, approval = ready(service)
    assert item.state is OutboxState.READY
    confirmed = service.dispatch("operation-1", approval, now=NOW)
    assert confirmed.state is OutboxState.CONFIRMED
    assert confirmed.provider_container_id == "container-operation-1"
    assert confirmed.provider_publication_id == "thread-operation-1"
    assert confirmed.attempt_count == 1
    assert service.simulator.trace == [
        ("create_container", "operation-1"),
        ("publish_container", "container-operation-1"),
    ]
    assert service.simulator.network_calls == 0
    with pytest.raises(ThreadsStoreError):
        service.dispatch("operation-1", approval, now=NOW)


@pytest.mark.parametrize(
    "stage, fault, expected_state, evidence",
    [
        ("container", Fault.BEFORE_CONTAINER, OutboxState.READY, "not_transmitted"),
        ("container", Fault.DURING_CONTAINER, OutboxState.UNKNOWN, "unknown"),
        ("container", Fault.AFTER_CONTAINER, OutboxState.UNKNOWN, "response_lost"),
        (
            "container",
            Fault.CONTAINER_REJECTED,
            OutboxState.FAILED_TERMINAL,
            "rejected",
        ),
        ("publish", Fault.BEFORE_PUBLISH, OutboxState.READY, "not_transmitted"),
        ("publish", Fault.DURING_PUBLISH, OutboxState.UNKNOWN, "unknown"),
        (
            "publish",
            Fault.AFTER_PUBLISH_SUCCESS,
            OutboxState.UNKNOWN,
            "response_lost",
        ),
        ("publish", Fault.RESPONSE_PARSE, OutboxState.UNKNOWN, "malformed"),
        (
            "publish",
            Fault.PUBLISH_REJECTED,
            OutboxState.FAILED_TERMINAL,
            "rejected",
        ),
    ],
)
def test_faults_are_classified_at_every_provider_boundary(
    tmp_path, stage, fault, expected_state, evidence
):
    service = make_service(tmp_path)
    _, approval = ready(service)
    service.simulator.set_fault("operation-1", stage, fault)
    item = service.dispatch("operation-1", approval, now=NOW)
    assert item.state is expected_state
    assert evidence in item.dispatch_evidence
    if expected_state is OutboxState.UNKNOWN:
        assert item.reconciliation_status == "required"


def test_proven_pre_dispatch_retry_is_bounded_and_reuses_container(tmp_path):
    service = make_service(tmp_path)
    _, approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.BEFORE_PUBLISH)
    first = service.dispatch("operation-1", approval, now=NOW)
    assert first.state is OutboxState.READY
    assert first.provider_container_id == "container-operation-1"
    second = service.dispatch("operation-1", approval, now=NOW)
    assert second.state is OutboxState.FAILED_TERMINAL
    assert service.simulator.trace.count(("create_container", "operation-1")) == 1


@pytest.mark.parametrize(
    "fault, expected",
    [
        (Fault.CRASH_BEFORE_TRANSMISSION, OutboxState.READY),
        (Fault.CRASH_AFTER_TRANSMISSION, OutboxState.UNKNOWN),
    ],
)
def test_restart_recovery_uses_durable_dispatch_evidence(tmp_path, fault, expected):
    service = make_service(tmp_path)
    _, approval = ready(service)
    service.simulator.set_fault("operation-1", "container", fault)
    with pytest.raises(SimulatedCrash):
        service.dispatch("operation-1", approval, now=NOW)
    reopened = ThreadsService(
        ThreadsStore(service.store.database), service.simulator, service.policy
    )
    recovered = reopened.recover_sending("operation-1", now=NOW)
    assert recovered.state is expected
    with pytest.raises(ThreadsStoreError):
        reopened.recover_sending("operation-1", now=NOW)


def test_stop_before_send_blocks_and_stop_in_flight_cannot_revoke_success(tmp_path):
    service = make_service(tmp_path)
    _, approval = ready(service)
    service.policy.emergency_stop = True
    with pytest.raises(PolicyDeniedError, match="stop"):
        service.dispatch("operation-1", approval, now=NOW)
    service.policy.emergency_stop = False
    service.simulator.set_fault("operation-1", "publish", Fault.STOP_IN_FLIGHT)
    service.simulator.on_in_flight = lambda: setattr(
        service.policy, "emergency_stop", True
    )
    result = service.dispatch("operation-1", approval, now=NOW)
    assert result.state is OutboxState.CONFIRMED
    assert service.policy.emergency_stop is True


@pytest.mark.parametrize(
    "result, expected",
    [
        (ReconciliationResult.CONFIRMED, OutboxState.CONFIRMED),
        (ReconciliationResult.REJECTED, OutboxState.FAILED_TERMINAL),
        (ReconciliationResult.UNRESOLVED, OutboxState.UNKNOWN),
    ],
)
def test_reconciliation_requires_verified_result(tmp_path, result, expected):
    service = make_service(tmp_path)
    _, approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    assert (
        service.dispatch("operation-1", approval, now=NOW).state is OutboxState.UNKNOWN
    )
    service.simulator.reconciliation["operation-1"] = result
    reconciled = service.reconcile("operation-1", now=NOW)
    assert reconciled.state is expected
    if expected is OutboxState.UNKNOWN:
        assert reconciled.reconciliation_status == "unresolved"


def test_reconciliation_failure_preserves_unknown_and_abandonment_is_permanent(
    tmp_path,
):
    service = make_service(tmp_path)
    _, approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", approval, now=NOW)
    service.simulator.set_fault("operation-1", "reconcile", Fault.RECONCILIATION)
    assert service.reconcile("operation-1", now=NOW).state is OutboxState.UNKNOWN
    abandoned = service.abandon_unknown("operation-1", now=NOW)
    assert abandoned.state is OutboxState.UNKNOWN_ABANDONED
    assert service.store.unresolved_for_account("account-1") == (abandoned,)
    with pytest.raises(ThreadsStoreError):
        service.reconcile("operation-1", now=NOW)
    with pytest.raises(ThreadsStoreError):
        service.dispatch("operation-1", approval, now=NOW)


def test_unknown_pauses_account_and_exact_identity_cannot_be_evaded(tmp_path):
    service = make_service(tmp_path)
    _, first_approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", first_approval, now=NOW)
    with pytest.raises(UnknownPublicationRiskError):
        propose(
            service,
            "operation-2",
            content="Проверяем публикацию operation-1",
        )
    propose(service, "operation-3")
    service.submit_for_approval("operation-3", now=NOW)
    with pytest.raises(PolicyDeniedError, match="paused"):
        service.authorize("operation-3", approve("operation-3"), now=NOW)


def test_exceptional_replacement_requires_separate_duplicate_risk_ack(tmp_path):
    service = make_service(tmp_path)
    _, first_approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", first_approval, now=NOW)
    no_ack = approve("operation-2")
    with pytest.raises(ValueError, match="duplicate risk"):
        propose(
            service,
            "operation-2",
            replacement_of="operation-1",
            replacement_approval=no_ack,
        )
    risk_approval = approve("operation-2", duplicate_risk_acknowledged=True)
    replacement, _ = propose(
        service,
        "operation-2",
        replacement_of="operation-1",
        replacement_approval=risk_approval,
    )
    assert replacement.replacement_of == "operation-1"
    service.submit_for_approval("operation-2", now=NOW)
    assert (
        service.authorize("operation-2", risk_approval, now=NOW).state
        is OutboxState.READY
    )


def test_replacement_rejects_resolved_target_and_missing_authenticated_ack(tmp_path):
    service = make_service(tmp_path)
    propose(service)
    risk_approval = approve("operation-2", duplicate_risk_acknowledged=True)
    with pytest.raises(PolicyDeniedError, match="not unresolved"):
        propose(
            service,
            "operation-2",
            replacement_of="operation-1",
            replacement_approval=risk_approval,
        )
    service.submit_for_approval("operation-1", now=NOW)
    first_approval = approve()
    service.authorize("operation-1", first_approval, now=NOW)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", first_approval, now=NOW)
    with pytest.raises(PolicyDeniedError, match="authenticated approval"):
        propose(service, "operation-2", replacement_of="operation-1")


def test_duplicate_fingerprint_is_a_bounded_warning_not_semantic_proof(tmp_path):
    service = make_service(tmp_path)
    propose(service)
    propose(
        service,
        "operation-2",
        content="  ПРОВЕРЯЕМ   публикацию operation-1 ",
    )
    assert service.store.likely_duplicates("operation-2") == ("operation-1",)


def test_cancel_and_invalid_transitions_are_explicit(tmp_path):
    service = make_service(tmp_path)
    propose(service)
    assert service.cancel("operation-1", now=NOW).state is OutboxState.CANCELLED
    with pytest.raises(ThreadsStoreError, match="invalid outbox transition"):
        service.submit_for_approval("operation-1", now=NOW)
    with pytest.raises(ValueError):
        service.store.transition("operation-1", "ready", reason="bad", now=NOW)
    with pytest.raises(ThreadsStoreError, match="unknown operation"):
        service.store.get("missing")


def test_store_rejects_invalid_transition_metadata_and_state_specific_updates(tmp_path):
    service = make_service(tmp_path)
    propose(service)
    with pytest.raises(ValueError, match="duplicate-risk approval"):
        service.store.create_proposal(
            operation_id="operation-2",
            account_ref="account-1",
            publication_type=PublicationType.POST,
            draft_id="draft-2",
            content="Новая публикация",
            source_provenance=SourceProvenance.PUBLIC,
            privacy_authorized=True,
            russian_output_authorized=True,
            correlation_id="correlation-2",
            now=NOW,
            duplicate_risk_approval_ref="approval-risk",
        )
    with pytest.raises(ValueError, match="approval status"):
        service.store.transition(
            "operation-1",
            OutboxState.AWAITING_APPROVAL,
            reason="review",
            now=NOW,
            approval_status="forged",
        )
    with pytest.raises(ThreadsStoreError, match="unknown operation"):
        service.store.transition(
            "missing",
            OutboxState.AWAITING_APPROVAL,
            reason="review",
            now=NOW,
        )
    with pytest.raises(ThreadsStoreError, match="sending state"):
        service.store.update_sending_evidence("operation-1", "evidence", now=NOW)
    with pytest.raises(ThreadsStoreError, match="unknown state"):
        service.store.note_reconciliation("operation-1", "unresolved", now=NOW)


def test_stage_three_readiness_explicitly_reports_missing_live_transport(tmp_path):
    service = make_service(tmp_path)
    assert service.stage_3_readiness() == ("live Threads transport is not implemented",)
    assert service.policy.stage is Stage.SIMULATION


def test_service_rejects_simulator_for_another_account(tmp_path):
    with pytest.raises(ValueError, match="does not match"):
        ThreadsService(
            ThreadsStore(tmp_path / "threads.sqlite3"),
            ThreadsSimulator(account_ref="account-2"),
            make_policy(),
        )


def test_simulator_rejects_unknown_fault_type_and_handles_inflight_without_hook(
    tmp_path,
):
    service = make_service(tmp_path)
    with pytest.raises(ValueError, match="fault"):
        service.simulator.set_fault("operation-1", "publish", "none")
    _, approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.STOP_IN_FLIGHT)
    assert service.dispatch("operation-1", approval, now=NOW).state is (
        OutboxState.CONFIRMED
    )
