from datetime import UTC, datetime, timedelta
import sqlite3
import threading

import pytest

from app.privacy import (
    ContentCategory,
    DeletionCoordinator,
    DeletionDisposition,
    DeletionRequest,
    ErasureReport,
    OperationOrigin,
    PrivacyLedger,
    TrustedPrivacyContext,
)


NOW = datetime(2026, 10, 10, 13, 0, tzinfo=UTC)
REQUIRED = frozenset(
    {
        ContentCategory.PRIVATE_PAYLOAD,
        ContentCategory.PRIVATE_MEMORY,
        ContentCategory.SUMMARY,
        ContentCategory.REVISION,
        ContentCategory.SHARED_ABSTRACTION,
        ContentCategory.SEARCH_INDEX,
        ContentCategory.DUPLICATE_KEY,
    }
)


def context(principal="participant-a", origin=OperationOrigin.CONTROL):
    return TrustedPrivacyContext(principal, "actor-a", "auth-a", origin)


def request(**changes):
    values = {
        "operation_id": "delete-operation-001",
        "principal_id": "participant-a",
        "content_reference": "event-001",
        "content_category": ContentCategory.PRIVATE_PAYLOAD,
        "requested_at": NOW,
        "actor_reference": "actor-a",
        "correlation_id": "delete-correlation-001",
        "authentication_event_id": "auth-a",
    }
    values.update(changes)
    return DeletionRequest(**values)


class FakeErasureAdapter:
    def __init__(self, report=None, *, error=None):
        self.report = report or ErasureReport(True, True, REQUIRED, ())
        self.error = error
        self.calls = []

    def erase(self, deletion):
        self.calls.append(deletion.operation_id)
        if self.error:
            raise self.error
        return self.report


