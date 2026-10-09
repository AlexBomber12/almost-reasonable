from dataclasses import replace
from datetime import UTC, datetime
import sqlite3
from threading import Barrier, Thread

import pytest

from app.core import store as store_module
from app.core.provider_contract import CostEstimateStatus
from app.core.provider_spend import (
    ProviderAttemptRecord,
    ProviderPersistenceError,
    ProviderStateStore,
)
from app.core.store import EventStore, FaultPoint, SCHEMA_VERSION


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def attempt(identifier="operation-a:1", outcome="completed"):
    error_code = (
        "project_spend_limit_exceeded" if outcome == "confirmed_spend_limit" else None
    )
    return ProviderAttemptRecord(
        attempt_id=identifier,
        correlation_id="correlation-a",
        provider="openai",
        model_identifier="gpt-6-luna",
        provider_config_version="openai-config-v1",
        prompt_version="agent-proposal-v1",
        core_self_version="core-self-v1",
        provider_response_id="resp_synthetic",
        started_at=NOW,
        duration_ms=25,
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=5,
        total_tokens=15,
        outcome=outcome,
        retry_count=0,
        estimated_cost="0.0000033",
        pricing_version="pricing-v1",
        currency="USD",
        cost_estimate_status=CostEstimateStatus.ESTIMATED,
        provider_error_code=error_code,
    )


def create_v2_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in (*store_module._SCHEMA_V1, *store_module._SCHEMA_V2):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_metadata(singleton, version) VALUES (1, 2)"
        )
        connection.execute(
            """
            INSERT INTO private_payloads(
                payload_id, content, audience_type, audience_principal_id,
                provenance_type, verification_status, created_at
            ) VALUES ('payload-v2', 'synthetic content', 'private_participant',
                      'participant-a', 'participant_assertion', 'unverified',
                      '2026-01-02T03:04:05.000000Z')
            """
        )
        connection.execute(
            """
            INSERT INTO event_envelopes(
                event_id, occurred_at, event_type, channel, correlation_id,
                authenticated_source_ref, source_message_id, audience_type,
                audience_principal_id, provenance_type, verification_status,
                payload_id
            ) VALUES ('event-v2', '2026-01-02T03:04:05.000000Z',
                      'participant_input', 'private_chat', 'correlation-v2',
                      'session-a', 'message-v2', 'private_participant',
                      'participant-a', 'participant_assertion', 'unverified',
                      'payload-v2')
            """
        )
        connection.execute(
            """
            INSERT INTO protected_event_taints(
                event_id, source_event_id, source_principal_id,
                source_provenance_type
            ) VALUES ('event-v2', 'event-v2', 'participant-a',
                      'participant_assertion')
            """
        )
        connection.execute(
            """
            INSERT INTO state_items(
                item_id, state_type, scope, audience_type,
                audience_principal_id, current_revision, created_at, updated_at
            ) VALUES ('state-v2', 'interest', 'private_candidate',
                      'private_participant', 'participant-a', 0,
                      '2026-01-02T03:04:05.000000Z',
                      '2026-01-02T03:04:05.000000Z')
            """
        )
        connection.execute("PRAGMA user_version = 2")


def test_v2_to_v3_migration_is_atomic_and_preserves_existing_data(tmp_path):
    database = tmp_path / "events.sqlite3"
    create_v2_database(database)

    store = EventStore(database)
    reopened = EventStore(database)

    assert store.diagnostics().schema_version == SCHEMA_VERSION == 3
    assert reopened.diagnostics().schema_version == 3
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
        assert connection.execute(
            "SELECT event_id FROM event_envelopes"
        ).fetchone() == ("event-v2",)
        assert connection.execute(
            "SELECT source_event_id FROM protected_event_taints"
        ).fetchone() == ("event-v2",)
        assert connection.execute("SELECT item_id FROM state_items").fetchone() == (
            "state-v2",
        )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"provider_spend_suspensions", "provider_usage_attempts"} <= tables


def test_failed_v3_migration_rolls_back_to_recoverable_v2(tmp_path):
    database = tmp_path / "events.sqlite3"
    create_v2_database(database)

    def fail(point):
        if point is FaultPoint.BEFORE_INITIALIZATION_LOCK:
            return
        assert point is FaultPoint.BEFORE_V3_MIGRATION_COMMIT
        raise RuntimeError("synthetic v3 migration failure")

    with pytest.raises(RuntimeError, match="synthetic v3 migration failure"):
        EventStore(database, fault_injector=fail)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute("SELECT version FROM schema_metadata").fetchone() == (
            2,
        )
        assert connection.execute("SELECT item_id FROM state_items").fetchone() == (
            "state-v2",
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'provider_usage_attempts'"
        ).fetchone() == (0,)

    assert EventStore(database).diagnostics().schema_version == 3


