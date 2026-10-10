"""Minimal, payload-free SQLite privacy ledger."""

from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
import fcntl
import os
from pathlib import Path
import sqlite3
from uuid import UUID
from weakref import finalize

from .files import open_trusted_directory_chain, regular_file_is_trusted
from .models import (
    AuditRecord,
    ConsentGrant,
    ConsentPurpose,
    ConsentUseResult,
    ContentCategory,
    DeletionDisposition,
    DeletionRecord,
    DeletionRequest,
    ErasureReport,
    OperationOrigin,
    PrivacyReconciliationSnapshot,
    RestoreDisposition,
    TrustedPrivacyContext,
    require_token,
    require_utc,
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
        authentication_event_id TEXT NOT NULL,
        UNIQUE(principal_id, content_reference, audience, purpose, revision)
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
    """CREATE TABLE consent_use_attempts(
        correlation_id TEXT PRIMARY KEY,
        principal_id TEXT NOT NULL,
        content_reference TEXT NOT NULL,
        audience TEXT NOT NULL,
        purpose TEXT NOT NULL,
        actor_reference TEXT NOT NULL,
        authentication_event_id TEXT NOT NULL,
        attempted_at TEXT NOT NULL,
        result_code TEXT NOT NULL,
        consent_id TEXT
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

_EXPECTED_SCHEMA = tuple(
    sorted(
        (
            "table",
            statement.split("(", 1)[0].split()[-1],
            statement.split("(", 1)[0].split()[-1],
            " ".join(statement.split()),
        )
        for statement in _SCHEMA
    )
)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class PrivacyLedger:
    """An independently committed control store; it contains no private payload."""

    def __init__(self, path: str | Path):
        requested_path = Path(path)
        if requested_path == Path(":memory:"):
            raise ValueError("in-memory privacy ledgers are not supported")
        self.path = requested_path.absolute()
        if self.path.is_symlink():
            raise ValueError("privacy ledger must not be a symbolic link")
        try:
            parent_descriptor = open_trusted_directory_chain(self.path.parent)
        except OSError as error:
            raise ValueError("privacy ledger parent is unsafe") from error
        try:
            descriptor = os.open(
                self.path.name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_descriptor,
            )
        except BaseException:
            os.close(parent_descriptor)
            raise
        self._parent_descriptor = parent_descriptor
        try:
            if not regular_file_is_trusted(os.fstat(descriptor)):
                raise ValueError("privacy ledger file is unsafe")
            self._require_trusted_sidecars()
            with closing(self._connect_descriptor(descriptor)) as connection:
                self._require_file_identity(connection, descriptor)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    self._has_valid_schema(connection)
                    os.fchmod(descriptor, 0o600)
                    self._configure(connection)
                    initialized_schema = False
                    with connection:
                        connection.execute("BEGIN IMMEDIATE")
                        if not self._has_valid_schema(connection):
                            for statement in _SCHEMA:
                                connection.execute(statement)
                            connection.execute(
                                "INSERT INTO privacy_schema_metadata VALUES(1, ?)",
                                (PRIVACY_LEDGER_SCHEMA_VERSION,),
                            )
                            connection.execute(
                                "INSERT INTO privacy_control_generation VALUES(1, 0)"
                            )
                            initialized_schema = True
                    if initialized_schema:
                        os.fsync(parent_descriptor)
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        except BaseException:
            os.close(descriptor)
            os.close(parent_descriptor)
            raise
        self._descriptor = descriptor
        self._finalizer = finalize(self, os.close, descriptor)
        self._parent_finalizer = finalize(self, os.close, parent_descriptor)

    @staticmethod
    def _has_valid_schema(connection: sqlite3.Connection) -> bool:
        try:
            schema = tuple(
                (
                    object_type,
                    name,
                    table_name,
                    " ".join(sql.split()),
                )
                for object_type, name, table_name, sql in connection.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                )
            )
            has_metadata = any(
                object_type == "table" and name == "privacy_schema_metadata"
                for object_type, name, _table_name, _sql in schema
            )
            if not has_metadata:
                if schema:
                    raise RuntimeError("privacy ledger path contains unrelated tables")
                return False
            if schema != _EXPECTED_SCHEMA:
                raise RuntimeError("invalid privacy ledger schema")
            version = connection.execute(
                "SELECT version FROM privacy_schema_metadata WHERE singleton=1"
            ).fetchone()
            if version != (PRIVACY_LEDGER_SCHEMA_VERSION,):
                raise RuntimeError("unsupported privacy ledger schema")
        except sqlite3.Error as error:
            raise RuntimeError("unable to validate existing privacy ledger") from error
        return True

    def _open(self) -> sqlite3.Connection:
        self._require_path_identity(self._descriptor)
        self._require_trusted_sidecars()
        connection = self._connect_descriptor(self._descriptor)
        try:
            self._require_file_identity(connection, self._descriptor)
            self._configure(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _connect_descriptor(descriptor: int) -> sqlite3.Connection:
        return sqlite3.connect(
            f"file:/proc/self/fd/{descriptor}?mode=rw",
            uri=True,
            timeout=5.0,
        )

    def _require_path_identity(self, descriptor: int) -> None:
        try:
            current = os.stat(
                self.path.name,
                dir_fd=self._parent_descriptor,
                follow_symlinks=False,
            )
            public = self.path.stat(follow_symlinks=False)
        except OSError as error:
            raise RuntimeError("privacy ledger path identity changed") from error
        identity = os.fstat(descriptor)
        expected = (identity.st_dev, identity.st_ino)
        if (current.st_dev, current.st_ino) != expected or (
            public.st_dev,
            public.st_ino,
        ) != expected:
            raise RuntimeError("privacy ledger path identity changed")

    def _require_file_identity(
        self, connection: sqlite3.Connection, descriptor: int
    ) -> None:
        self._require_path_identity(descriptor)
        database_path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        try:
            database_identity = database_path.stat(follow_symlinks=False)
        except OSError as error:
            raise RuntimeError("privacy ledger connection identity changed") from error
        identity = os.fstat(descriptor)
        if (database_identity.st_dev, database_identity.st_ino) != (
            identity.st_dev,
            identity.st_ino,
        ):
            raise RuntimeError("privacy ledger connection identity changed")

    def _configure(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys=ON")
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if journal_mode != ("wal",):
            raise RuntimeError("privacy ledger requires WAL journal mode")
        for suffix in ("-wal", "-shm"):
            try:
                sidecar_descriptor = os.open(
                    f"{self.path.name}{suffix}",
                    os.O_RDWR | os.O_NOFOLLOW,
                    dir_fd=self._parent_descriptor,
                )
            except FileNotFoundError:
                continue
            try:
                if not regular_file_is_trusted(os.fstat(sidecar_descriptor)):
                    raise RuntimeError("privacy ledger sidecar is unsafe")
                os.fchmod(sidecar_descriptor, 0o600)
            finally:
                os.close(sidecar_descriptor)

    def _require_trusted_sidecars(self) -> None:
        for suffix in ("-wal", "-shm"):
            try:
                descriptor = os.open(
                    f"{self.path.name}{suffix}",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=self._parent_descriptor,
                )
            except FileNotFoundError:
                continue
            except OSError as error:
                raise RuntimeError("privacy ledger sidecar is unsafe") from error
            try:
                if not regular_file_is_trusted(os.fstat(descriptor)):
                    raise RuntimeError("privacy ledger sidecar is unsafe")
            finally:
                os.close(descriptor)

    @staticmethod
    def _require_control_context(
        context: TrustedPrivacyContext,
        *,
        principal_id: str,
        actor_reference: str,
        authentication_event_id: str,
    ) -> None:
        if (
            not isinstance(context, TrustedPrivacyContext)
            or context.origin is not OperationOrigin.CONTROL
        ):
            raise ValueError("invalid privacy control authentication")
        if context.principal_id != principal_id:
            raise ValueError("invalid privacy control authorization")
        if (
            context.actor_reference != actor_reference
            or context.authentication_event_id != authentication_event_id
        ):
            raise ValueError("invalid privacy control authentication")

    def insert_consent(
        self, context: TrustedPrivacyContext, grant: ConsentGrant
    ) -> bool:
        self._require_control_context(
            context,
            principal_id=grant.principal_id,
            actor_reference=grant.actor_reference,
            authentication_event_id=grant.authentication_event_id,
        )
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
                revision_conflict = connection.execute(
                    "SELECT consent_id FROM consent_grants WHERE principal_id=? "
                    "AND content_reference=? AND audience=? AND purpose=? "
                    "AND revision=?",
                    (
                        grant.principal_id,
                        grant.content_reference,
                        grant.audience,
                        grant.purpose.value,
                        grant.revision,
                    ),
                ).fetchone()
                if revision_conflict is not None:
                    raise ValueError("consent revision conflict")
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
        context: TrustedPrivacyContext,
        principal_id: str,
        content_reference: str,
        audience: str,
        purpose: ConsentPurpose,
        correlation_id: str,
        actor_reference: str,
        authentication_event_id: str,
        clock: Callable[[], datetime],
        dispatch: Callable[[], None],
    ) -> ConsentUseResult:
        require_token(principal_id, "principal id")
        require_token(content_reference, "content reference")
        require_token(audience, "audience")
        if not isinstance(purpose, ConsentPurpose):
            raise ValueError("invalid consent purpose")
        require_token(correlation_id, "correlation id")
        require_token(actor_reference, "actor reference")
        require_token(authentication_event_id, "authentication event id")
        self._require_control_context(
            context,
            principal_id=principal_id,
            actor_reference=actor_reference,
            authentication_event_id=authentication_event_id,
        )
        identity = (
            principal_id,
            content_reference,
            audience,
            purpose.value,
            actor_reference,
            authentication_event_id,
        )
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT principal_id, content_reference, audience, purpose, "
                "actor_reference, authentication_event_id, result_code, consent_id "
                "FROM consent_use_attempts WHERE correlation_id=?",
                (correlation_id,),
            ).fetchone()
            if existing is not None:
                if existing[:6] != identity:
                    raise ValueError("consent use operation identity conflict")
                if existing[6] == "completed":
                    repeated = ConsentUseResult(
                        False, "already_dispatched", existing[7]
                    )
                elif existing[6] in {"dispatch_pending", "dispatch_uncertain"}:
                    repeated = ConsentUseResult(
                        False, "dispatch_uncertain", existing[7]
                    )
                elif existing[6] != "pre_dispatch_failure":
                    repeated = ConsentUseResult(False, existing[6], existing[7])
                else:
                    repeated = None
                if repeated is not None:
                    repeated_at = clock()
                    require_utc(repeated_at, "consent use timestamp")
                    self._insert_audit(
                        connection,
                        AuditRecord(
                            correlation_id,
                            actor_reference,
                            "consent_use",
                            "consent",
                            repeated_at,
                            repeated.reason,
                            "payload_free",
                        ),
                    )
                    return repeated
            attempted_at = clock()
            require_utc(attempted_at, "consent use attempt timestamp")
            if existing is None:
                connection.execute(
                    "INSERT INTO consent_use_attempts VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        correlation_id,
                        *identity,
                        _stamp(attempted_at),
                        "dispatch_pending",
                    ),
                )
            else:
                connection.execute(
                    "UPDATE consent_use_attempts SET attempted_at=?, "
                    "result_code='dispatch_pending', consent_id=NULL "
                    "WHERE correlation_id=?",
                    (_stamp(attempted_at), correlation_id),
                )
            self._insert_audit(
                connection,
                AuditRecord(
                    correlation_id,
                    actor_reference,
                    "consent_use_attempt",
                    "consent",
                    attempted_at,
                    "dispatch_pending",
                    "payload_free",
                ),
            )

        dispatch_started = False
        try:
            with self._open() as connection:
                connection.execute("BEGIN IMMEDIATE")
                used_at = clock()
                require_utc(used_at, "consent use timestamp")
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
                if result.allowed:
                    dispatch_started = True
                    dispatch()
                connection.execute(
                    "UPDATE consent_use_attempts SET result_code=?, consent_id=? "
                    "WHERE correlation_id=?",
                    (
                        "completed" if result.allowed else result.reason,
                        result.consent_id,
                        correlation_id,
                    ),
                )
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
            return result
        except Exception:
            failure_reason = (
                "dispatch_uncertain" if dispatch_started else "pre_dispatch_failure"
            )
            with self._open() as connection:
                connection.execute("BEGIN IMMEDIATE")
                failed_at = clock()
                require_utc(failed_at, "consent use failure timestamp")
                connection.execute(
                    "UPDATE consent_use_attempts SET result_code=? WHERE correlation_id=?",
                    (failure_reason, correlation_id),
                )
                self._insert_audit(
                    connection,
                    AuditRecord(
                        correlation_id,
                        actor_reference,
                        "consent_use",
                        "consent",
                        failed_at,
                        failure_reason,
                        "payload_free",
                    ),
                )
            raise

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
        context: TrustedPrivacyContext,
        consent_id: str,
        operation_id: str,
        principal_id: str,
        actor_reference: str,
        authentication_event_id: str,
        revoked_at: datetime,
        correlation_id: str,
    ) -> bool:
        require_token(consent_id, "consent id")
        require_token(operation_id, "operation id")
        require_token(principal_id, "principal id")
        require_token(actor_reference, "actor reference")
        require_token(authentication_event_id, "authentication event id")
        require_utc(revoked_at, "revocation timestamp")
        require_token(correlation_id, "correlation id")
        self._require_control_context(
            context,
            principal_id=principal_id,
            actor_reference=actor_reference,
            authentication_event_id=authentication_event_id,
        )
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
            owner = connection.execute(
                "SELECT 1 FROM consent_grants WHERE consent_id=? AND principal_id=?",
                (consent_id, principal_id),
            ).fetchone()
            if owner is None:
                raise ValueError("revocation consent ownership mismatch")
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
                self._increment_control_generation(connection)
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
        self,
        context: TrustedPrivacyContext,
        request: DeletionRequest,
        disposition: DeletionDisposition,
        reason: str,
    ) -> bool:
        self._require_control_context(
            context,
            principal_id=request.principal_id,
            actor_reference=request.actor_reference,
            authentication_event_id=request.authentication_event_id,
        )
        if not isinstance(disposition, DeletionDisposition):
            raise ValueError("invalid deletion disposition")
        if disposition is DeletionDisposition.APPLIED:
            raise ValueError("applied deletion requires verified erasure")
        require_token(reason, "deletion reason")
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
            self._increment_control_generation(connection)
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
        if disposition is DeletionDisposition.APPLIED:
            raise ValueError("applied deletion requires verified erasure")
        return self._update_deletion(request, disposition, reason, updated_at)

    def _record_verified_deletion(
        self,
        request: DeletionRequest,
        report: ErasureReport,
        updated_at: datetime,
    ) -> bool:
        required = frozenset(ContentCategory.required_erasure_categories())
        if (
            not isinstance(report, ErasureReport)
            or not report.payload_removed
            or not report.tombstone_written
            or report.blockers
            or not required <= report.invalidated_categories
        ):
            raise ValueError("applied deletion requires verified erasure")
        return self._update_deletion(
            request,
            DeletionDisposition.APPLIED,
            "erasure_applied",
            updated_at,
        )

    def _update_deletion(
        self,
        request: DeletionRequest,
        disposition: DeletionDisposition,
        reason: str,
        updated_at: datetime,
    ) -> bool:
        if not isinstance(disposition, DeletionDisposition):
            raise ValueError("invalid deletion disposition")
        require_token(reason, "deletion reason")
        require_utc(updated_at, "deletion transition timestamp")
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

    @staticmethod
    def _increment_control_generation(connection: sqlite3.Connection) -> None:
        updated = connection.execute(
            "UPDATE privacy_control_generation SET generation=generation+1 "
            "WHERE singleton=1"
        )
        if updated.rowcount != 1:
            raise RuntimeError("privacy control generation is unavailable")

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
        context: TrustedPrivacyContext,
        principal_id: str,
        notice_version: str,
        actor_reference: str,
        authentication_event_id: str,
        acknowledged_at: datetime,
        correlation_id: str,
    ) -> bool:
        require_token(principal_id, "principal id")
        require_token(notice_version, "notice version")
        require_token(actor_reference, "actor reference")
        require_token(authentication_event_id, "authentication event id")
        require_utc(acknowledged_at, "acknowledgement timestamp")
        require_token(correlation_id, "correlation id")
        self._require_control_context(
            context,
            principal_id=principal_id,
            actor_reference=actor_reference,
            authentication_event_id=authentication_event_id,
        )
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
        *,
        expected_generation: int | None = None,
    ) -> bool:
        require_token(restore_id, "restore id")
        try:
            parsed_id = UUID(restore_id.removeprefix("restore-"))
        except ValueError as error:
            raise ValueError("invalid restore id") from error
        if restore_id != f"restore-{parsed_id}":
            raise ValueError("invalid restore id")
        require_utc(occurred_at, "restore timestamp")
        allowed_reasons = {
            RestoreDisposition.ELIGIBLE_FOR_ACTIVATION: {"reconciliation_complete"},
            RestoreDisposition.BLOCKED: {
                "adapter_initialization_failure",
                "adapter_writer_release_failure",
                "adapter_writer_release_incomplete",
                "deletion_adapter_failure",
                "deletion_reconciliation_incomplete",
                "dependency_rebuild_failure",
                "dependency_rebuild_incomplete",
                "invalid_erasure_report",
                "post_reconciliation_integrity_failed",
                "post_reconciliation_schema_invalid",
                "privacy_ledger_changed_during_restore",
                "quarantine_finalization_failed",
                "quarantine_identity_changed",
                "quarantine_integrity_failed",
                "revocation_adapter_failure",
                "revocation_reconciliation_incomplete",
            },
        }
        if not isinstance(disposition, RestoreDisposition):
            raise ValueError("invalid restore disposition")
        if reason not in allowed_reasons[disposition]:
            raise ValueError("invalid restore reason")
        if disposition is RestoreDisposition.ELIGIBLE_FOR_ACTIVATION:
            if type(expected_generation) is not int or expected_generation < 0:
                raise ValueError("invalid restore generation")
        elif expected_generation is not None:
            raise ValueError("invalid restore generation")
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if expected_generation is not None:
                generation = connection.execute(
                    "SELECT generation FROM privacy_control_generation WHERE singleton=1"
                ).fetchone()
                if generation is None:
                    raise RuntimeError("privacy control generation is unavailable")
                if generation != (expected_generation,):
                    return False
            connection.execute(
                "INSERT INTO restore_outcomes VALUES(?,?,?,?)",
                (restore_id, _stamp(occurred_at), disposition.value, reason),
            )
        return True
