"""WAL-consistent backup and fail-closed quarantined restore."""

from collections.abc import Callable
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Protocol
from uuid import uuid4

from .ledger import PrivacyLedger
from .models import (
    BackupMetadata,
    ContentCategory,
    DeletionRecord,
    ErasureReport,
    PrivacyReconciliationSnapshot,
    RestoreDisposition,
    RestoreResult,
    require_utc,
)


BACKUP_FORMAT_VERSION = 1
BACKUP_PAGE_BATCH = 256
MAX_BACKUP_BUSY_ATTEMPTS = 20
BACKUP_BUSY_SLEEP_SECONDS = 0.01


class BackupError(RuntimeError):
    """Sanitized backup/restore failure."""


class BackupBusyError(BackupError):
    """The database did not become available within the configured wait."""


class RestoreAdapter(Protocol):
    def apply_deletion(self, record: DeletionRecord) -> ErasureReport: ...

    def apply_revocation(self, consent_id: str, revoked_at: datetime) -> bool: ...

    def rebuild_dependencies(self) -> bool: ...


def _validate_existing_file(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise BackupError(f"{label} path must be absolute")
    if path.is_symlink():
        raise BackupError(f"{label} must not be a symbolic link")
    if not path.is_file() or not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
        raise BackupError(f"{label} must be a regular file")


def _validate_new_file(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise BackupError(f"{label} path must be absolute")
    if path.is_symlink():
        raise BackupError(f"{label} must not be a symbolic link")
    if path.exists():
        raise BackupError(f"{label} already exists")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise BackupError(f"{label} parent is unsafe")


def _schema_version(path: Path) -> int:
    try:
        with sqlite3.connect(path, timeout=0.1) as connection:
            row = connection.execute(
                "SELECT version FROM schema_metadata WHERE singleton=1"
            ).fetchone()
    except sqlite3.Error as error:
        raise BackupError("unable to validate database schema") from error
    if row is None or not isinstance(row[0], int):
        raise BackupError("unable to validate database schema")
    return row[0]


_OwnedFile = tuple[Path, int]


def _open_owned_file(path: Path, access: int, owned: list[_OwnedFile]) -> int:
    descriptor = os.open(path, access | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    owned.append((path, descriptor))
    os.fchmod(descriptor, 0o600)
    return descriptor


def _cleanup_created(owned: list[_OwnedFile]) -> None:
    for path, descriptor in owned:
        try:
            current = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            identity = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) == (
                identity.st_dev,
                identity.st_ino,
            ):
                path.unlink()
        os.close(descriptor)


def _close_owned(owned: list[_OwnedFile]) -> None:
    for _path, descriptor in owned:
        os.close(descriptor)


def _sqlite_copy(source: Path, destination: Path) -> None:
    with (
        closing(
            sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=0.1)
        ) as source_connection,
        closing(sqlite3.connect(destination, timeout=0.1)) as destination_connection,
    ):
        busy_attempts = 0

        def progress(status: int, _remaining: int, _total: int) -> None:
            nonlocal busy_attempts
            if status in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                busy_attempts += 1
                if busy_attempts >= MAX_BACKUP_BUSY_ATTEMPTS:
                    error = sqlite3.OperationalError(
                        "bounded SQLite backup wait expired"
                    )
                    error.sqlite_errorcode = status
                    raise error

        source_connection.backup(
            destination_connection,
            pages=BACKUP_PAGE_BATCH,
            progress=progress,
            sleep=BACKUP_BUSY_SLEEP_SECONDS,
        )
        destination_connection.commit()


def _is_busy(error: sqlite3.OperationalError) -> bool:
    return getattr(error, "sqlite_errorcode", None) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }


def _integrity_ok(path: Path) -> bool:
    with sqlite3.connect(path) as connection:
        return connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def _safe_integrity_ok(path: Path) -> bool:
    try:
        return _integrity_ok(path)
    except sqlite3.Error:
        return False


