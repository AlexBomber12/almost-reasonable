from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from app.threads import store as store_module
from app.threads.domain import (
    AccountPolicy,
    Approval,
    ApprovalPurpose,
    InboundKind,
    OutboxState,
    PublicationAuthorizationSnapshot,
    PublicationType,
    SourceProvenance,
    Stage,
    content_fingerprint,
    issue_approval,
)
from app.threads.service import PolicyDeniedError, ThreadsService
from app.threads.simulator import (
    Fault,
    ProviderFailure,
    ReconciliationResult,
    SimulatedCrash,
    SimulationAuthorizationProvider,
    ThreadsSimulator,
    simulated_provider_id,
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
    authorization_provider = SimulationAuthorizationProvider()
    return ThreadsService(
        ThreadsStore(tmp_path / "threads.sqlite3"),
        simulator,
        policy,
        utc_now=lambda: NOW,
        authorization_provider=authorization_provider,
    )


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
    result = service.propose(**values)
    provider = service.authorization_provider
    if isinstance(provider, SimulationAuthorizationProvider):
        provider.set_privacy(service.policy.account_ref, operation_id, True)
    return result


def trust_approval(service, approval):
    provider = service.authorization_provider
    assert isinstance(provider, SimulationAuthorizationProvider)
    provider.set_approval(approval)
    return approval


def ready(service, operation_id="operation-1", **changes):
    item, created = propose(service, operation_id, **changes)
    assert created
    service.submit_for_approval(operation_id, now=NOW)
    approval = trust_approval(service, approve(operation_id))
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


def test_initial_schema_and_version_row_are_one_transaction(tmp_path, monkeypatch):
    path = tmp_path / "threads.sqlite3"
    monkeypatch.setattr(
        store_module,
        "_SCHEMA",
        f"{store_module._SCHEMA}\nCREATE TABLE invalid(",
    )
    with pytest.raises(sqlite3.OperationalError):
        ThreadsStore(path)
    with sqlite3.connect(path) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    assert tables == []


def test_version_one_database_migrates_authorization_and_correlation_schema(tmp_path):
    path = tmp_path / "threads.sqlite3"
    store = ThreadsStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO threads_inbound_events VALUES "
            "('event-legacy', 'correlation-old', 'account-1', "
            "'meta-simulator-v1', 'reply', 'conversation-legacy', "
            "'parent-legacy', ?, 'Публичное сообщение', ?, NULL)",
            (NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            "INSERT INTO threads_conversation_outcomes VALUES "
            "('outcome-legacy', 'correlation-old', 'account-1', 'reply', "
            "'event-legacy', 'no_action', 'legacy-reason', NULL, ?)",
            (NOW.isoformat(),),
        )
        connection.execute("DROP TABLE threads_account_effect_locks")
        connection.execute("DROP TABLE threads_unknown_effect_locks")
        connection.execute("DROP TABLE threads_operator_decisions")
        connection.execute("DROP TABLE threads_poll_policy_denials")
        connection.execute("DROP TABLE threads_poll_actions")
        connection.execute(
            "ALTER TABLE threads_inbound_events DROP COLUMN correlation_id"
        )
        connection.execute(
            "ALTER TABLE threads_conversation_outcomes DROP COLUMN correlation_id"
        )
        connection.execute("UPDATE threads_schema_metadata SET version=1")
    ThreadsStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version FROM threads_schema_metadata"
        ).fetchone() == (6,)
        assert connection.execute(
            "SELECT correlation_id FROM threads_inbound_events "
            "WHERE event_id='event-legacy'"
        ).fetchone() == ("legacy-event-legacy",)
        assert connection.execute(
            "SELECT processed_at FROM threads_inbound_events "
            "WHERE event_id='event-legacy'"
        ).fetchone() == (NOW.isoformat(),)
        assert connection.execute(
            "SELECT correlation_id FROM threads_conversation_outcomes "
            "WHERE outcome_id='outcome-legacy'"
        ).fetchone() == ("legacy-event-legacy",)
        inbound_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(threads_inbound_events)")
        }
        outcome_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(threads_conversation_outcomes)"
            )
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "correlation_id" in inbound_columns
        assert "processed_at" in inbound_columns
    assert "correlation_id" in outcome_columns
    assert "threads_unknown_effect_locks" in tables
    assert "threads_account_effect_locks" in tables
    assert "threads_operator_decisions" in tables
    assert "threads_poll_policy_denials" in tables
    assert "threads_poll_actions" in tables
    assert store.pending_inbound_events("account-1", InboundKind.REPLY) == ()


def test_existing_database_requires_version_metadata_row(tmp_path):
    path = tmp_path / "threads.sqlite3"
    ThreadsStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM threads_schema_metadata")
    with pytest.raises(ThreadsStoreError, match="metadata is missing"):
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


def test_ready_transition_rechecks_authorization_inside_write_transaction(
    tmp_path, monkeypatch
):
    service = make_service(tmp_path)
    propose(service)
    service.submit_for_approval("operation-1", now=NOW)
    approval = trust_approval(service, approve())
    provider = service.authorization_provider
    current_snapshot = provider.current_publication_authorization
    checks = 0

    def revoke_at_precommit(**kwargs):
        nonlocal checks
        checks += 1
        if checks == 2:
            provider.set_approval(approve(revoked=True))
        return current_snapshot(**kwargs)

    monkeypatch.setattr(
        provider, "current_publication_authorization", revoke_at_precommit
    )
    with pytest.raises(ValueError, match="revoked"):
        service.authorize("operation-1", approval, now=NOW)

    assert checks == 2
    assert service.store.get("operation-1").state is OutboxState.AWAITING_APPROVAL
    with sqlite3.connect(service.store.database) as connection:
        assert connection.execute(
            "SELECT reason FROM threads_outbox_transitions "
            "WHERE operation_id='operation-1' ORDER BY revision DESC LIMIT 1"
        ).fetchone() == (
            f"authorize_policy_denied_approval-"
            f"{content_fingerprint(approval.reference)[:16]}",
        )