def test_concurrent_v2_initialization_is_safe(tmp_path):
    database = tmp_path / "events.sqlite3"
    create_v2_database(database)
    barrier = Barrier(3)
    outcomes = []

    def initialize():
        barrier.wait()
        try:
            outcomes.append(EventStore(database).diagnostics().schema_version)
        except Exception as error:
            outcomes.append(error)

    threads = [Thread(target=initialize) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert outcomes == [3, 3]


def test_confirmed_spend_suspension_and_telemetry_are_atomic_and_durable(tmp_path):
    database = tmp_path / "events.sqlite3"
    state = ProviderStateStore(EventStore(database))

    created = state.record_attempt(
        attempt(outcome="confirmed_spend_limit"),
        confirmed_spend_limit_code="project_spend_limit_exceeded",
    )
    duplicate = state.record_attempt(
        attempt(outcome="confirmed_spend_limit"),
        confirmed_spend_limit_code="project_spend_limit_exceeded",
    )
    reopened = ProviderStateStore(EventStore(database))

    assert created is True
    assert duplicate is False
    assert reopened.is_spend_suspended() is True
    assert reopened.attempt_exists("operation-a:1") is True
    assert reopened.usage_attempt_count() == 1
    suspension = reopened.suspension()
    assert suspension.provider == "openai"
    assert suspension.provider_error_code == "project_spend_limit_exceeded"
    assert suspension.attempt_id == "operation-a:1"


def test_ordinary_attempt_does_not_suspend_and_missing_usage_stays_null(tmp_path):
    state = ProviderStateStore(EventStore(tmp_path / "events.sqlite3"))
    missing = attempt()
    missing = ProviderAttemptRecord(
        **{
            field: getattr(missing, field)
            for field in missing.__dataclass_fields__
            if field
            not in {
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "total_tokens",
                "estimated_cost",
                "pricing_version",
                "currency",
                "cost_estimate_status",
            }
        },
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        estimated_cost=None,
        pricing_version=None,
        currency=None,
        cost_estimate_status=CostEstimateStatus.USAGE_UNAVAILABLE,
    )

    state.record_attempt(missing)

    assert state.is_spend_suspended() is False
    row = state.usage_attempt("operation-a:1")
    assert row.input_tokens is None
    assert row.estimated_cost is None
    assert row.cost_estimate_status is CostEstimateStatus.USAGE_UNAVAILABLE


def test_invalid_records_and_suspension_codes_fail_without_writes(tmp_path):
    state = ProviderStateStore(EventStore(tmp_path / "events.sqlite3"))

    with pytest.raises(ValueError):
        state.record_attempt(attempt(identifier="bad value"))
    with pytest.raises(ValueError):
        state.record_attempt(
            attempt(), confirmed_spend_limit_code="rate_limit_exceeded"
        )

    assert state.usage_attempt_count() == 0
    assert state.is_spend_suspended() is False


def test_storage_errors_are_sanitized(tmp_path, monkeypatch):
    state = ProviderStateStore(EventStore(tmp_path / "events.sqlite3"))

    def fail_open():
        raise store_module.EventStoreBusyError("private synthetic detail")

    monkeypatch.setattr(state._event_store, "_open", fail_open)

    with pytest.raises(
        ProviderPersistenceError, match="provider state operation failed"
    ):
        state.record_attempt(attempt())
    with pytest.raises(
        ProviderPersistenceError, match="provider state operation failed"
    ):
        state.is_spend_suspended()


def test_provider_state_store_requires_the_application_event_store():
    with pytest.raises(TypeError):
        ProviderStateStore(object())


def test_persisted_rows_are_revalidated_and_corruption_fails_closed(tmp_path):
    database = tmp_path / "events.sqlite3"
    state = ProviderStateStore(EventStore(database))
    state.record_attempt(
        attempt(outcome="confirmed_spend_limit"),
        confirmed_spend_limit_code="project_spend_limit_exceeded",
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE provider_spend_suspensions "
            "SET confirmed_at = '2026-10-10T12:00:00+00:00'"
        )

    with pytest.raises(ProviderPersistenceError):
        state.suspension()

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE provider_usage_attempts SET cost_estimate_status = 'invented'"
        )

    with pytest.raises(ProviderPersistenceError):
        state.usage_attempt("operation-a:1")