def create_sqlite_backup(
    source_path: str | Path,
    destination_path: str | Path,
    expected_schema_version: int,
    created_at: datetime,
    *,
    after_copy: Callable[[Path], None] | None = None,
) -> BackupMetadata:
    source = Path(source_path)
    destination = Path(destination_path)
    metadata_path = Path(f"{destination}.metadata.json")
    require_utc(created_at, "backup timestamp")
    _validate_existing_file(source, "source")
    _validate_new_file(destination, "destination")
    _validate_new_file(metadata_path, "metadata destination")
    if _schema_version(source) != expected_schema_version:
        raise BackupError("unsupported source schema")
    owned: list[_OwnedFile] = []
    try:
        _open_owned_file(destination, os.O_RDWR, owned)
        _sqlite_copy(source, destination)
        if after_copy is not None:
            after_copy(destination)
        if _schema_version(destination) != expected_schema_version:
            raise BackupError("backup schema verification failed")
        if not _integrity_ok(destination):
            raise BackupError("backup consistency verification failed")
        payload = json.dumps(
            {
                "format_version": BACKUP_FORMAT_VERSION,
                "schema_version": expected_schema_version,
                "created_at": created_at.isoformat(timespec="microseconds"),
                "database_filename": destination.name,
            },
            sort_keys=True,
        ).encode("utf-8")
        descriptor = _open_owned_file(metadata_path, os.O_WRONLY, owned)
        os.write(descriptor, payload)
        os.fsync(descriptor)
    except sqlite3.OperationalError as error:
        _cleanup_created(owned)
        if _is_busy(error):
            raise BackupBusyError("SQLite source is busy") from error
        raise BackupError("SQLite backup failed") from error
    except BackupError:
        _cleanup_created(owned)
        raise
    except Exception as error:
        _cleanup_created(owned)
        raise BackupError("SQLite backup failed") from error
    _close_owned(owned)
    return BackupMetadata(metadata_path, expected_schema_version, created_at)


def _load_metadata(backup: Path, expected_schema_version: int) -> BackupMetadata:
    metadata_path = Path(f"{backup}.metadata.json")
    _validate_existing_file(metadata_path, "backup metadata")
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        created_at = datetime.fromisoformat(raw["created_at"])
        valid = (
            raw["format_version"] == BACKUP_FORMAT_VERSION
            and raw["schema_version"] == expected_schema_version
            and raw["database_filename"] == backup.name
        )
        require_utc(created_at, "backup timestamp")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise BackupError("invalid backup metadata") from error
    if not valid:
        raise BackupError("invalid backup metadata")
    return BackupMetadata(metadata_path, expected_schema_version, created_at)


