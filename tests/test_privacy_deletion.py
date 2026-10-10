from datetime import UTC, datetime

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
    assert record.disposition is DeletionDisposition.APPLIED
    assert "event-001" in repr(record)
    assert "synthetic deleted private message" not in repr(record)


def test_invalid_authorization_and_ineligible_are_explicit(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    coordinator = DeletionCoordinator(ledger, FakeErasureAdapter())
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
    assert (
        coordinator._terminal(
            request(),
            DeletionDisposition.INVALID_AUTHORIZATION,
            "wrong_principal",
        )
        == wrong
    )
    assert chat.disposition is DeletionDisposition.INVALID_AUTHORIZATION
    assert ineligible.disposition is DeletionDisposition.NOT_ELIGIBLE


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
    outcomes = DeletionCoordinator(restarted_ledger, adapter).reconcile_pending()
    assert [outcome.disposition for outcome in outcomes] == [
        DeletionDisposition.APPLIED
    ]
    assert adapter.calls == ["delete-operation-001"]


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
            request(), DeletionDisposition.PENDING, "ignored-duplicate-reason"
        )
        is False
    )
    with pytest.raises(ValueError, match="operation identity conflict"):
        ledger.insert_deletion(
            request(content_reference="event-003"),
            DeletionDisposition.PENDING,
            "awaiting_erasure",
        )