def test_missing_attempt_and_missing_provider_tables_fail_closed(tmp_path):
    database = tmp_path / "events.sqlite3"
    state = ProviderStateStore(EventStore(database))

    with pytest.raises(ProviderPersistenceError):
        state.usage_attempt("missing:1")

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE provider_usage_attempts")

    with pytest.raises(ProviderPersistenceError):
        state.attempt_exists("operation-a:1")
    with pytest.raises(ProviderPersistenceError):
        state.usage_attempt_count()
    with pytest.raises(ProviderPersistenceError):
        state.record_attempt(attempt())


def test_matching_confirmed_code_is_required(tmp_path):
    state = ProviderStateStore(EventStore(tmp_path / "events.sqlite3"))

    with pytest.raises(ValueError, match="does not match"):
        state.record_attempt(
            attempt(outcome="confirmed_spend_limit"),
            confirmed_spend_limit_code="organization_spend_limit_exceeded",
        )


@pytest.mark.parametrize(
    "record",
    [
        object(),
        replace(attempt(), provider="other"),
        replace(attempt(), started_at="not-a-time"),
        replace(attempt(), started_at=datetime(2026, 1, 1)),
        replace(attempt(), duration_ms=-1),
        replace(attempt(), retry_count=True),
        replace(attempt(), input_tokens=-1),
        replace(attempt(), cost_estimate_status="estimated"),
        replace(attempt(), estimated_cost=None),
        replace(attempt(), estimated_cost="not-a-decimal"),
        replace(attempt(), estimated_cost="NaN"),
        replace(attempt(), currency="EUR"),
        replace(
            attempt(),
            cost_estimate_status=CostEstimateStatus.PRICING_UNAVAILABLE,
        ),
    ],
)
def test_attempt_record_validation_rejects_malformed_telemetry(tmp_path, record):
    state = ProviderStateStore(EventStore(tmp_path / "events.sqlite3"))

    with pytest.raises((TypeError, ValueError)):
        state.record_attempt(record)

    assert state.usage_attempt_count() == 0


def test_repeated_distinct_spend_failures_keep_one_consistent_suspension(tmp_path):
    state = ProviderStateStore(EventStore(tmp_path / "events.sqlite3"))
    first = attempt(outcome="confirmed_spend_limit")
    second = replace(first, attempt_id="operation-b:1")

    state.record_attempt(
        first, confirmed_spend_limit_code="project_spend_limit_exceeded"
    )
    state.record_attempt(
        second, confirmed_spend_limit_code="project_spend_limit_exceeded"
    )

    assert state.usage_attempt_count() == 2
    assert state.suspension().attempt_id == "operation-a:1"


def test_busy_database_fails_closed_without_partial_attempt(tmp_path):
    database = tmp_path / "events.sqlite3"
    event_store = EventStore(database, busy_timeout_ms=1)
    state = ProviderStateStore(event_store)
    holder = event_store._open()
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(ProviderPersistenceError):
            state.record_attempt(attempt())
    finally:
        holder.rollback()
        holder.close()

    assert state.usage_attempt_count() == 0


def test_failed_suspension_insert_rolls_back_telemetry_atomically(tmp_path):
    database = tmp_path / "events.sqlite3"
    state = ProviderStateStore(EventStore(database))
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER synthetic_suspension_failure
            BEFORE INSERT ON provider_spend_suspensions
            BEGIN
                SELECT RAISE(ABORT, 'synthetic private failure');
            END
            """
        )

    with pytest.raises(ProviderPersistenceError):
        state.record_attempt(
            attempt(outcome="confirmed_spend_limit"),
            confirmed_spend_limit_code="project_spend_limit_exceeded",
        )

    assert state.usage_attempt_count() == 0
    assert state.is_spend_suspended() is False


def test_concurrent_spend_attempts_preserve_one_suspension_and_both_records(tmp_path):
    database = tmp_path / "events.sqlite3"
    event_store = EventStore(database)
    barrier = Barrier(3)
    outcomes = []

    def record(identifier):
        barrier.wait()
        try:
            outcomes.append(
                ProviderStateStore(event_store).record_attempt(
                    attempt(identifier, "confirmed_spend_limit"),
                    confirmed_spend_limit_code="project_spend_limit_exceeded",
                )
            )
        except Exception as error:
            outcomes.append(error)

    threads = [
        Thread(target=record, args=("operation-a:1",)),
        Thread(target=record, args=("operation-b:1",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    state = ProviderStateStore(event_store)
    assert outcomes == [True, True]
    assert state.usage_attempt_count() == 2
    assert state.is_spend_suspended() is True