@pytest.mark.parametrize(
    "policy_change, message",
    [
        ({"stage": Stage.DISABLED}, "stage"),
        ({"stage": Stage.STAGE_3}, "live Threads transport"),
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


def test_shared_store_lifecycle_methods_reject_another_accounts_operation(tmp_path):
    owner = make_service(tmp_path, account_ref="account-2")
    other = make_service(tmp_path, account_ref="account-1")
    propose(owner)

    with pytest.raises(PolicyDeniedError, match="account mismatch"):
        other.submit_for_approval("operation-1", now=NOW)
    with pytest.raises(PolicyDeniedError, match="account mismatch"):
        other.cancel("operation-1", now=NOW)
    assert owner.store.get("operation-1").state is OutboxState.PROPOSED

    owner.submit_for_approval("operation-1", now=NOW)
    foreign_item = owner.store.get("operation-1")
    with sqlite3.connect(owner.store.database) as connection:
        foreign_transition_count = connection.execute(
            "SELECT COUNT(*) FROM threads_outbox_transitions WHERE operation_id=?",
            ("operation-1",),
        ).fetchone()[0]
    with pytest.raises(PolicyDeniedError, match="account mismatch"):
        other.authorize("operation-1", approve(), now=NOW)
    assert owner.store.get("operation-1") == foreign_item
    with sqlite3.connect(owner.store.database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM threads_outbox_transitions WHERE operation_id=?",
                ("operation-1",),
            ).fetchone()[0]
            == foreign_transition_count
        )

    approval = trust_approval(owner, approve(account_ref="account-2"))
    owner.authorize("operation-1", approval, now=NOW)
    owner.simulator.set_fault(
        "operation-1", "container", Fault.CRASH_AFTER_TRANSMISSION
    )
    with pytest.raises(SimulatedCrash):
        owner.dispatch("operation-1", approval, now=NOW)
    with pytest.raises(PolicyDeniedError, match="account mismatch"):
        other.recover_sending("operation-1", now=NOW)
    assert owner.store.get("operation-1").state is OutboxState.SENDING

    assert owner.recover_sending("operation-1", now=NOW).state is OutboxState.UNKNOWN
    with pytest.raises(PolicyDeniedError, match="account mismatch"):
        other.reconcile("operation-1", now=NOW)
    with pytest.raises(PolicyDeniedError, match="account mismatch"):
        other.abandon_unknown("operation-1", approval, now=NOW)
    assert owner.store.get("operation-1").state is OutboxState.UNKNOWN
    assert other.simulator.trace == []


def test_missing_current_authorization_provider_fails_closed(tmp_path):
    simulator = ThreadsSimulator(account_ref="account-1")
    service = ThreadsService(
        ThreadsStore(tmp_path / "threads.sqlite3"), simulator, make_policy()
    )
    propose(service)
    service.submit_for_approval("operation-1", now=NOW)
    with pytest.raises(PolicyDeniedError, match="provider is unavailable"):
        service.authorize("operation-1", approve(), now=NOW)
    assert service.store.get("operation-1").state is OutboxState.AWAITING_APPROVAL
    assert simulator.trace == []
    assert simulator.network_calls == 0


def test_unregistered_or_unavailable_current_approval_fails_closed(tmp_path):
    service = make_service(tmp_path)
    propose(service)
    service.submit_for_approval("operation-1", now=NOW)
    approval = approve()
    with pytest.raises(PolicyDeniedError, match="current approval"):
        service.authorize("operation-1", approval, now=NOW)
    trust_approval(service, approval)
    service.authorize("operation-1", approval, now=NOW)
    service.authorization_provider.approval_available = False
    with pytest.raises(PolicyDeniedError, match="current publication"):
        service.dispatch("operation-1", approval, now=NOW)
    assert service.store.get("operation-1").state is OutboxState.READY
    assert service.simulator.trace == []


def test_simulated_approvals_are_scoped_by_account_operation_and_reference():
    provider = SimulationAuthorizationProvider()
    approvals = (
        approve(reference="approval-shared"),
        approve("operation-2", reference="approval-shared"),
        approve("operation-3", account_ref="account-2", reference="approval-shared"),
    )
    for approval in approvals:
        provider.set_approval(approval)
    for approval in approvals:
        assert (
            provider.current_approval(
                account_ref=approval.account_ref,
                operation_id=approval.operation_id,
                approval_reference=approval.reference,
                now=NOW,
            )
            is approval
        )


def test_revocation_or_privacy_change_before_dispatch_blocks_all_effects(tmp_path):
    revoked = make_service(tmp_path / "revoked")
    _, approval = ready(revoked)
    revoked.authorization_provider.set_approval(approve(revoked=True))
    with pytest.raises(ValueError, match="revoked"):
        revoked.dispatch("operation-1", approval, now=NOW)
    assert revoked.store.get("operation-1").state is OutboxState.READY
    assert revoked.simulator.trace == []

    private = make_service(tmp_path / "private")
    _, approval = ready(private)
    private.authorization_provider.set_privacy("account-1", "operation-1", False)
    with pytest.raises(PolicyDeniedError, match="current draft privacy"):
        private.dispatch("operation-1", approval, now=NOW)
    assert private.store.get("operation-1").state is OutboxState.READY
    assert private.simulator.trace == []


@pytest.mark.parametrize("phase", ["authorize", "dispatch", "unauthenticated"])
def test_initial_policy_denials_are_revisioned_and_attributed(tmp_path, phase):
    service = make_service(tmp_path)
    if phase == "authorize":
        propose(service)
        service.submit_for_approval("operation-1", now=NOW)
        approval = trust_approval(service, approve())
        service.policy.emergency_stop = True
        expected_actor = f"approval-{content_fingerprint(approval.reference)[:16]}"
        with pytest.raises(PolicyDeniedError, match="stop"):
            service.authorize("operation-1", approval, now=NOW)
        expected_state = OutboxState.AWAITING_APPROVAL
        expected_phase = "authorize"
    elif phase == "dispatch":
        _, approval = ready(service)
        service.authorization_provider.set_approval(approve(revoked=True))
        expected_actor = f"approval-{content_fingerprint(approval.reference)[:16]}"
        with pytest.raises(ValueError, match="revoked"):
            service.dispatch("operation-1", approval, now=NOW)
        expected_state = OutboxState.READY
        expected_phase = "dispatch"
    else:
        propose(service)
        service.submit_for_approval("operation-1", now=NOW)
        expected_actor = "unauthenticated"
        with pytest.raises(ValueError, match="authenticated"):
            service.authorize("operation-1", object(), now=NOW)
        expected_state = OutboxState.AWAITING_APPROVAL
        expected_phase = "authorize"

    item = service.store.get("operation-1")
    assert item.state is expected_state
    with sqlite3.connect(service.store.database) as connection:
        assert connection.execute(
            "SELECT transition.reason, outbox.correlation_id "
            "FROM threads_outbox_transitions AS transition "
            "JOIN threads_outbox AS outbox USING(operation_id) "
            "WHERE transition.operation_id=? ORDER BY transition.revision DESC LIMIT 1",
            ("operation-1",),
        ).fetchone() == (
            f"{expected_phase}_policy_denied_{expected_actor}",
            "correlation-operation-1",
        )


@pytest.mark.parametrize("bad_snapshot", ["wrong_reference", "invalid_snapshot"])
def test_authorization_provider_contract_fails_closed_on_bad_current_data(
    tmp_path, monkeypatch, bad_snapshot
):
    service = make_service(tmp_path)
    _, approval = ready(service)
    provider = service.authorization_provider
    result = (
        PublicationAuthorizationSnapshot(approve(reference="approval-wrong"), True)
        if bad_snapshot == "wrong_reference"
        else object()
    )
    monkeypatch.setattr(
        provider, "current_publication_authorization", lambda **_kwargs: result
    )
    expected = ValueError if bad_snapshot == "wrong_reference" else PolicyDeniedError
    with pytest.raises(expected):
        service.dispatch("operation-1", approval, now=NOW)
    assert service.simulator.trace == []


def test_privacy_provider_unavailability_fails_closed(tmp_path):
    service = make_service(tmp_path)
    _, approval = ready(service)
    service.authorization_provider.privacy_available = False
    with pytest.raises(PolicyDeniedError, match="current publication"):
        service.dispatch("operation-1", approval, now=NOW)
    assert service.store.get("operation-1").state is OutboxState.READY
    assert service.simulator.trace == []


def test_happy_path_preserves_multistep_provider_evidence(tmp_path):
    service = make_service(tmp_path)
    item, approval = ready(service)
    assert item.state is OutboxState.READY
    confirmed = service.dispatch("operation-1", approval, now=NOW)
    assert confirmed.state is OutboxState.CONFIRMED
    container_id = simulated_provider_id("container", "operation-1")
    assert confirmed.provider_container_id == container_id
    assert confirmed.provider_publication_id == simulated_provider_id(
        "thread", "operation-1"
    )
    assert confirmed.attempt_count == 1
    assert service.simulator.trace == [
        ("create_container", "operation-1"),
        ("publish_container", container_id),
    ]
    assert service.simulator.network_calls == 0
    assert service.authorization_provider.network_calls == 0
    with pytest.raises(ThreadsStoreError):
        service.dispatch("operation-1", approval, now=NOW)


def test_simulated_provider_ids_stay_bounded_for_maximum_operation_ids(tmp_path):
    service = make_service(tmp_path)
    long_success = "a" * 128
    propose(
        service,
        long_success,
        draft_id="draft-long-success",
        correlation_id="correlation-long-success",
    )
    service.submit_for_approval(long_success, now=NOW)
    success_approval = issue_approval(
        reference="approval-long-success",
        account_ref="account-1",
        operation_id=long_success,
        expires_at=NOW + timedelta(hours=1),
    )
    trust_approval(service, success_approval)
    service.authorize(long_success, success_approval, now=NOW)
    confirmed = service.dispatch(long_success, success_approval, now=NOW)
    assert len(confirmed.provider_container_id) <= 128
    assert len(confirmed.provider_publication_id) <= 128

    long_reconciled = "b" * 128
    propose(
        service,
        long_reconciled,
        draft_id="draft-long-reconciled",
        correlation_id="correlation-long-reconciled",
    )
    service.submit_for_approval(long_reconciled, now=NOW)
    reconcile_approval = issue_approval(
        reference="approval-long-reconciled",
        account_ref="account-1",
        operation_id=long_reconciled,
        expires_at=NOW + timedelta(hours=1),
    )
    trust_approval(service, reconcile_approval)
    service.authorize(long_reconciled, reconcile_approval, now=NOW)
    service.simulator.set_fault(long_reconciled, "publish", Fault.DURING_PUBLISH)
    service.dispatch(long_reconciled, reconcile_approval, now=NOW)
    service.simulator.reconciliation[long_reconciled] = ReconciliationResult.CONFIRMED
    reconciled = service.reconcile(long_reconciled, now=NOW)
    assert len(reconciled.provider_publication_id) <= 128
    assert reconciled.provider_publication_id == simulated_provider_id(
        "thread", long_reconciled
    )


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


def test_unknown_provider_effect_class_fails_closed_as_unknown(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    _, approval = ready(service)

    def future_failure(_item):
        raise ProviderFailure("future_effect_class", "unrecognized_effect")

    monkeypatch.setattr(service.simulator, "create_container", future_failure)
    item = service.dispatch("operation-1", approval, now=NOW)
    assert item.state is OutboxState.UNKNOWN
    assert item.reconciliation_status == "required"
    assert item.dispatch_evidence == "unrecognized_effect"


def test_proven_pre_dispatch_retry_is_bounded_and_reuses_container(tmp_path):
    service = make_service(tmp_path)
    _, approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.BEFORE_PUBLISH)
    first = service.dispatch("operation-1", approval, now=NOW)
    assert first.state is OutboxState.READY
    assert first.provider_container_id == simulated_provider_id(
        "container", "operation-1"
    )
    second = service.dispatch("operation-1", approval, now=NOW)
    assert second.state is OutboxState.FAILED_TERMINAL
    assert service.simulator.trace.count(("create_container", "operation-1")) == 1


@pytest.mark.parametrize(
    "stage, fault, expected",
    [
        ("container", Fault.CRASH_BEFORE_TRANSMISSION, OutboxState.READY),
        ("container", Fault.CRASH_AFTER_TRANSMISSION, OutboxState.UNKNOWN),
        ("publish", Fault.CRASH_BEFORE_TRANSMISSION, OutboxState.READY),
        ("publish", Fault.CRASH_AFTER_TRANSMISSION, OutboxState.UNKNOWN),
    ],
)
def test_restart_recovery_uses_durable_dispatch_evidence(
    tmp_path, stage, fault, expected
):
    service = make_service(tmp_path)
    _, approval = ready(service)
    service.simulator.set_fault("operation-1", stage, fault)
    with pytest.raises(SimulatedCrash):
        service.dispatch("operation-1", approval, now=NOW)
    reopened = ThreadsService(
        ThreadsStore(service.store.database), service.simulator, service.policy
    )
    recovered = reopened.recover_sending("operation-1", now=NOW)
    assert recovered.state is expected
    with pytest.raises(ThreadsStoreError):
        reopened.recover_sending("operation-1", now=NOW)


def test_recovered_pre_dispatch_failures_obey_attempt_cap(tmp_path):
    service = make_service(tmp_path)
    _, approval = ready(service)
    service.simulator.set_fault(
        "operation-1", "container", Fault.CRASH_BEFORE_TRANSMISSION
    )
    with pytest.raises(SimulatedCrash):
        service.dispatch("operation-1", approval, now=NOW)
    assert service.recover_sending("operation-1", now=NOW).state is OutboxState.READY
    with pytest.raises(SimulatedCrash):
        service.dispatch("operation-1", approval, now=NOW)
    terminal = service.recover_sending("operation-1", now=NOW)
    assert terminal.state is OutboxState.FAILED_TERMINAL
    assert terminal.attempt_count == 2


@pytest.mark.parametrize("stage", ["container", "publish"])
def test_abrupt_process_death_after_durable_boundary_marker_is_unknown(
    tmp_path, monkeypatch, stage
):
    service = make_service(tmp_path)
    _, approval = ready(service)

    def terminate(*_args):
        persisted = service.store.get("operation-1")
        assert persisted.dispatch_evidence == f"{stage}_dispatch_started"
        if stage == "publish":
            assert persisted.provider_container_id == simulated_provider_id(
                "container", "operation-1"
            )
        raise SystemExit("abrupt process death")

    method = "create_container" if stage == "container" else "publish"
    monkeypatch.setattr(service.simulator, method, terminate)
    with pytest.raises(SystemExit, match="abrupt"):
        service.dispatch("operation-1", approval, now=NOW)
    reopened = ThreadsService(
        ThreadsStore(service.store.database), service.simulator, service.policy
    )
    assert reopened.recover_sending("operation-1", now=NOW).state is (
        OutboxState.UNKNOWN
    )


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


def test_policy_stop_after_container_persists_and_reuses_proven_safe_container(
    tmp_path, monkeypatch
):
    service = make_service(tmp_path)
    _, approval = ready(service)
    calls = 0

    def create_then_stop(item):
        nonlocal calls
        calls += 1
        service.policy.emergency_stop = True
        return simulated_provider_id("container", item.operation_id)

    monkeypatch.setattr(service.simulator, "create_container", create_then_stop)
    with pytest.raises(PolicyDeniedError, match="stop"):
        service.dispatch("operation-1", approval, now=NOW)
    blocked = service.store.get("operation-1")
    assert blocked.state is OutboxState.READY
    assert blocked.provider_container_id == simulated_provider_id(
        "container", "operation-1"
    )
    assert blocked.dispatch_evidence == "publish_not_transmitted"
    service.policy.emergency_stop = False
    confirmed = service.dispatch("operation-1", approval, now=NOW)
    assert confirmed.state is OutboxState.CONFIRMED
    assert calls == 1


def test_approval_time_is_refreshed_after_container_creation(tmp_path, monkeypatch):
    current_time = [NOW]
    runtime_policy = make_policy()
    simulator = ThreadsSimulator(account_ref="account-1")
    authorization_provider = SimulationAuthorizationProvider()
    service = ThreadsService(
        ThreadsStore(tmp_path / "threads.sqlite3"),
        simulator,
        runtime_policy,
        utc_now=lambda: current_time[0],
        authorization_provider=authorization_provider,
    )
    propose(service)
    service.submit_for_approval("operation-1", now=NOW)
    expiring = approve(expires_at=NOW + timedelta(minutes=1))
    trust_approval(service, expiring)
    service.authorize("operation-1", expiring, now=NOW)
    create_container = simulator.create_container

    def create_then_advance_time(item):
        container_id = create_container(item)
        current_time[0] = NOW + timedelta(minutes=2)
        return container_id

    monkeypatch.setattr(simulator, "create_container", create_then_advance_time)
    with pytest.raises(ValueError, match="expired"):
        service.dispatch("operation-1", expiring, now=NOW)
    blocked = service.store.get("operation-1")
    assert blocked.state is OutboxState.READY
    assert blocked.provider_container_id == simulated_provider_id(
        "container", "operation-1"
    )
    assert simulator.trace == [("create_container", "operation-1")]


def test_approval_expiry_is_rechecked_before_ready_transition(tmp_path, monkeypatch):
    current_time = [NOW]
    service = make_service(tmp_path)
    service.utc_now = lambda: current_time[0]
    propose(service)
    service.submit_for_approval("operation-1", now=NOW)
    expiring = trust_approval(service, approve(expires_at=NOW + timedelta(minutes=1)))
    current_snapshot = service.authorization_provider.current_publication_authorization

    def authorize_snapshot_then_expire(**kwargs):
        result = current_snapshot(**kwargs)
        current_time[0] = NOW + timedelta(minutes=2)
        return result

    monkeypatch.setattr(
        service.authorization_provider,
        "current_publication_authorization",
        authorize_snapshot_then_expire,
    )
    with pytest.raises(ValueError, match="expired"):
        service.authorize("operation-1", expiring, now=NOW)
    assert service.store.get("operation-1").state is OutboxState.AWAITING_APPROVAL
    assert service.simulator.trace == []


def test_current_draft_privacy_is_rechecked_before_publication(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    _, approval = ready(service)
    create_container = service.simulator.create_container

    def create_then_revoke_privacy(item):
        container_id = create_container(item)
        service.authorization_provider.set_privacy(
            item.account_ref, item.operation_id, False
        )
        return container_id

    monkeypatch.setattr(
        service.simulator, "create_container", create_then_revoke_privacy
    )
    with pytest.raises(PolicyDeniedError, match="current draft privacy"):
        service.dispatch("operation-1", approval, now=NOW)
    blocked = service.store.get("operation-1")
    assert blocked.state is OutboxState.READY
    assert blocked.provider_container_id == simulated_provider_id(
        "container", "operation-1"
    )
    assert service.simulator.trace == [("create_container", "operation-1")]


def test_current_approval_status_is_rechecked_before_publication(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    _, approval = ready(service)
    create_container = service.simulator.create_container

    def create_then_revoke_approval(item):
        container_id = create_container(item)
        service.authorization_provider.set_approval(
            approve(item.operation_id, revoked=True)
        )
        return container_id

    monkeypatch.setattr(
        service.simulator, "create_container", create_then_revoke_approval
    )
    with pytest.raises(ValueError, match="revoked"):
        service.dispatch("operation-1", approval, now=NOW)
    blocked = service.store.get("operation-1")
    assert blocked.state is OutboxState.READY
    assert blocked.provider_container_id == simulated_provider_id(
        "container", "operation-1"
    )
    assert service.simulator.trace == [("create_container", "operation-1")]


def test_publication_checks_use_atomic_authorization_snapshot(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    _, approval = ready(service)
    monkeypatch.setattr(
        service.authorization_provider,
        "current_approval",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("split approval read")),
    )
    assert service.dispatch("operation-1", approval, now=NOW).state is (
        OutboxState.CONFIRMED
    )


def test_privacy_is_rechecked_after_final_container_marker(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    _, approval = ready(service)
    update_evidence = service.store.update_sending_evidence

    def update_then_revoke(operation_id, evidence, **kwargs):
        update_evidence(operation_id, evidence, **kwargs)
        if evidence == "container_dispatch_started":
            service.authorization_provider.set_privacy("account-1", operation_id, False)

    monkeypatch.setattr(service.store, "update_sending_evidence", update_then_revoke)
    with pytest.raises(PolicyDeniedError, match="current draft privacy"):
        service.dispatch("operation-1", approval, now=NOW)
    blocked = service.store.get("operation-1")
    assert blocked.state is OutboxState.READY
    assert blocked.dispatch_evidence == "container_not_transmitted"
    assert service.simulator.trace == []


def test_privacy_is_rechecked_after_final_publish_marker(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    _, approval = ready(service)
    update_evidence = service.store.update_sending_evidence

    def update_then_revoke(operation_id, evidence, **kwargs):
        update_evidence(operation_id, evidence, **kwargs)
        if evidence == "publish_dispatch_started":
            service.authorization_provider.set_privacy("account-1", operation_id, False)

    monkeypatch.setattr(service.store, "update_sending_evidence", update_then_revoke)
    with pytest.raises(PolicyDeniedError, match="current draft privacy"):
        service.dispatch("operation-1", approval, now=NOW)
    blocked = service.store.get("operation-1")
    assert blocked.state is OutboxState.READY
    assert blocked.dispatch_evidence == "publish_not_transmitted"
    assert service.simulator.trace == [("create_container", "operation-1")]


def test_recovery_cannot_overlap_a_live_dispatcher(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    _, approval = ready(service)
    update_evidence = service.store.update_sending_evidence

    def update_then_attempt_recovery(operation_id, evidence, **kwargs):
        update_evidence(operation_id, evidence, **kwargs)
        if evidence == "publish_dispatch_started":
            with pytest.raises(ThreadsStoreError, match="active dispatch worker"):
                service.recover_sending(operation_id, now=NOW)

    monkeypatch.setattr(
        service.store, "update_sending_evidence", update_then_attempt_recovery
    )
    confirmed = service.dispatch("operation-1", approval, now=NOW)
    assert confirmed.state is OutboxState.CONFIRMED
    assert service.simulator.trace == [
        ("create_container", "operation-1"),
        (
            "publish_container",
            simulated_provider_id("container", "operation-1"),
        ),
    ]


@pytest.mark.parametrize(
    "policy_field, policy_value, message",
    [
        ("emergency_stop", True, "stop"),
        ("account_ref", "account-2", "account mismatch"),
    ],
)
def test_mutable_policy_is_rechecked_after_authorization_lookup(
    tmp_path, monkeypatch, policy_field, policy_value, message
):
    service = make_service(tmp_path)
    _, approval = ready(service)
    current_snapshot = service.authorization_provider.current_publication_authorization

    def authorize_snapshot_then_stop(**kwargs):
        result = current_snapshot(**kwargs)
        setattr(service.policy, policy_field, policy_value)
        return result

    monkeypatch.setattr(
        service.authorization_provider,
        "current_publication_authorization",
        authorize_snapshot_then_stop,
    )
    with pytest.raises(PolicyDeniedError, match=message):
        service.dispatch("operation-1", approval, now=NOW)
    assert service.store.get("operation-1").state is OutboxState.READY
    assert service.simulator.trace == []


def test_approval_expiry_is_rechecked_after_final_authorization_lookup(
    tmp_path, monkeypatch
):
    current_time = [NOW]
    runtime_policy = make_policy()
    simulator = ThreadsSimulator(account_ref="account-1")
    authorization_provider = SimulationAuthorizationProvider()
    service = ThreadsService(
        ThreadsStore(tmp_path / "threads.sqlite3"),
        simulator,
        runtime_policy,
        utc_now=lambda: current_time[0],
        authorization_provider=authorization_provider,
    )
    propose(service)
    service.submit_for_approval("operation-1", now=NOW)
    expiring = trust_approval(service, approve(expires_at=NOW + timedelta(minutes=1)))
    service.authorize("operation-1", expiring, now=NOW)
    current_snapshot = authorization_provider.current_publication_authorization
    snapshot_checks = 0

    def expire_during_final_snapshot_lookup(**kwargs):
        nonlocal snapshot_checks
        snapshot_checks += 1
        result = current_snapshot(**kwargs)
        if snapshot_checks == 4:
            current_time[0] = NOW + timedelta(minutes=2)
        return result

    monkeypatch.setattr(
        authorization_provider,
        "current_publication_authorization",
        expire_during_final_snapshot_lookup,
    )
    with pytest.raises(ValueError, match="expired"):
        service.dispatch("operation-1", expiring, now=NOW)
    blocked = service.store.get("operation-1")
    assert blocked.state is OutboxState.READY
    assert blocked.dispatch_evidence == "publish_not_transmitted"
    assert simulator.trace == [("create_container", "operation-1")]


def test_dispatch_persists_the_current_approval_reference(tmp_path):
    service = make_service(tmp_path)
    _, original = ready(service)
    replacement = approve(reference="approval-replacement")
    trust_approval(service, replacement)
    confirmed = service.dispatch("operation-1", replacement, now=NOW)
    assert original.reference == "approval-operation-1"
    assert confirmed.approval_reference == "approval-replacement"
    assert confirmed.approval_status == "approved"


def test_expiry_at_initial_dispatch_check_preserves_ready_state(tmp_path):
    current_time = [NOW]
    runtime_policy = make_policy()
    simulator = ThreadsSimulator(account_ref="account-1")
    authorization_provider = SimulationAuthorizationProvider()
    service = ThreadsService(
        ThreadsStore(tmp_path / "threads.sqlite3"),
        simulator,
        runtime_policy,
        utc_now=lambda: current_time[0],
        authorization_provider=authorization_provider,
    )
    propose(service)
    service.submit_for_approval("operation-1", now=NOW)
    expiring = approve(expires_at=NOW + timedelta(minutes=1))
    trust_approval(service, expiring)
    service.authorize("operation-1", expiring, now=NOW)
    current_time[0] = NOW + timedelta(minutes=2)
    with pytest.raises(ValueError, match="expired"):
        service.dispatch("operation-1", expiring, now=NOW)
    blocked = service.store.get("operation-1")
    assert blocked.state is OutboxState.READY
    assert blocked.dispatch_evidence is None
    assert simulator.trace == []


def test_repeated_pre_publish_denials_obey_attempt_cap(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    _, approval = ready(service)
    update_evidence = service.store.update_sending_evidence

    def update_then_stop(operation_id, evidence, **kwargs):
        update_evidence(operation_id, evidence, **kwargs)
        if evidence == "publish_dispatch_started":
            service.policy.emergency_stop = True

    monkeypatch.setattr(service.store, "update_sending_evidence", update_then_stop)
    with pytest.raises(PolicyDeniedError, match="stop"):
        service.dispatch("operation-1", approval, now=NOW)
    assert service.store.get("operation-1").state is OutboxState.READY
    service.policy.emergency_stop = False
    with pytest.raises(PolicyDeniedError, match="stop"):
        service.dispatch("operation-1", approval, now=NOW)
    terminal = service.store.get("operation-1")
    assert terminal.state is OutboxState.FAILED_TERMINAL
    assert terminal.attempt_count == 2


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


@pytest.mark.parametrize("result", [None, "confirmed", object()])
def test_unknown_reconciliation_result_preserves_uncertainty(
    tmp_path, monkeypatch, result
):
    service = make_service(tmp_path)
    _, approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", approval, now=NOW)
    monkeypatch.setattr(service.simulator, "reconcile", lambda _item: result)
    unresolved = service.reconcile("operation-1", now=NOW)
    assert unresolved.state is OutboxState.UNKNOWN
    assert unresolved.reconciliation_status == "invalid_result"


def test_reconciliation_failure_preserves_unknown_and_abandonment_is_permanent(
    tmp_path,
):
    service = make_service(tmp_path)
    _, approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", approval, now=NOW)
    service.simulator.set_fault("operation-1", "reconcile", Fault.RECONCILIATION)
    assert service.reconcile("operation-1", now=NOW).state is OutboxState.UNKNOWN
    with pytest.raises(ValueError, match="purpose"):
        service.abandon_unknown("operation-1", approval, now=NOW)
    abandonment_approval = trust_approval(
        service,
        approve(
            reference="approval-abandon-operation-1",
            purpose=ApprovalPurpose.ABANDON_UNKNOWN,
        ),
    )
    service.store.claim_reconciliation(
        operation_id="operation-1", account_ref="account-1", now=NOW
    )
    with pytest.raises(ThreadsStoreError, match="active operation"):
        service.abandon_unknown("operation-1", abandonment_approval, now=NOW)
    service.store.release_unknown_effect_lock(
        target_operation_id="operation-1",
        holder_operation_id="operation-1",
        lock_kind="reconciliation",
        required=True,
    )
    abandoned = service.abandon_unknown("operation-1", abandonment_approval, now=NOW)
    assert abandoned.state is OutboxState.UNKNOWN_ABANDONED
    assert (
        service.store.operator_decision_reference("operation-1", "abandon_unknown")
        == "approval-abandon-operation-1"
    )
    assert service.store.unresolved_for_account("account-1") == (abandoned,)
    with pytest.raises(ThreadsStoreError):
        service.reconcile("operation-1", now=NOW)
    with pytest.raises(ThreadsStoreError):
        service.dispatch("operation-1", approval, now=NOW)


def test_reconciliation_claim_requires_authenticated_restart_recovery(
    tmp_path, monkeypatch
):
    service = make_service(tmp_path)
    _, publication_approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", publication_approval, now=NOW)
    reconcile = service.simulator.reconcile
    monkeypatch.setattr(
        service.simulator,
        "reconcile",
        lambda _item: (_ for _ in ()).throw(SystemExit("process terminated")),
    )
    with pytest.raises(SystemExit, match="terminated"):
        service.reconcile("operation-1", now=NOW)
    with pytest.raises(ThreadsStoreError, match="reserved"):
        service.reconcile("operation-1", now=NOW)
    with pytest.raises(ValueError, match="purpose"):
        service.recover_reconciliation_claim(
            "operation-1", publication_approval, now=NOW
        )

    recovery_approval = trust_approval(
        service,
        approve(
            reference="approval-recover-operation-1",
            purpose=ApprovalPurpose.RECOVER_RECONCILIATION,
        ),
    )
    restarted = ThreadsService(
        ThreadsStore(service.store.database),
        service.simulator,
        service.policy,
        utc_now=lambda: NOW,
        authorization_provider=service.authorization_provider,
    )
    restarted.recover_reconciliation_claim("operation-1", recovery_approval, now=NOW)
    decision_prefix = (
        "recover_reconciliation-"
        f"{content_fingerprint(recovery_approval.reference)[:16]}"
    )
    assert (
        restarted.store.operator_decision_reference(
            "operation-1", f"{decision_prefix}-1"
        )
        == recovery_approval.reference
    )
    restarted.store.claim_reconciliation(
        operation_id="operation-1", account_ref="account-1", now=NOW
    )
    restarted.recover_reconciliation_claim("operation-1", recovery_approval, now=NOW)
    assert (
        restarted.store.operator_decision_reference(
            "operation-1", f"{decision_prefix}-2"
        )
        == recovery_approval.reference
    )
    monkeypatch.setattr(service.simulator, "reconcile", reconcile)
    assert restarted.reconcile("operation-1", now=NOW).state is OutboxState.UNKNOWN
    with pytest.raises(ThreadsStoreError, match="claim is missing"):
        restarted.recover_reconciliation_claim(
            "operation-1", recovery_approval, now=NOW
        )


def test_reconciliation_recovery_cannot_overlap_live_worker(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    _, publication_approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", publication_approval, now=NOW)
    recovery_approval = trust_approval(
        service,
        approve(
            reference="approval-recover-operation-1",
            purpose=ApprovalPurpose.RECOVER_RECONCILIATION,
        ),
    )

    def recover_while_reconciliation_is_live(_item):
        with pytest.raises(ThreadsStoreError, match="reconciliation worker"):
            service.recover_reconciliation_claim(
                "operation-1", recovery_approval, now=NOW
            )
        return ReconciliationResult.UNRESOLVED

    monkeypatch.setattr(
        service.simulator, "reconcile", recover_while_reconciliation_is_live
    )
    result = service.reconcile("operation-1", now=NOW)
    assert result.state is OutboxState.UNKNOWN
    assert result.reconciliation_status == "unresolved"
    service.store.claim_reconciliation(
        operation_id="operation-1", account_ref="account-1", now=NOW
    )
    service.recover_reconciliation_claim("operation-1", recovery_approval, now=NOW)


@pytest.mark.parametrize("action", ["abandon", "recover"])
def test_administrative_approval_expiry_is_refreshed_after_lookup(
    tmp_path, monkeypatch, action
):
    current_time = [NOW]
    service = make_service(tmp_path)
    service.utc_now = lambda: current_time[0]
    _, publication_approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", publication_approval, now=NOW)
    purpose = (
        ApprovalPurpose.ABANDON_UNKNOWN
        if action == "abandon"
        else ApprovalPurpose.RECOVER_RECONCILIATION
    )
    administrative_approval = trust_approval(
        service,
        approve(
            reference=f"approval-{action}-operation-1",
            expires_at=NOW + timedelta(minutes=1),
            purpose=purpose,
        ),
    )
    if action == "recover":
        service.store.claim_reconciliation(
            operation_id="operation-1", account_ref="account-1", now=NOW
        )
    current_approval = service.authorization_provider.current_approval

    def authorize_then_expire(**kwargs):
        result = current_approval(**kwargs)
        current_time[0] = NOW + timedelta(minutes=2)
        return result

    monkeypatch.setattr(
        service.authorization_provider, "current_approval", authorize_then_expire
    )
    with pytest.raises(ValueError, match="expired"):
        if action == "abandon":
            service.abandon_unknown("operation-1", administrative_approval, now=NOW)
        else:
            service.recover_reconciliation_claim(
                "operation-1", administrative_approval, now=NOW
            )
    assert service.store.get("operation-1").state is OutboxState.UNKNOWN
    if action == "recover":
        with pytest.raises(ThreadsStoreError, match="already reserved"):
            service.store.claim_reconciliation(
                operation_id="operation-1", account_ref="account-1", now=NOW
            )


@pytest.mark.parametrize("action", ["abandon", "recover"])
def test_administrative_approval_is_rechecked_inside_write_transaction(
    tmp_path, monkeypatch, action
):
    service = make_service(tmp_path)
    _, publication_approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", publication_approval, now=NOW)
    purpose = (
        ApprovalPurpose.ABANDON_UNKNOWN
        if action == "abandon"
        else ApprovalPurpose.RECOVER_RECONCILIATION
    )
    administrative_approval = trust_approval(
        service,
        approve(
            reference=f"approval-{action}-operation-1",
            purpose=purpose,
        ),
    )
    if action == "recover":
        service.store.claim_reconciliation(
            operation_id="operation-1", account_ref="account-1", now=NOW
        )
    current_approval = service.authorization_provider.current_approval
    administrative_checks = 0

    def revoke_before_precommit_lookup(**kwargs):
        nonlocal administrative_checks
        if kwargs["approval_reference"] == administrative_approval.reference:
            administrative_checks += 1
            if administrative_checks == 2:
                service.authorization_provider.set_approval(
                    approve(
                        reference=administrative_approval.reference,
                        revoked=True,
                        purpose=purpose,
                    )
                )
        return current_approval(**kwargs)

    monkeypatch.setattr(
        service.authorization_provider,
        "current_approval",
        revoke_before_precommit_lookup,
    )
    with pytest.raises(ValueError, match="revoked"):
        if action == "abandon":
            service.abandon_unknown("operation-1", administrative_approval, now=NOW)
        else:
            service.recover_reconciliation_claim(
                "operation-1", administrative_approval, now=NOW
            )
    assert administrative_checks == 2
    assert service.store.get("operation-1").state is OutboxState.UNKNOWN
    assert (
        service.store.operator_decision_reference("operation-1", "abandon_unknown")
        is None
    )
    if action == "recover":
        with pytest.raises(ThreadsStoreError, match="already reserved"):
            service.store.claim_reconciliation(
                operation_id="operation-1", account_ref="account-1", now=NOW
            )


@pytest.mark.parametrize(
    "provider_state, expected",
    [
        ("missing_provider", "provider is unavailable"),
        ("unavailable", "current approval is unavailable"),
        ("unregistered", "current approval is unavailable"),
        ("wrong_reference", "reference mismatch"),
    ],
)
def test_administrative_authorization_provider_failures_preserve_unknown_state(
    tmp_path, monkeypatch, provider_state, expected
):
    service = make_service(tmp_path)
    _, publication_approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", publication_approval, now=NOW)
    administrative_approval = approve(
        reference="approval-abandon-operation-1",
        purpose=ApprovalPurpose.ABANDON_UNKNOWN,
    )
    if provider_state != "unregistered":
        trust_approval(service, administrative_approval)
    if provider_state == "missing_provider":
        service.authorization_provider = None
    elif provider_state == "unavailable":
        service.authorization_provider.approval_available = False
    elif provider_state == "wrong_reference":
        monkeypatch.setattr(
            service.authorization_provider,
            "current_approval",
            lambda **_kwargs: approve(
                reference="approval-wrong",
                purpose=ApprovalPurpose.ABANDON_UNKNOWN,
            ),
        )
    with pytest.raises((PolicyDeniedError, ValueError), match=expected):
        service.abandon_unknown("operation-1", administrative_approval, now=NOW)
    assert service.store.get("operation-1").state is OutboxState.UNKNOWN


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
    third_approval = trust_approval(service, approve("operation-3"))
    with pytest.raises(PolicyDeniedError, match="paused"):
        service.authorize("operation-3", third_approval, now=NOW)


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
    trust_approval(service, risk_approval)
    assert (
        service.authorize("operation-2", risk_approval, now=NOW).state
        is OutboxState.READY
    )


def test_exact_replacement_retry_survives_target_resolution_and_ack_expiry(tmp_path):
    service = make_service(tmp_path)
    _, first_approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", first_approval, now=NOW)
    original_ack = approve("operation-2", duplicate_risk_acknowledged=True)
    replacement, created = propose(
        service,
        "operation-2",
        replacement_of="operation-1",
        replacement_approval=original_ack,
    )
    assert created is True
    service.simulator.reconciliation["operation-1"] = ReconciliationResult.CONFIRMED
    service.reconcile("operation-1", now=NOW)
    expired_ack = approve(
        "operation-2",
        reference=original_ack.reference,
        expires_at=NOW,
        duplicate_risk_acknowledged=True,
    )
    repeated, repeated_created = propose(
        service,
        "operation-2",
        replacement_of="operation-1",
        replacement_approval=expired_ack,
    )
    assert repeated_created is False
    assert repeated == replacement


def test_replacement_is_blocked_if_target_reconciles_before_dispatch(tmp_path):
    service = make_service(tmp_path)
    _, first_approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", first_approval, now=NOW)
    risk_approval = approve("operation-2", duplicate_risk_acknowledged=True)
    propose(
        service,
        "operation-2",
        replacement_of="operation-1",
        replacement_approval=risk_approval,
    )
    service.submit_for_approval("operation-2", now=NOW)
    trust_approval(service, risk_approval)
    service.authorize("operation-2", risk_approval, now=NOW)
    service.simulator.reconciliation["operation-1"] = ReconciliationResult.CONFIRMED
    service.reconcile("operation-1", now=NOW)
    with pytest.raises(PolicyDeniedError, match="no longer unresolved"):
        service.dispatch("operation-2", risk_approval, now=NOW)


def test_replacement_target_is_revalidated_in_proposal_transaction(
    tmp_path, monkeypatch
):
    service = make_service(tmp_path)
    _, first_approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", first_approval, now=NOW)
    create_proposal = service.store.create_proposal

    def resolve_target_before_insert(**kwargs):
        service.store.transition(
            "operation-1",
            OutboxState.CONFIRMED,
            reason="concurrent_reconciliation_success",
            now=NOW,
            provider_publication_id="thread-reconciled",
            reconciliation_status="verified_success",
        )
        return create_proposal(**kwargs)

    monkeypatch.setattr(service.store, "create_proposal", resolve_target_before_insert)
    risk_approval = approve("operation-2", duplicate_risk_acknowledged=True)
    with pytest.raises(PolicyDeniedError, match="not unresolved"):
        propose(
            service,
            "operation-2",
            replacement_of="operation-1",
            replacement_approval=risk_approval,
        )
    with pytest.raises(ThreadsStoreError, match="unknown operation"):
        service.store.get("operation-2")


def test_replacement_publication_serializes_target_reconciliation(
    tmp_path, monkeypatch
):
    service = make_service(tmp_path)
    _, first_approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", first_approval, now=NOW)
    risk_approval = trust_approval(
        service, approve("operation-2", duplicate_risk_acknowledged=True)
    )
    propose(
        service,
        "operation-2",
        replacement_of="operation-1",
        replacement_approval=risk_approval,
    )
    service.submit_for_approval("operation-2", now=NOW)
    service.authorize("operation-2", risk_approval, now=NOW)
    publish = service.simulator.publish

    def publish_while_target_reconciles(item, container_id):
        with pytest.raises(ThreadsStoreError, match="reserved"):
            service.reconcile("operation-1", now=NOW)
        assert service.store.get("operation-1").state is OutboxState.UNKNOWN
        return publish(item, container_id)

    monkeypatch.setattr(service.simulator, "publish", publish_while_target_reconciles)
    replacement = service.dispatch("operation-2", risk_approval, now=NOW)
    assert replacement.state is OutboxState.CONFIRMED
    assert service.reconcile("operation-1", now=NOW).state is OutboxState.UNKNOWN


def test_account_effect_reservation_serializes_container_and_publish(
    tmp_path, monkeypatch
):
    service = make_service(tmp_path)
    _, first_approval = ready(service)
    _, second_approval = ready(service, "operation-2")
    create_container = service.simulator.create_container

    def create_while_second_dispatches(item):
        if item.operation_id == "operation-1":
            with sqlite3.connect(service.store.database) as connection:
                assert connection.execute(
                    "SELECT holder_operation_id FROM threads_account_effect_locks "
                    "WHERE account_ref='account-1'"
                ).fetchone() == ("operation-1",)
            with pytest.raises(PolicyDeniedError, match="being dispatched"):
                service.dispatch("operation-2", second_approval, now=NOW)
            assert service.store.get("operation-2").state is OutboxState.READY
        return create_container(item)

    monkeypatch.setattr(
        service.simulator, "create_container", create_while_second_dispatches
    )
    assert service.dispatch("operation-1", first_approval, now=NOW).state is (
        OutboxState.CONFIRMED
    )
    assert service.dispatch("operation-2", second_approval, now=NOW).state is (
        OutboxState.CONFIRMED
    )
    with sqlite3.connect(service.store.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM threads_account_effect_locks"
        ).fetchone() == (0,)


def test_replacement_waits_when_target_reconciliation_claim_exists(tmp_path):
    service = make_service(tmp_path)
    _, first_approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", first_approval, now=NOW)
    risk_approval = trust_approval(
        service, approve("operation-2", duplicate_risk_acknowledged=True)
    )
    propose(
        service,
        "operation-2",
        replacement_of="operation-1",
        replacement_approval=risk_approval,
    )
    service.submit_for_approval("operation-2", now=NOW)
    service.authorize("operation-2", risk_approval, now=NOW)
    service.store.claim_reconciliation(
        operation_id="operation-1", account_ref="account-1", now=NOW
    )
    with pytest.raises(PolicyDeniedError, match="being reconciled"):
        service.dispatch("operation-2", risk_approval, now=NOW)
    blocked = service.store.get("operation-2")
    assert blocked.state is OutboxState.READY
    assert blocked.dispatch_evidence == "container_not_transmitted"
    service.store.release_unknown_effect_lock(
        target_operation_id="operation-1",
        holder_operation_id="operation-1",
        lock_kind="reconciliation",
        required=True,
    )


def test_replacement_crash_recovery_releases_target_reservation(tmp_path):
    service = make_service(tmp_path)
    _, first_approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", first_approval, now=NOW)
    risk_approval = trust_approval(
        service, approve("operation-2", duplicate_risk_acknowledged=True)
    )
    propose(
        service,
        "operation-2",
        replacement_of="operation-1",
        replacement_approval=risk_approval,
    )
    service.submit_for_approval("operation-2", now=NOW)
    service.authorize("operation-2", risk_approval, now=NOW)
    service.simulator.set_fault(
        "operation-2", "publish", Fault.CRASH_AFTER_TRANSMISSION
    )
    with pytest.raises(SimulatedCrash):
        service.dispatch("operation-2", risk_approval, now=NOW)
    with pytest.raises(ThreadsStoreError, match="reserved"):
        service.reconcile("operation-1", now=NOW)
    assert service.recover_sending("operation-2", now=NOW).state is OutboxState.UNKNOWN
    assert service.reconcile("operation-1", now=NOW).state is OutboxState.UNKNOWN


def test_replacement_cannot_bypass_an_additional_unknown_publication(tmp_path):
    service = make_service(tmp_path)
    _, first_approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", first_approval, now=NOW)
    second_approval = approve("operation-2", duplicate_risk_acknowledged=True)
    propose(
        service,
        "operation-2",
        replacement_of="operation-1",
        replacement_approval=second_approval,
    )
    service.submit_for_approval("operation-2", now=NOW)
    trust_approval(service, second_approval)
    service.authorize("operation-2", second_approval, now=NOW)
    service.simulator.set_fault("operation-2", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-2", second_approval, now=NOW)
    third_approval = approve("operation-3", duplicate_risk_acknowledged=True)
    propose(
        service,
        "operation-3",
        replacement_of="operation-1",
        replacement_approval=third_approval,
    )
    service.submit_for_approval("operation-3", now=NOW)
    trust_approval(service, third_approval)
    with pytest.raises(PolicyDeniedError, match="additional unresolved"):
        service.authorize("operation-3", third_approval, now=NOW)


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
    trust_approval(service, first_approval)
    service.authorize("operation-1", first_approval, now=NOW)
    service.simulator.set_fault("operation-1", "publish", Fault.DURING_PUBLISH)
    service.dispatch("operation-1", first_approval, now=NOW)
    with pytest.raises(PolicyDeniedError, match="authenticated approval"):
        propose(service, "operation-2", replacement_of="operation-1")
    forged = Approval(
        "approval-forged-replacement",
        "account-1",
        "operation-2",
        NOW + timedelta(hours=1),
        False,
        True,
        object(),
    )
    with pytest.raises(PolicyDeniedError, match="authenticated approval"):
        propose(
            service,
            "operation-2",
            replacement_of="operation-1",
            replacement_approval=forged,
        )


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
    with pytest.raises(ValueError, match="pre-create check"):
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
            precreate_check=object(),
        )
    with pytest.raises(ValueError, match="authorization fence"):
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
            replacement_of="operation-1",
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
    with pytest.raises(ValueError, match="pre-commit check"):
        service.store.transition(
            "operation-1",
            OutboxState.AWAITING_APPROVAL,
            reason="review",
            now=NOW,
            precommit_check=object(),
        )
    with pytest.raises(ValueError, match="authorization fence"):
        service.store.transition(
            "operation-1",
            OutboxState.AWAITING_APPROVAL,
            reason="review",
            now=NOW,
            operator_decision="abandon_unknown",
            operator_approval_reference="approval-operator",
        )
    with pytest.raises(ValueError, match="supplied together"):
        service.store.transition(
            "operation-1",
            OutboxState.AWAITING_APPROVAL,
            reason="review",
            now=NOW,
            operator_decision="abandon_unknown",
        )
    with pytest.raises(ValueError, match="operator decision"):
        service.store.transition(
            "operation-1",
            OutboxState.AWAITING_APPROVAL,
            reason="review",
            now=NOW,
            operator_decision="invalid_decision",
            operator_approval_reference="approval-operator",
        )
    with pytest.raises(ValueError, match="target and kind"):
        service.store.transition(
            "operation-1",
            OutboxState.AWAITING_APPROVAL,
            reason="review",
            now=NOW,
            release_lock_target_operation_id="operation-1",
        )
    with pytest.raises(ValueError, match="release-lock kind"):
        service.store.transition(
            "operation-1",
            OutboxState.AWAITING_APPROVAL,
            reason="review",
            now=NOW,
            release_lock_target_operation_id="operation-1",
            release_lock_kind="invalid",
        )
    with pytest.raises(ValueError, match="release-lock requirement"):
        service.store.transition(
            "operation-1",
            OutboxState.AWAITING_APPROVAL,
            reason="review",
            now=NOW,
            release_lock_required=1,
        )
    with pytest.raises(ValueError, match="missing its target"):
        service.store.transition(
            "operation-1",
            OutboxState.AWAITING_APPROVAL,
            reason="review",
            now=NOW,
            release_lock_required=True,
        )
    with pytest.raises(ThreadsStoreError, match="required unknown-effect"):
        service.store.transition(
            "operation-1",
            OutboxState.AWAITING_APPROVAL,
            reason="review",
            now=NOW,
            release_lock_target_operation_id="operation-1",
            release_lock_kind="replacement_publication",
            release_lock_required=True,
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
    with pytest.raises(ValueError, match="provider container"):
        service.store.update_sending_evidence(
            "operation-1", "evidence", now=NOW, provider_container_id="bad value"
        )
    with pytest.raises(ThreadsStoreError, match="unknown state"):
        service.store.note_reconciliation("operation-1", "unresolved", now=NOW)


def test_unknown_effect_lock_validation_fails_closed(tmp_path):
    service = make_service(tmp_path)
    propose(service)
    assert (
        service.store.operator_decision_reference("operation-1", "abandon_unknown")
        is None
    )
    with pytest.raises(ThreadsStoreError, match="no longer reservable"):
        service.store.reserve_publication_effect(
            operation_id="operation-1",
            account_ref="account-1",
            replacement_target_operation_id="operation-1",
            now=NOW,
        )
    with pytest.raises(ThreadsStoreError, match="target is unavailable"):
        service.store.claim_reconciliation(
            operation_id="operation-1", account_ref="account-1", now=NOW
        )
    with pytest.raises(ValueError, match="pre-commit check"):
        service.store.recover_reconciliation_claim(
            operation_id="operation-1",
            account_ref="account-1",
            approval_reference="approval-recovery",
            now=NOW,
            precommit_check=object(),
        )
    with pytest.raises(ValueError, match="lock kind"):
        service.store.release_unknown_effect_lock(
            target_operation_id="operation-1",
            holder_operation_id="operation-1",
            lock_kind="invalid",
            required=True,
        )
    with pytest.raises(ValueError, match="release requirement"):
        service.store.release_unknown_effect_lock(
            target_operation_id="operation-1",
            holder_operation_id="operation-1",
            lock_kind="reconciliation",
            required=1,
        )
    with pytest.raises(ThreadsStoreError, match="reservation is missing"):
        service.store.release_unknown_effect_lock(
            target_operation_id="operation-1",
            holder_operation_id="operation-1",
            lock_kind="reconciliation",
            required=True,
        )
    with pytest.raises(ValueError, match="account-effect release"):
        service.store.transition(
            "operation-1",
            OutboxState.AWAITING_APPROVAL,
            reason="review",
            now=NOW,
            account_effect_lock_required=True,
        )
    with pytest.raises(ValueError, match="account-effect release requirement"):
        service.store.transition(
            "operation-1",
            OutboxState.AWAITING_APPROVAL,
            reason="review",
            now=NOW,
            release_account_effect_lock=1,
        )
    with pytest.raises(ValueError, match="account-effect lock requirement"):
        service.store.transition(
            "operation-1",
            OutboxState.AWAITING_APPROVAL,
            reason="review",
            now=NOW,
            account_effect_lock_required=1,
        )
    with pytest.raises(ThreadsStoreError, match="account-effect reservation"):
        service.store.transition(
            "operation-1",
            OutboxState.AWAITING_APPROVAL,
            reason="review",
            now=NOW,
            release_account_effect_lock=True,
            account_effect_lock_required=True,
        )
    with pytest.raises(ValueError, match="account reference"):
        with service.store.account_dispatch_guard("bad value"):
            pass
    with pytest.raises(ValueError, match="expected outbox state"):
        service.store.record_policy_denial(
            "operation-1",
            expected_state="proposed",
            reason="authorize_policy_denied_unauthenticated",
            now=NOW,
        )
    with pytest.raises(ThreadsStoreError, match="state changed"):
        service.store.record_policy_denial(
            "missing",
            expected_state=OutboxState.PROPOSED,
            reason="authorize_policy_denied_unauthenticated",
            now=NOW,
        )
    with pytest.raises(ThreadsStoreError, match="state changed"):
        service.store.record_policy_denial(
            "operation-1",
            expected_state=OutboxState.READY,
            reason="authorize_policy_denied_unauthenticated",
            now=NOW,
        )


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
    with pytest.raises(ValueError, match="simulated approval"):
        service.authorization_provider.set_approval("forged")
    with pytest.raises(ValueError, match="operation ID"):
        service.authorization_provider.set_privacy("account-1", "bad operation", True)
    with pytest.raises(ValueError, match="privacy decision"):
        service.authorization_provider.set_privacy("account-1", "operation-1", 1)
    with pytest.raises(ValueError, match="fault"):
        service.simulator.set_fault("operation-1", "publish", "none")
    _, approval = ready(service)
    service.simulator.set_fault("operation-1", "publish", Fault.STOP_IN_FLIGHT)
    assert service.dispatch("operation-1", approval, now=NOW).state is (
        OutboxState.CONFIRMED
    )
