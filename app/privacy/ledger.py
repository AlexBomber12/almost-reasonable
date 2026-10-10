"""Minimal, payload-free SQLite privacy ledger."""

from collections.abc import Callable
from datetime import UTC, datetime
import os
from pathlib import Path
import sqlite3

from .models import (
    AuditRecord,
    ConsentGrant,
    ConsentPurpose,
    ConsentUseResult,
    DeletionDisposition,
    DeletionRecord,
    DeletionRequest,
    PrivacyReconciliationSnapshot,
    RestoreDisposition,
)


PRIVACY_LEDGER_SCHEMA_VERSION = 1


_SCHEMA = (
    """CREATE TABLE privacy_schema_metadata(
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        version INTEGER NOT NULL
    )""",
    """CREATE TABLE consent_grants(
        consent_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE,
        principal_id TEXT NOT NULL,
        content_reference TEXT NOT NULL,
        audience TEXT NOT NULL,
        purpose TEXT NOT NULL,
        granted_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        actor_reference TEXT NOT NULL,
        revision INTEGER NOT NULL,
        correlation_id TEXT NOT NULL,
        authentication_event_id TEXT NOT NULL
    )""",
    """CREATE TABLE privacy_control_generation(
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        generation INTEGER NOT NULL CHECK(generation >= 0)
    )""",
    """CREATE TABLE consent_revocations(
        consent_id TEXT PRIMARY KEY REFERENCES consent_grants(consent_id),
        operation_id TEXT NOT NULL UNIQUE,
        principal_id TEXT NOT NULL,
        actor_reference TEXT NOT NULL,
        authentication_event_id TEXT NOT NULL,
        revoked_at TEXT NOT NULL,
        correlation_id TEXT NOT NULL
    )""",
    """CREATE TABLE deletion_records(
        operation_id TEXT PRIMARY KEY,
        principal_id TEXT NOT NULL,
        content_reference TEXT NOT NULL,
        content_category TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        actor_reference TEXT NOT NULL,
        correlation_id TEXT NOT NULL,
        authentication_event_id TEXT NOT NULL,
        disposition TEXT NOT NULL,
        reason TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE privacy_audit(
        audit_key INTEGER PRIMARY KEY,
        correlation_id TEXT NOT NULL,
        actor_reference TEXT NOT NULL,
        action_type TEXT NOT NULL,
        record_category TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        result_code TEXT NOT NULL,
        redaction_status TEXT NOT NULL
    )""",
    """CREATE TABLE notice_acknowledgements(
        principal_id TEXT NOT NULL,
        notice_version TEXT NOT NULL,
        actor_reference TEXT NOT NULL,
        authentication_event_id TEXT NOT NULL,
        acknowledged_at TEXT NOT NULL,
        correlation_id TEXT NOT NULL,
        PRIMARY KEY(principal_id, notice_version)
    )""",
    """CREATE TABLE restore_outcomes(
        restore_id TEXT PRIMARY KEY,
        occurred_at TEXT NOT NULL,
        disposition TEXT NOT NULL,
        reason TEXT NOT NULL
    )""",
)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class PrivacyLedger:
    """An independently committed control store; it contains no private payload."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path == Path(":memory:"):
            raise ValueError("in-memory privacy ledgers are not supported")
        if self.path.exists() and self.path.is_symlink():
            raise ValueError("privacy ledger must not be a symbolic link")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='privacy_schema_metadata'"
            ).fetchone()
            if tables is None:
                unrelated = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
                if unrelated is not None:
                    raise RuntimeError("privacy ledger path contains unrelated tables")
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO privacy_schema_metadata VALUES(1, ?)",
                    (PRIVACY_LEDGER_SCHEMA_VERSION,),
                )
                connection.execute(
                    "INSERT INTO privacy_control_generation VALUES(1, 0)"
                )
            version = connection.execute(
                "SELECT version FROM privacy_schema_metadata WHERE singleton=1"
            ).fetchone()
            if version != (PRIVACY_LEDGER_SCHEMA_VERSION,):
                raise RuntimeError("unsupported privacy ledger schema")

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.path}{suffix}")
            if sidecar.exists():
                os.chmod(sidecar, 0o600, follow_symlinks=False)
        return connection

    def insert_consent(self, grant: ConsentGrant) -> bool:
        values = (
            grant.consent_id,
            grant.operation_id,
            grant.principal_id,
            grant.content_reference,
            grant.audience,
            grant.purpose.value,
            _stamp(grant.granted_at),
            _stamp(grant.expires_at),
            grant.actor_reference,
            grant.revision,
            grant.correlation_id,
            grant.authentication_event_id,
        )
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT consent_id, operation_id, principal_id, content_reference, audience, "
                "purpose, granted_at, expires_at, actor_reference, revision, correlation_id, "
                "authentication_event_id FROM consent_grants "
                "WHERE operation_id=? OR consent_id=?",
                (grant.operation_id, grant.consent_id),
            ).fetchone()
            created = existing is None
            if existing is not None:
                if existing != values:
                    raise ValueError("consent operation identity conflict")
            else:
                connection.execute(
                    "INSERT INTO consent_grants VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", values
                )
            self._insert_audit(
                connection,
                AuditRecord(
                    grant.correlation_id,
                    grant.actor_reference,
                    "consent_grant",
                    "consent",
                    grant.granted_at,
                    "granted" if created else "already_granted",
                    "payload_free",
                ),
            )
        return created

    def authorize_consent_use(
        self,
        *,
        principal_id: str,
        content_reference: str,
        audience: str,
        purpose: ConsentPurpose,
        used_at: datetime,
        correlation_id: str,
        actor_reference: str,
        dispatch: Callable[[], None],
    ) -> ConsentUseResult:
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT g.consent_id, g.granted_at, g.expires_at, r.revoked_at "
                "FROM consent_grants g LEFT JOIN consent_revocations r "
                "ON r.consent_id=g.consent_id WHERE g.principal_id=? "
                "AND g.content_reference=? AND g.audience=? AND g.purpose=? "
                "ORDER BY g.revision DESC LIMIT 1",
                (principal_id, content_reference, audience, purpose.value),
            ).fetchone()
            if row is None:
                result = ConsentUseResult(False, "no_matching_consent")
            else:
                consent_id = row[0]
                granted_at = _time(row[1])
                expires_at = _time(row[2])
                revoked_at = None if row[3] is None else _time(row[3])
                if used_at < granted_at:
                    result = ConsentUseResult(False, "not_yet_valid", consent_id)
                elif revoked_at is not None and used_at >= revoked_at:
                    result = ConsentUseResult(False, "revoked", consent_id)
                elif used_at >= expires_at:
                    result = ConsentUseResult(False, "expired", consent_id)
                else:
                    result = ConsentUseResult(True, "authorized", consent_id)
            self._insert_audit(
                connection,
                AuditRecord(
                    correlation_id,
                    actor_reference,
                    "consent_use",
                    "consent",
                    used_at,
                    result.reason,
                    "payload_free",
                ),
            )
            if result.allowed:
                dispatch()
        return result

    def consent_principal(self, consent_id: str) -> str | None:
        with self._open() as connection:
            row = connection.execute(
                "SELECT principal_id FROM consent_grants WHERE consent_id=?",
                (consent_id,),
            ).fetchone()
        return None if row is None else row[0]

    def insert_revocation(
        self,
        *,
        consent_id: str,
        operation_id: str,
        principal_id: str,
        actor_reference: str,
        authentication_event_id: str,
        revoked_at: datetime,
        correlation_id: str,
    ) -> bool:
        values = (
            consent_id,
            operation_id,
            principal_id,
            actor_reference,
            authentication_event_id,
            _stamp(revoked_at),
            correlation_id,
        )
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT consent_id, operation_id, principal_id, actor_reference, "
                "authentication_event_id, revoked_at, correlation_id "
                "FROM consent_revocations WHERE consent_id=? OR operation_id=?",
                (consent_id, operation_id),
            ).fetchone()
            created = existing is None
            if existing is not None:
                if existing != values:
                    raise ValueError("revocation operation identity conflict")
            else:
                connection.execute(
                    "INSERT INTO consent_revocations VALUES(?,?,?,?,?,?,?)", values
                )
                connection.execute(
                    "UPDATE privacy_control_generation SET generation=generation+1 "
                    "WHERE singleton=1"
                )
            self._insert_audit(
                connection,
                AuditRecord(
                    correlation_id,
                    actor_reference,
                    "consent_revocation",
                    "consent",
                    revoked_at,
                    "revoked" if created else "already_revoked",
                    "payload_free",
                ),
            )
        return created

    def revocations(self) -> tuple[tuple[str, datetime], ...]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT consent_id, revoked_at FROM consent_revocations ORDER BY revoked_at"
            ).fetchall()
        return tuple((row[0], _time(row[1])) for row in rows)

    def insert_deletion(
        self, request: DeletionRequest, disposition: DeletionDisposition, reason: str
    ) -> bool:
        values = (
            request.operation_id,
            request.principal_id,
            request.content_reference,
            request.content_category.value,
            _stamp(request.requested_at),
            request.actor_reference,
            request.correlation_id,
            request.authentication_event_id,
            disposition.value,
            reason,
            _stamp(request.requested_at),
        )
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT operation_id, principal_id, content_reference, content_category, "
                "requested_at, actor_reference, correlation_id, authentication_event_id "
                "FROM deletion_records WHERE operation_id=?",
                (request.operation_id,),
            ).fetchone()
            if existing is not None:
                if existing != values[:8]:
                    raise ValueError("deletion operation identity conflict")
                return False
            connection.execute(
                "INSERT INTO deletion_records VALUES(?,?,?,?,?,?,?,?,?,?,?)", values
            )
            connection.execute(
                "UPDATE privacy_control_generation SET generation=generation+1 "
                "WHERE singleton=1"
            )
            self._insert_audit(
                connection,
                self._deletion_audit(
                    request, disposition, reason, request.requested_at
                ),
            )
        return True

    def update_deletion(
        self,
        request: DeletionRequest,
        disposition: DeletionDisposition,
        reason: str,
        updated_at: datetime,
    ) -> bool:
        identity = (
            request.operation_id,
            request.principal_id,
            request.content_reference,
            request.content_category.value,
            _stamp(request.requested_at),
            request.actor_reference,
            request.correlation_id,
            request.authentication_event_id,
        )
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT operation_id, principal_id, content_reference, content_category, "
                "requested_at, actor_reference, correlation_id, authentication_event_id, "
                "disposition "
                "FROM deletion_records WHERE operation_id=?",
                (request.operation_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(request.operation_id)
            if existing[:8] != identity:
                raise ValueError("deletion operation identity conflict")
            if existing[8] == DeletionDisposition.APPLIED.value:
                return False
            connection.execute(
                "UPDATE deletion_records SET disposition=?, reason=?, updated_at=? "
                "WHERE operation_id=?",
                (disposition.value, reason, _stamp(updated_at), request.operation_id),
            )
            self._insert_audit(
                connection,
                self._deletion_audit(request, disposition, reason, updated_at),
            )
        return True

    @staticmethod
    def _deletion_audit(
        request: DeletionRequest,
        disposition: DeletionDisposition,
        reason: str,
        occurred_at: datetime,
    ) -> AuditRecord:
        return AuditRecord(
            request.correlation_id,
            request.actor_reference,
            "deletion",
            request.content_category.value,
            occurred_at,
            f"{disposition.value}:{reason}",
            "content_free_tombstone"
            if disposition is DeletionDisposition.APPLIED
            else "payload_free",
        )

    def deletion(self, operation_id: str) -> DeletionRecord:
        with self._open() as connection:
            row = connection.execute(
                "SELECT operation_id, principal_id, content_reference, content_category, "
                "requested_at, actor_reference, correlation_id, authentication_event_id, "
                "disposition, reason, updated_at FROM deletion_records WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return self._deletion_record(row)

    def deletions_for_reconciliation(self) -> tuple[DeletionRecord, ...]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT operation_id, principal_id, content_reference, content_category, "
                "requested_at, actor_reference, correlation_id, authentication_event_id, "
                "disposition, reason, updated_at FROM deletion_records "
                "WHERE disposition IN ('pending','applied','incomplete_blocked') "
                "ORDER BY requested_at, operation_id"
            ).fetchall()
        return tuple(self._deletion_record(row) for row in rows)

    def control_generation(self) -> int:
        with self._open() as connection:
            row = connection.execute(
                "SELECT generation FROM privacy_control_generation WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise RuntimeError("privacy control generation is unavailable")
        return row[0]

    def reconciliation_snapshot(self) -> PrivacyReconciliationSnapshot:
        with self._open() as connection:
            connection.execute("BEGIN")
            generation = connection.execute(
                "SELECT generation FROM privacy_control_generation WHERE singleton=1"
            ).fetchone()
            deletion_rows = connection.execute(
                "SELECT operation_id, principal_id, content_reference, content_category, "
                "requested_at, actor_reference, correlation_id, authentication_event_id, "
                "disposition, reason, updated_at FROM deletion_records "
                "WHERE disposition IN ('pending','applied','incomplete_blocked') "
                "ORDER BY requested_at, operation_id"
            ).fetchall()
            revocation_rows = connection.execute(
                "SELECT consent_id, revoked_at FROM consent_revocations ORDER BY revoked_at"
            ).fetchall()
        if generation is None:
            raise RuntimeError("privacy control generation is unavailable")
        return PrivacyReconciliationSnapshot(
            generation[0],
            tuple(self._deletion_record(row) for row in deletion_rows),
            tuple((row[0], _time(row[1])) for row in revocation_rows),
        )

    @staticmethod
    def _deletion_record(row: tuple[object, ...]) -> DeletionRecord:
        from .models import ContentCategory

        request = DeletionRequest(
            row[0],
            row[1],
            row[2],
            ContentCategory(row[3]),
            _time(row[4]),
            row[5],
            row[6],
            row[7],
        )
        return DeletionRecord(
            request, DeletionDisposition(row[8]), row[9], _time(row[10])
        )

    def record_audit(self, record: AuditRecord) -> None:
        with self._open() as connection:
            self._insert_audit(connection, record)

    def _insert_audit(
        self, connection: sqlite3.Connection, record: AuditRecord
    ) -> None:
        connection.execute(
            "INSERT INTO privacy_audit(correlation_id, actor_reference, action_type, "
            "record_category, occurred_at, result_code, redaction_status) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                record.correlation_id,
                record.actor_reference,
                record.action_type,
                record.record_category,
                _stamp(record.occurred_at),
                record.result_code,
                record.redaction_status,
            ),
        )

    def audit_records(self) -> tuple[AuditRecord, ...]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT correlation_id, actor_reference, action_type, record_category, "
                "occurred_at, result_code, redaction_status FROM privacy_audit ORDER BY audit_key"
            ).fetchall()
        return tuple(
            AuditRecord(row[0], row[1], row[2], row[3], _time(row[4]), row[5], row[6])
            for row in rows
        )

    def acknowledge_notice(
        self,
        principal_id: str,
        notice_version: str,
        actor_reference: str,
        authentication_event_id: str,
        acknowledged_at: datetime,
        correlation_id: str,
    ) -> bool:
        values = (
            principal_id,
            notice_version,
            actor_reference,
            authentication_event_id,
            _stamp(acknowledged_at),
            correlation_id,
        )
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT principal_id, notice_version, actor_reference, authentication_event_id, "
                "acknowledged_at, correlation_id FROM notice_acknowledgements "
                "WHERE principal_id=? AND notice_version=?",
                (principal_id, notice_version),
            ).fetchone()
            if existing is not None:
                return False
            connection.execute(
                "INSERT INTO notice_acknowledgements VALUES(?,?,?,?,?,?)", values
            )
        return True

    def has_notice(self, principal_id: str, notice_version: str) -> bool:
        with self._open() as connection:
            row = connection.execute(
                "SELECT 1 FROM notice_acknowledgements WHERE principal_id=? AND notice_version=?",
                (principal_id, notice_version),
            ).fetchone()
        return row is not None

    def record_restore(
        self,
        restore_id: str,
        occurred_at: datetime,
        disposition: RestoreDisposition,
        reason: str,
    ) -> None:
        with self._open() as connection:
            connection.execute(
                "INSERT INTO restore_outcomes VALUES(?,?,?,?)",
                (restore_id, _stamp(occurred_at), disposition.value, reason),
            )