def test_deletion_writes_pending_first_then_applies_idempotently(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    adapter = FakeErasureAdapter()
    coordinator = DeletionCoordinator(ledger, adapter)
    first = coordinator.delete(context(), request())
    second = coordinator.delete(context(), request())
    assert first.disposition is DeletionDisposition.APPLIED
    assert second.disposition is DeletionDisposition.APPLIED
    assert adapter.calls == ["delete-operation-001"]
    record = PrivacyLedger(tmp_path / "privacy.sqlite3").deletion(
        "delete-operation-001"
    )
    assert record.operation_id == "delete-operation-001"
    assert record.disposition is DeletionDisposition.APPLIED
    assert "event-001" in repr(record)
    assert "synthetic deleted private message" not in repr(record)
    assert [entry.result_code for entry in ledger.audit_records()] == [
        "pending:awaiting_erasure",
        "applied:erasure_applied",
    ]
    rejected_retry = coordinator.delete(context(origin=OperationOrigin.CHAT), request())
    assert rejected_retry.disposition is DeletionDisposition.INVALID_AUTHORIZATION
    assert rejected_retry.reason == "invalid_authentication"
    assert ledger.audit_records()[-1].action_type == "deletion_rejected"


def test_invalid_authorization_and_ineligible_are_explicit(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    rejection_time = NOW + timedelta(minutes=5)
    coordinator = DeletionCoordinator(
        ledger, FakeErasureAdapter(), clock=lambda: rejection_time
    )
    wrong = coordinator.delete(context("participant-b"), request())
    chat = coordinator.delete(
        context(origin=OperationOrigin.CHAT),
        request(operation_id="delete-operation-chat"),
    )
    ineligible = coordinator.delete(
        context(),
        request(
            operation_id="delete-operation-public",
            content_category=ContentCategory.PUBLIC_ACTION,
        ),
    )
    assert wrong.disposition is DeletionDisposition.INVALID_AUTHORIZATION
    assert coordinator.delete(context("participant-b"), request()) == wrong
    assert ledger.deletions_for_reconciliation() == ()
    recovered = coordinator.delete(context(), request())
    assert recovered.disposition is DeletionDisposition.APPLIED
    assert (
        coordinator._terminal(
            context(),
            request(
                operation_id="delete-operation-public",
                content_category=ContentCategory.PUBLIC_ACTION,
            ),
            DeletionDisposition.NOT_ELIGIBLE,
            "public_action_not_erasable",
        )
        == ineligible
    )
    assert chat.disposition is DeletionDisposition.INVALID_AUTHORIZATION
    assert ineligible.disposition is DeletionDisposition.NOT_ELIGIBLE
    assert {
        entry.occurred_at
        for entry in ledger.audit_records()
        if entry.action_type == "deletion_rejected"
    } == {rejection_time}


def test_ledger_rejects_unauthenticated_direct_deletion_insert(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    generation = ledger.control_generation()

    with pytest.raises(ValueError, match="authentication"):
        ledger.insert_deletion(
            context(origin=OperationOrigin.CHAT),
            request(),
            DeletionDisposition.PENDING,
            "awaiting_erasure",
        )

    with pytest.raises(KeyError):
        ledger.deletion(request().operation_id)
    assert ledger.control_generation() == generation
    assert ledger.audit_records() == ()


def test_partial_or_failed_erasure_is_never_completed(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    partial = ErasureReport(
        payload_removed=True,
        tombstone_written=True,
        invalidated_categories=REQUIRED - {ContentCategory.SEARCH_INDEX},
        blockers=("search_index_not_invalidated",),
    )
    result = DeletionCoordinator(ledger, FakeErasureAdapter(partial)).delete(
        context(), request()
    )
    assert result.disposition is DeletionDisposition.INCOMPLETE_BLOCKED
    assert result.reason == "erasure_incomplete"

    recovered = DeletionCoordinator(ledger, FakeErasureAdapter()).delete(
        context(), request()
    )
    assert recovered.disposition is DeletionDisposition.APPLIED

    failed = DeletionCoordinator(
        ledger, FakeErasureAdapter(error=RuntimeError("synthetic private canary"))
    ).delete(context(), request(operation_id="delete-operation-002"))
    assert failed.disposition is DeletionDisposition.INCOMPLETE_BLOCKED
    assert failed.reason == "adapter_failure"
    assert "synthetic private canary" not in repr(ledger.audit_records())

    malformed = DeletionCoordinator(ledger, FakeErasureAdapter(report=object())).delete(
        context(), request(operation_id="delete-operation-003")
    )
    assert malformed.disposition is DeletionDisposition.INCOMPLETE_BLOCKED
    assert malformed.reason == "invalid_erasure_report"


class SyntheticCrash(BaseException):
    pass


def test_crash_after_ledger_write_leaves_durable_pending_for_reconciliation(tmp_path):
    path = tmp_path / "privacy.sqlite3"
    coordinator = DeletionCoordinator(PrivacyLedger(path), FakeErasureAdapter())

    def crash(_):
        raise SyntheticCrash

    with pytest.raises(SyntheticCrash):
        coordinator.delete(context(), request(), after_pending=crash)

    restarted_ledger = PrivacyLedger(path)
    assert restarted_ledger.deletion("delete-operation-001").disposition is (
        DeletionDisposition.PENDING
    )
    adapter = FakeErasureAdapter()
    completed_at = NOW + timedelta(hours=2)
    outcomes = DeletionCoordinator(
        restarted_ledger, adapter, clock=lambda: completed_at
    ).reconcile_pending()
    assert [outcome.disposition for outcome in outcomes] == [
        DeletionDisposition.APPLIED
    ]
    assert adapter.calls == ["delete-operation-001"]
    assert restarted_ledger.deletion("delete-operation-001").updated_at == completed_at
    assert restarted_ledger.audit_records()[-1].occurred_at == completed_at


def test_missing_provenance_and_identity_conflicts_fail_closed(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    missing = ErasureReport(False, False, frozenset(), ("missing_source_provenance",))
    coordinator = DeletionCoordinator(ledger, FakeErasureAdapter(missing))
    assert coordinator.delete(context(), request()).disposition is (
        DeletionDisposition.INCOMPLETE_BLOCKED
    )
    with pytest.raises(ValueError, match="operation identity conflict"):
        coordinator.delete(context(), request(content_reference="event-002"))

    assert (
        ledger.insert_deletion(
            context(),
            request(),
            DeletionDisposition.PENDING,
            "ignored-duplicate-reason",
        )
        is False
    )
    with pytest.raises(ValueError, match="operation identity conflict"):
        ledger.insert_deletion(
            context(),
            request(content_reference="event-003"),
            DeletionDisposition.PENDING,
            "awaiting_erasure",
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"payload_removed": "false"},
        {"tombstone_written": 1},
        {"invalidated_categories": frozenset({"private_payload"})},
        {"invalidated_categories": set(REQUIRED)},
        {"blockers": ["search_index_not_invalidated"]},
        {"blockers": ("bad blocker",)},
    ],
)
def test_erasure_report_rejects_deserialized_or_malformed_types(changes):
    values = {
        "payload_removed": True,
        "tombstone_written": True,
        "invalidated_categories": REQUIRED,
        "blockers": (),
    }
    values.update(changes)
    with pytest.raises(ValueError, match="erasure report"):
        ErasureReport(**values)


def test_concurrent_deletion_ledger_insertion_is_idempotent(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    barrier = threading.Barrier(3)
    results = []
    errors = []

    def insert():
        barrier.wait()
        try:
            results.append(
                ledger.insert_deletion(
                    context(),
                    request(),
                    DeletionDisposition.PENDING,
                    "awaiting_erasure",
                )
            )
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=insert) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(results) == [False, True]


def test_concurrent_retry_cannot_regress_an_applied_deletion(tmp_path, monkeypatch):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    applied = threading.Event()

    class RacingAdapter:
        def __init__(self):
            self.lock = threading.Lock()
            self.calls = 0

        def erase(self, _deletion):
            with self.lock:
                call = self.calls
                self.calls += 1
            if call == 0:
                assert applied.wait(timeout=2)
                return ErasureReport(False, False, frozenset(), ("delayed_failure",))
            return ErasureReport(True, True, REQUIRED, ())

    real_update = ledger.update_deletion

    def track_update(deletion, disposition, reason, updated_at):
        updated = real_update(deletion, disposition, reason, updated_at)
        if disposition is DeletionDisposition.APPLIED:
            applied.set()
        return updated

    monkeypatch.setattr(ledger, "update_deletion", track_update)
    coordinator = DeletionCoordinator(ledger, RacingAdapter())
    barrier = threading.Barrier(3)
    outcomes = []
    errors = []

    def delete():
        barrier.wait()
        try:
            outcomes.append(coordinator.delete(context(), request()))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=delete) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert errors == []
    assert [outcome.disposition for outcome in outcomes] == [
        DeletionDisposition.APPLIED,
        DeletionDisposition.APPLIED,
    ]
    assert (
        ledger.deletion(request().operation_id).disposition
        is DeletionDisposition.APPLIED
    )
    assert all(
        "incomplete_blocked" not in entry.result_code
        for entry in ledger.audit_records()
    )


def test_deletion_outcome_rolls_back_if_atomic_audit_write_fails(tmp_path, monkeypatch):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    deletion = request()
    ledger.insert_deletion(
        context(), deletion, DeletionDisposition.PENDING, "awaiting_erasure"
    )
    monkeypatch.setattr(
        ledger,
        "_insert_audit",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("audit failure")),
    )
    with pytest.raises(RuntimeError, match="audit failure"):
        DeletionCoordinator(ledger, FakeErasureAdapter()).delete(context(), deletion)
    assert (
        ledger.deletion(deletion.operation_id).disposition
        is DeletionDisposition.PENDING
    )


def test_deletion_insert_rolls_back_if_atomic_audit_write_fails(tmp_path, monkeypatch):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    monkeypatch.setattr(
        ledger,
        "_insert_audit",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("audit failure")),
    )
    with pytest.raises(RuntimeError, match="audit failure"):
        DeletionCoordinator(ledger, FakeErasureAdapter()).delete(context(), request())
    with pytest.raises(KeyError):
        ledger.deletion(request().operation_id)