class QuarantinedRestore:
    def __init__(
        self,
        ledger: PrivacyLedger,
        expected_schema_version: int,
        adapter_factory: Callable[[Path], RestoreAdapter],
    ):
        self.ledger = ledger
        self.expected_schema_version = expected_schema_version
        self.adapter_factory = adapter_factory

    def restore(
        self,
        backup_path: str | Path,
        quarantine_path: str | Path,
        restored_at: datetime,
    ) -> RestoreResult:
        backup = Path(backup_path)
        quarantine = Path(quarantine_path)
        require_utc(restored_at, "restore timestamp")
        _validate_existing_file(backup, "backup")
        _validate_new_file(quarantine, "quarantine")
        ready_path = Path(f"{quarantine}.ready.json")
        _validate_new_file(ready_path, "readiness marker")
        _load_metadata(backup, self.expected_schema_version)
        if _schema_version(backup) != self.expected_schema_version:
            raise BackupError("unsupported backup schema")
        owned: list[_OwnedFile] = []
        try:
            _open_owned_file(quarantine, os.O_RDWR, owned)
            _sqlite_copy(backup, quarantine)
        except (OSError, sqlite3.Error) as error:
            _cleanup_created(owned)
            raise BackupError("quarantine copy failed") from error
        _close_owned(owned)
        restore_id = f"restore-{uuid4()}"
        if not _safe_integrity_ok(quarantine):
            result = RestoreResult(
                restore_id,
                RestoreDisposition.BLOCKED,
                False,
                "quarantine_integrity_failed",
            )
            self.ledger.record_restore(
                restore_id, restored_at, result.disposition, result.reason
            )
            return result
        snapshot = self.ledger.reconciliation_snapshot()
        try:
            adapter = self.adapter_factory(quarantine)
        except Exception:
            result = RestoreResult(
                restore_id,
                RestoreDisposition.BLOCKED,
                False,
                "adapter_initialization_failure",
            )
            self.ledger.record_restore(
                restore_id, restored_at, result.disposition, result.reason
            )
            return result
        reason = self._reconcile(adapter, snapshot)
        if reason is not None:
            result = RestoreResult(
                restore_id, RestoreDisposition.BLOCKED, False, reason
            )
            self.ledger.record_restore(
                restore_id, restored_at, result.disposition, reason
            )
            return result
        try:
            schema_matches = _schema_version(quarantine) == self.expected_schema_version
        except BackupError:
            schema_matches = False
        if not schema_matches:
            result = RestoreResult(
                restore_id,
                RestoreDisposition.BLOCKED,
                False,
                "post_reconciliation_schema_invalid",
            )
            self.ledger.record_restore(
                restore_id, restored_at, result.disposition, result.reason
            )
            return result
        if not _safe_integrity_ok(quarantine):
            result = RestoreResult(
                restore_id,
                RestoreDisposition.BLOCKED,
                False,
                "post_reconciliation_integrity_failed",
            )
            self.ledger.record_restore(
                restore_id, restored_at, result.disposition, result.reason
            )
            return result
        if self.ledger.control_generation() != snapshot.generation:
            result = RestoreResult(
                restore_id,
                RestoreDisposition.BLOCKED,
                False,
                "privacy_ledger_changed_during_restore",
            )
            self.ledger.record_restore(
                restore_id, restored_at, result.disposition, result.reason
            )
            return result
        self.ledger.record_restore(
            restore_id,
            restored_at,
            RestoreDisposition.ELIGIBLE_FOR_ACTIVATION,
            "reconciliation_complete",
        )
        marker = json.dumps(
            {
                "restore_id": restore_id,
                "disposition": "eligible_for_activation",
                "ledger_generation": snapshot.generation,
            },
            sort_keys=True,
        ).encode("utf-8")
        descriptor = os.open(
            ready_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        try:
            os.write(descriptor, marker)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return RestoreResult(
            restore_id,
            RestoreDisposition.ELIGIBLE_FOR_ACTIVATION,
            True,
            "reconciliation_complete",
            snapshot.generation,
        )

    def is_activation_current(self, result: RestoreResult) -> bool:
        return (
            result.activation_eligible is True
            and isinstance(result.ledger_generation, int)
            and self.ledger.control_generation() == result.ledger_generation
        )

    def _reconcile(
        self, adapter: RestoreAdapter, snapshot: PrivacyReconciliationSnapshot
    ) -> str | None:
        required = frozenset(ContentCategory.required_erasure_categories())
        for record in snapshot.deletions:
            try:
                report = adapter.apply_deletion(record)
            except Exception:
                return "deletion_adapter_failure"
            if not isinstance(report, ErasureReport):
                return "invalid_erasure_report"
            if (
                not report.payload_removed
                or not report.tombstone_written
                or report.blockers
                or not required <= report.invalidated_categories
            ):
                return "deletion_reconciliation_incomplete"
        for consent_id, revoked_at in snapshot.revocations:
            try:
                if adapter.apply_revocation(consent_id, revoked_at) is not True:
                    return "revocation_reconciliation_incomplete"
            except Exception:
                return "revocation_adapter_failure"
        try:
            if adapter.rebuild_dependencies() is not True:
                return "dependency_rebuild_incomplete"
        except Exception:
            return "dependency_rebuild_failure"
        return None
