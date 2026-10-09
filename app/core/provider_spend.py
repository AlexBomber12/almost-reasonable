"""Durable provider suspension and privacy-safe usage telemetry."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import sqlite3

from .provider_contract import (
    PROVIDER_NAME,
    CostEstimateStatus,
    require_identifier,
)
from .store import EventStore, EventStoreError


CONFIRMED_SPEND_LIMIT_CODES = frozenset(
    ("project_spend_limit_exceeded", "organization_spend_limit_exceeded")
)


class ProviderPersistenceError(RuntimeError):
    """Sanitized fail-closed persistence error."""


@dataclass(frozen=True, slots=True)
class ProviderAttemptRecord:
    attempt_id: str
    correlation_id: str
    provider: str
    model_identifier: str
    provider_config_version: str
    prompt_version: str
    core_self_version: str
    provider_response_id: str | None
    started_at: datetime
    duration_ms: int
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    outcome: str
    retry_count: int
    estimated_cost: str | None
    pricing_version: str | None
    currency: str | None
    cost_estimate_status: CostEstimateStatus
    provider_error_code: str | None


@dataclass(frozen=True, slots=True)
class SpendSuspension:
    provider: str
    confirmed_at: datetime
    provider_error_code: str
    provider_config_version: str
    attempt_id: str


class ProviderStateStore:
    """Purpose-specific view of provider tables in the application database."""

    def __init__(self, event_store: EventStore):
        if type(event_store) is not EventStore:
            raise TypeError("event_store must be an EventStore")
        self._event_store = event_store

    def is_spend_suspended(self) -> bool:
        return self.suspension() is not None

    def suspension(self) -> SpendSuspension | None:
        connection = self._open()
        try:
            with connection:
                connection.execute("BEGIN")
                EventStore._verify_current_schema(connection)
                row = connection.execute(
                    """
                    SELECT provider, confirmed_at, provider_error_code,
                           provider_config_version, attempt_id
                    FROM provider_spend_suspensions
                    WHERE provider = ?
                    """,
                    (PROVIDER_NAME,),
                ).fetchone()
            if row is None:
                return None
            confirmed_at = datetime.fromisoformat(row[1])
            if EventStore._serialize_time(confirmed_at) != row[1]:
                raise ValueError("noncanonical timestamp")
            return SpendSuspension(row[0], confirmed_at, row[2], row[3], row[4])
        except (sqlite3.Error, TypeError, ValueError, EventStoreError):
            raise ProviderPersistenceError("provider state operation failed") from None
        finally:
            connection.close()

    def attempt_exists(self, attempt_id: str) -> bool:
        require_identifier("attempt id", attempt_id)
        connection = self._open()
        try:
            with connection:
                connection.execute("BEGIN")
                EventStore._verify_current_schema(connection)
                row = connection.execute(
                    "SELECT 1 FROM provider_usage_attempts WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
            return row is not None
        except (sqlite3.Error, EventStoreError):
            raise ProviderPersistenceError("provider state operation failed") from None
        finally:
            connection.close()

    def record_attempt(
        self,
        record: ProviderAttemptRecord,
        *,
        confirmed_spend_limit_code: str | None = None,
    ) -> bool:
        timestamp = _validate_record(record)
        if (
            confirmed_spend_limit_code is not None
            and confirmed_spend_limit_code not in CONFIRMED_SPEND_LIMIT_CODES
        ):
            raise ValueError("unconfirmed spend-limit code")
        if confirmed_spend_limit_code != record.provider_error_code and (
            confirmed_spend_limit_code is not None
        ):
            raise ValueError("spend-limit code does not match attempt")
        connection = self._open()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                EventStore._verify_current_schema(connection)
                existing = connection.execute(
                    "SELECT 1 FROM provider_usage_attempts WHERE attempt_id = ?",
                    (record.attempt_id,),
                ).fetchone()
                if existing is not None:
                    return False
                connection.execute(
                    """
                    INSERT INTO provider_usage_attempts(
                        attempt_id, correlation_id, provider, model_identifier,
                        provider_config_version, prompt_version, core_self_version,
                        provider_response_id, started_at, duration_ms,
                        input_tokens, cached_input_tokens, output_tokens,
                        total_tokens, outcome, retry_count, estimated_cost,
                        pricing_version, currency, cost_estimate_status,
                        provider_error_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?)
                    """,
                    (
                        record.attempt_id,
                        record.correlation_id,
                        record.provider,
                        record.model_identifier,
                        record.provider_config_version,
                        record.prompt_version,
                        record.core_self_version,
                        record.provider_response_id,
                        timestamp,
                        record.duration_ms,
                        record.input_tokens,
                        record.cached_input_tokens,
                        record.output_tokens,
                        record.total_tokens,
                        record.outcome,
                        record.retry_count,
                        record.estimated_cost,
                        record.pricing_version,
                        record.currency,
                        record.cost_estimate_status.value,
                        record.provider_error_code,
                    ),
                )
                if confirmed_spend_limit_code is not None:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO provider_spend_suspensions(
                            provider, confirmed_at, provider_error_code,
                            provider_config_version, attempt_id
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            PROVIDER_NAME,
                            timestamp,
                            confirmed_spend_limit_code,
                            record.provider_config_version,
                            record.attempt_id,
                        ),
                    )
            return True
        except (sqlite3.Error, EventStoreError):
            raise ProviderPersistenceError("provider state operation failed") from None
        finally:
            connection.close()

    def usage_attempt(self, attempt_id: str) -> ProviderAttemptRecord:
        require_identifier("attempt id", attempt_id)
        connection = self._open()
        try:
            with connection:
                connection.execute("BEGIN")
                EventStore._verify_current_schema(connection)
                row = connection.execute(
                    """
                    SELECT attempt_id, correlation_id, provider, model_identifier,
                           provider_config_version, prompt_version, core_self_version,
                           provider_response_id, started_at, duration_ms,
                           input_tokens, cached_input_tokens, output_tokens,
                           total_tokens, outcome, retry_count, estimated_cost,
                           pricing_version, currency, cost_estimate_status,
                           provider_error_code
                    FROM provider_usage_attempts
                    WHERE attempt_id = ?
                    """,
                    (attempt_id,),
                ).fetchone()
            if row is None:
                raise ProviderPersistenceError("provider state operation failed")
            values = list(row)
            values[8] = datetime.fromisoformat(values[8])
            values[19] = CostEstimateStatus(values[19])
            record = ProviderAttemptRecord(*values)
            _validate_record(record)
            return record
        except ProviderPersistenceError:
            raise
        except (sqlite3.Error, TypeError, ValueError, EventStoreError):
            raise ProviderPersistenceError("provider state operation failed") from None
        finally:
            connection.close()

    def usage_attempt_count(self) -> int:
        connection = self._open()
        try:
            with connection:
                connection.execute("BEGIN")
                EventStore._verify_current_schema(connection)
                return connection.execute(
                    "SELECT COUNT(*) FROM provider_usage_attempts"
                ).fetchone()[0]
        except (sqlite3.Error, EventStoreError):
            raise ProviderPersistenceError("provider state operation failed") from None
        finally:
            connection.close()

    def _open(self) -> sqlite3.Connection:
        try:
            return self._event_store._open()
        except EventStoreError:
            raise ProviderPersistenceError("provider state operation failed") from None


def _validate_record(record: ProviderAttemptRecord) -> str:
    if type(record) is not ProviderAttemptRecord:
        raise TypeError("record must be a ProviderAttemptRecord")
    for field, value in (
        ("attempt id", record.attempt_id),
        ("correlation id", record.correlation_id),
        ("model identifier", record.model_identifier),
        ("provider configuration version", record.provider_config_version),
        ("prompt version", record.prompt_version),
        ("Core Self version", record.core_self_version),
        ("outcome", record.outcome),
    ):
        require_identifier(field, value)
    if record.provider != PROVIDER_NAME:
        raise ValueError("provider is invalid")
    if record.provider_response_id is not None:
        require_identifier("provider response id", record.provider_response_id)
    if record.provider_error_code is not None:
        require_identifier("provider error code", record.provider_error_code)
    if not isinstance(record.started_at, datetime) or record.started_at.tzinfo is None:
        raise ValueError("attempt timestamp is invalid")
    timestamp = EventStore._serialize_time(record.started_at)
    for value in (
        record.duration_ms,
        record.retry_count,
    ):
        if type(value) is not int or value < 0:
            raise ValueError("attempt counter is invalid")
    tokens = (
        record.input_tokens,
        record.cached_input_tokens,
        record.output_tokens,
        record.total_tokens,
    )
    if any(
        value is not None and (type(value) is not int or value < 0) for value in tokens
    ):
        raise ValueError("attempt usage is invalid")
    if type(record.cost_estimate_status) is not CostEstimateStatus:
        raise ValueError("cost estimate status is invalid")
    if record.cost_estimate_status is CostEstimateStatus.ESTIMATED:
        if None in (record.estimated_cost, record.pricing_version, record.currency):
            raise ValueError("estimated cost metadata is incomplete")
        try:
            amount = Decimal(record.estimated_cost)
        except (InvalidOperation, TypeError):
            raise ValueError("estimated cost is invalid") from None
        if not amount.is_finite() or amount < 0:
            raise ValueError("estimated cost is invalid")
        require_identifier("pricing version", record.pricing_version)
        if record.currency != "USD":
            raise ValueError("estimated cost currency is invalid")
    elif any(
        value is not None
        for value in (record.estimated_cost, record.pricing_version, record.currency)
    ):
        raise ValueError("unavailable cost must not have a value")
    return timestamp