def test_deletion_insert_rolls_back_when_control_generation_is_missing(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    deletion = request()
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("DELETE FROM privacy_control_generation")

    with pytest.raises(RuntimeError, match="generation"):
        ledger.insert_deletion(
            context(), deletion, DeletionDisposition.PENDING, "awaiting_erasure"
        )

    with pytest.raises(KeyError):
        ledger.deletion(deletion.operation_id)
    assert ledger.audit_records() == ()


def test_applied_deletion_requires_a_verified_erasure_report(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    deletion = request()
    generation_before = ledger.control_generation()

    with pytest.raises(ValueError, match="disposition"):
        ledger.insert_deletion(context(), deletion, "pending", "awaiting_erasure")
    with pytest.raises(ValueError, match="verified erasure"):
        ledger.insert_deletion(
            context(), deletion, DeletionDisposition.APPLIED, "erasure_applied"
        )
    with pytest.raises(KeyError):
        ledger.deletion(deletion.operation_id)
    assert ledger.control_generation() == generation_before
    assert ledger.audit_records() == ()

    ledger.insert_deletion(
        context(), deletion, DeletionDisposition.PENDING, "awaiting_erasure"
    )
    audit_before = ledger.audit_records()
    with pytest.raises(ValueError, match="disposition"):
        ledger.update_deletion(deletion, "pending", "awaiting_erasure", NOW)
    with pytest.raises(ValueError, match="verified erasure"):
        ledger.update_deletion(
            deletion,
            DeletionDisposition.APPLIED,
            "erasure_applied",
            NOW,
        )
    incomplete_reports = (
        None,
        ErasureReport(False, True, REQUIRED, ()),
        ErasureReport(True, False, REQUIRED, ()),
        ErasureReport(True, True, REQUIRED, ("erasure_incomplete",)),
        ErasureReport(True, True, frozenset(), ()),
    )
    for report in incomplete_reports:
        with pytest.raises(ValueError, match="verified erasure"):
            ledger._record_verified_deletion(deletion, report, NOW)

    assert ledger.deletion(deletion.operation_id).disposition is (
        DeletionDisposition.PENDING
    )
    assert ledger.audit_records() == audit_before


def test_verified_deletion_transition_is_not_public(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    deletion = request()
    ledger.insert_deletion(
        context(), deletion, DeletionDisposition.PENDING, "awaiting_erasure"
    )
    forged_report = ErasureReport(True, True, REQUIRED, ())

    with pytest.raises(AttributeError):
        ledger.record_verified_deletion(deletion, forged_report, NOW)

    assert (
        ledger.deletion(deletion.operation_id).disposition
        is DeletionDisposition.PENDING
    )


def test_deletion_update_rejects_a_missing_operation(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    deletion = request(operation_id="delete-operation-missing")
    with pytest.raises(KeyError, match="delete-operation-missing"):
        ledger.update_deletion(
            deletion,
            DeletionDisposition.INCOMPLETE_BLOCKED,
            "adapter_failure",
            deletion.requested_at,
        )
    assert ledger.audit_records() == ()

    existing = request()
    ledger.insert_deletion(
        context(), existing, DeletionDisposition.PENDING, "awaiting_erasure"
    )
    with pytest.raises(ValueError, match="operation identity conflict"):
        ledger.update_deletion(
            request(content_reference="event-other"),
            DeletionDisposition.INCOMPLETE_BLOCKED,
            "adapter_failure",
            existing.requested_at,
        )
    assert (
        ledger.deletion(existing.operation_id).disposition
        is DeletionDisposition.PENDING
    )
