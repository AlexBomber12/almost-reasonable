"""WAL-consistent backup and fail-closed quarantined restore."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Protocol
from uuid import uuid4

from app.core.store import _SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3

from . import files as _files
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
MAX_BACKUP_METADATA_BYTES = 4096
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

_directory_is_trusted = _files.directory_is_trusted
_regular_file_is_trusted = _files.regular_file_is_trusted
_open_directory_chain = _files.open_trusted_directory_chain


def _schema_objects(connection: sqlite3.Connection) -> dict[tuple[str, str, str], str]:
    return {
        (object_type, name, table_name): " ".join(sql.split())
        for object_type, name, table_name, sql in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL"
        )
    }


def _expected_application_schemas() -> dict[int, dict[tuple[str, str, str], str]]:
    statements_by_version = {
        1: _SCHEMA_V1,
        2: (*_SCHEMA_V1, *_SCHEMA_V2),
        3: (*_SCHEMA_V1, *_SCHEMA_V2, *_SCHEMA_V3),
    }
    schemas = {}
    for version, statements in statements_by_version.items():
        with sqlite3.connect(":memory:") as connection:
            for statement in statements:
                connection.execute(statement)
            schemas[version] = _schema_objects(connection)
    return schemas


_EXPECTED_APPLICATION_SCHEMAS = _expected_application_schemas()
_KNOWN_APPLICATION_SCHEMA_KEYS = frozenset(
    key for schema in _EXPECTED_APPLICATION_SCHEMAS.values() for key in schema
)


class BackupError(RuntimeError):
    """Sanitized backup/restore failure."""


class BackupBusyError(BackupError):
    """The database did not become available within the configured wait."""


class _QuarantineIdentityError(RuntimeError):
    """The public quarantine path no longer names the owned database."""


class RestoreAdapter(Protocol):
    def apply_deletion(self, record: DeletionRecord) -> ErasureReport: ...

    def apply_revocation(self, consent_id: str, revoked_at: datetime) -> bool: ...

    def rebuild_dependencies(self) -> bool: ...

    def release_writers(self) -> bool: ...


def _validate_parent_chain(path: Path, label: str) -> None:
    try:
        descriptor = _open_directory_chain(path.parent)
    except OSError as error:
        raise BackupError(f"{label} parent is unsafe") from error
    os.close(descriptor)


def _validate_existing_file(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise BackupError(f"{label} path must be absolute")
    _validate_parent_chain(path, label)
    if path.is_symlink():
        raise BackupError(f"{label} must not be a symbolic link")
    try:
        identity = path.stat(follow_symlinks=False)
    except OSError:
        raise BackupError(f"{label} must be a regular file") from None
    if not stat.S_ISREG(identity.st_mode):
        raise BackupError(f"{label} must be a regular file")
    if not _regular_file_is_trusted(identity):
        raise BackupError(f"{label} file is unsafe")


def _validate_new_file(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise BackupError(f"{label} path must be absolute")
    _validate_parent_chain(path, label)
    if path.is_symlink():
        raise BackupError(f"{label} must not be a symbolic link")
    if path.exists():
        raise BackupError(f"{label} already exists")


def _database_target(
    path_or_descriptor: Path | int, *, immutable: bool = False
) -> tuple[str | Path, bool]:
    if type(path_or_descriptor) is int:
        immutable_parameter = "&immutable=1" if immutable else ""
        return (
            f"file:/proc/self/fd/{path_or_descriptor}?mode=ro{immutable_parameter}",
            True,
        )
    return path_or_descriptor, False


def _schema_version(path_or_descriptor: Path | int, *, immutable: bool = False) -> int:
    target, uri = _database_target(path_or_descriptor, immutable=immutable)
    try:
        with sqlite3.connect(target, timeout=0.1, uri=uri) as connection:
            return _connection_schema_version(connection)
    except sqlite3.Error as error:
        raise BackupError("unable to validate database schema") from error


def _connection_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT version FROM schema_metadata WHERE singleton=1"
    ).fetchone()
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if row is None or type(row[0]) is not int or user_version != row[0]:
        raise BackupError("unable to validate database schema")
    expected_schema = _EXPECTED_APPLICATION_SCHEMAS.get(row[0])
    actual_schema = _schema_objects(connection)
    if (
        expected_schema is None
        or {
            key: sql
            for key, sql in actual_schema.items()
            if key in _KNOWN_APPLICATION_SCHEMA_KEYS
        }
        != expected_schema
    ):
        raise BackupError("unable to validate database schema")
    return row[0]


@dataclass(slots=True)
class _OwnedFile:
    path: Path
    descriptor: int
    parent_descriptor: int
    cleanup_alias: Path | None = None


@dataclass(frozen=True, slots=True)
class _PinnedDatabase:
    path: Path
    descriptor: int
    immutable: bool = False
    sidecars: tuple[tuple[Path, int], ...] = ()
    expected_schema_version: int | None = None


def _open_owned_file(path: Path, access: int, owned: list[_OwnedFile]) -> int:
    parent_descriptor = _open_directory_chain(path.parent)
    try:
        descriptor = os.open(
            path.name,
            access | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_descriptor,
        )
    except BaseException:
        os.close(parent_descriptor)
        raise
    owned.append(_OwnedFile(path, descriptor, parent_descriptor))
    os.fchmod(descriptor, 0o600)
    return descriptor


def _cleanup_created(owned: list[_OwnedFile], *, count: int | None = None) -> None:
    remaining = len(owned) if count is None else count
    while owned and remaining:
        output = owned[0]
        paths = (output.path, output.cleanup_alias)
        for path in paths:
            if path is None:
                continue
            try:
                current = os.stat(
                    path.name,
                    dir_fd=output.parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                identity = os.fstat(output.descriptor)
                if (current.st_dev, current.st_ino) == (
                    identity.st_dev,
                    identity.st_ino,
                ):
                    os.unlink(path.name, dir_fd=output.parent_descriptor)
        owned.pop(0)
        try:
            os.close(output.descriptor)
        finally:
            os.close(output.parent_descriptor)
        remaining -= 1


def _cleanup_created_resumable(owned: list[_OwnedFile]) -> None:
    try:
        _cleanup_created(owned)
    finally:
        _cleanup_created(owned)


def _cleanup_created_database_sidecars(
    owned: list[_OwnedFile],
    *,
    count: int | None = None,
    preserved_sidecars: set[Path],
) -> None:
    outputs = owned if count is None else owned[:count]
    for output in outputs:
        if not _path_matches_descriptor(output.path, output.descriptor):
            try:
                os.stat(
                    output.path.name,
                    dir_fd=output.parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                _cleanup_missing_database_sidecars(output)
            continue
        pinned = _pin_database(output.path, "staging database", pin_sidecars=True)
        try:
            owned_identity = os.fstat(output.descriptor)
            pinned_identity = os.fstat(pinned.descriptor)
            if (pinned_identity.st_dev, pinned_identity.st_ino) != (
                owned_identity.st_dev,
                owned_identity.st_ino,
            ):
                continue
            for sidecar_path, descriptor in pinned.sidecars:
                if _path_matches_descriptor(sidecar_path, descriptor):
                    os.unlink(sidecar_path.name, dir_fd=output.parent_descriptor)
                else:
                    preserved_sidecars.add(sidecar_path)
        finally:
            _close_pinned_database(pinned)


def _cleanup_missing_database_sidecars(
    output: _OwnedFile, preserved_sidecars: set[Path] | None = None
) -> None:
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        sidecar_path = Path(f"{output.path}{suffix}")
        if preserved_sidecars is not None and sidecar_path in preserved_sidecars:
            continue
        try:
            descriptor = os.open(
                sidecar_path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=output.parent_descriptor,
            )
        except FileNotFoundError:
            continue
        try:
            try:
                os.stat(
                    output.path.name,
                    dir_fd=output.parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                main_path_absent = True
            else:
                main_path_absent = False
            if (
                main_path_absent
                and _regular_file_is_trusted(os.fstat(descriptor))
                and _path_matches_descriptor(sidecar_path, descriptor)
            ):
                os.unlink(sidecar_path.name, dir_fd=output.parent_descriptor)
        finally:
            os.close(descriptor)


def _cleanup_created_database(
    owned: list[_OwnedFile], *, count: int | None = None
) -> None:
    outputs = owned if count is None else owned[:count]
    post_cleanup_outputs = [
        _OwnedFile(
            output.path,
            output.descriptor,
            os.dup(output.parent_descriptor),
            output.cleanup_alias,
        )
        for output in outputs
    ]
    preserved_sidecars: set[Path] = set()
    try:
        try:
            _cleanup_created_database_sidecars(
                owned,
                count=count,
                preserved_sidecars=preserved_sidecars,
            )
        except BaseException:
            _cleanup_created_database_sidecars(
                owned,
                count=count,
                preserved_sidecars=preserved_sidecars,
            )
            _cleanup_created(owned, count=count)
            raise
        _cleanup_created(owned, count=count)
    finally:
        for output in post_cleanup_outputs:
            try:
                try:
                    _cleanup_missing_database_sidecars(output, preserved_sidecars)
                except BaseException:
                    _cleanup_missing_database_sidecars(output, preserved_sidecars)
                    raise
            finally:
                os.close(output.parent_descriptor)


def _close_owned(owned: list[_OwnedFile]) -> None:
    for output in owned:
        os.close(output.descriptor)
        os.close(output.parent_descriptor)


def _fsync_owned_parents(owned: list[_OwnedFile]) -> None:
    for output in owned:
        os.fsync(output.parent_descriptor)


def _publish_owned_file(source: Path, destination: Path, output: _OwnedFile) -> None:
    if not _path_matches_descriptor(source, output.descriptor):
        raise BackupError("quarantine staging identity changed")
    output.cleanup_alias = destination
    try:
        os.link(
            source.name,
            destination.name,
            src_dir_fd=output.parent_descriptor,
            dst_dir_fd=output.parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise BackupError("quarantine publication failed") from error
    if not _path_matches_descriptor(destination, output.descriptor):
        raise BackupError("quarantine publication identity changed")
    output.path = destination
    output.cleanup_alias = source
    os.unlink(source.name, dir_fd=output.parent_descriptor)
    output.cleanup_alias = None
    os.fsync(output.parent_descriptor)


def _path_matches_descriptor(path: Path, descriptor: int) -> bool:
    try:
        current = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    identity = os.fstat(descriptor)
    return (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)


def _pin_database(
    path: Path,
    label: str,
    *,
    immutable: bool = False,
    pin_sidecars: bool = False,
    expected_schema_version: int | None = None,
) -> _PinnedDatabase:
    try:
        parent_descriptor = _open_directory_chain(path.parent)
    except OSError as error:
        raise BackupError(f"{label} identity changed") from error
    descriptor: int | None = None
    sidecars: list[tuple[Path, int]] = []
    opened_descriptors: list[int] = []
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_descriptor,
        )
        opened_descriptors.append(descriptor)
        if pin_sidecars:
            for suffix in _SQLITE_SIDECAR_SUFFIXES:
                sidecar_path = Path(f"{path}{suffix}")
                try:
                    sidecar_descriptor = os.open(
                        sidecar_path.name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=parent_descriptor,
                    )
                except FileNotFoundError:
                    continue
                sidecars.append((sidecar_path, sidecar_descriptor))
                opened_descriptors.append(sidecar_descriptor)
        trusted = _regular_file_is_trusted(
            os.fstat(descriptor)
        ) and _path_matches_descriptor(path, descriptor)
        trusted = trusted and all(
            _regular_file_is_trusted(os.fstat(sidecar_descriptor))
            and _path_matches_descriptor(sidecar_path, sidecar_descriptor)
            for sidecar_path, sidecar_descriptor in sidecars
        )
        if not trusted:
            raise BackupError(f"{label} identity changed")
    except OSError as error:
        for opened_descriptor in opened_descriptors:
            os.close(opened_descriptor)
        raise BackupError(f"{label} identity changed") from error
    except BaseException:
        for opened_descriptor in opened_descriptors:
            os.close(opened_descriptor)
        raise
    finally:
        os.close(parent_descriptor)
    return _PinnedDatabase(
        path, descriptor, immutable, tuple(sidecars), expected_schema_version
    )


def _close_pinned_database(database: _PinnedDatabase) -> None:
    os.close(database.descriptor)
    for _sidecar_path, descriptor in database.sidecars:
        os.close(descriptor)


def _require_database_identity(source: _PinnedDatabase) -> None:
    if not _path_matches_descriptor(source.path, source.descriptor) or not all(
        _path_matches_descriptor(path, descriptor)
        for path, descriptor in source.sidecars
    ):
        raise BackupError("backup source identity changed")
    current = _pin_database(source.path, "backup source", pin_sidecars=True)
    try:
        if source.immutable and current.sidecars:
            raise BackupError("immutable backup contains SQLite sidecars")
        expected = os.fstat(source.descriptor)
        observed = os.fstat(current.descriptor)
        if (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino):
            raise BackupError("backup source identity changed")
    finally:
        _close_pinned_database(current)


def _require_connection_identity(
    connection: sqlite3.Connection, source: _PinnedDatabase
) -> None:
    database_path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    if not _path_matches_descriptor(database_path, source.descriptor):
        raise BackupError("backup source connection identity changed")


def _use_rollback_journal(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
        raise sqlite3.OperationalError("unable to finalize backup journal")


def _descriptor_sha256(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(descriptor, 1024 * 1024, offset):
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _sqlite_sidecars_absent(path: Path, parent_descriptor: int) -> bool:
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        try:
            os.stat(
                f"{path.name}{suffix}",
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        return False
    return True


def _sqlite_copy(source: _PinnedDatabase, destination_descriptor: int) -> None:
    _require_database_identity(source)
    source_target, source_uri = _database_target(
        source.descriptor, immutable=source.immutable
    )
    source_connection = sqlite3.connect(source_target, uri=source_uri, timeout=0.1)
    with closing(source_connection):
        _require_database_identity(source)
        _require_connection_identity(source_connection, source)
        destination_connection = sqlite3.connect(
            f"file:/proc/self/fd/{destination_descriptor}?mode=rw",
            uri=True,
            timeout=0.1,
        )
        with closing(destination_connection):
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
            _use_rollback_journal(destination_connection)
            if source.expected_schema_version is not None:
                try:
                    source_schema_version = _connection_schema_version(
                        source_connection
                    )
                except sqlite3.Error as error:
                    raise BackupError("unable to validate source schema") from error
                if source_schema_version != source.expected_schema_version:
                    raise BackupError("unsupported source schema")
        _require_database_identity(source)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("incomplete file write")
        remaining = remaining[written:]


def _is_busy(error: sqlite3.OperationalError) -> bool:
    return getattr(error, "sqlite_errorcode", None) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }


def _integrity_ok(path_or_descriptor: Path | int, *, immutable: bool = False) -> bool:
    target, uri = _database_target(path_or_descriptor, immutable=immutable)
    with sqlite3.connect(target, uri=uri) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            and connection.execute("PRAGMA foreign_key_check").fetchone() is None
        )


def _safe_integrity_ok(
    path_or_descriptor: Path | int, *, immutable: bool = False
) -> bool:
    try:
        return _integrity_ok(path_or_descriptor, immutable=immutable)
    except sqlite3.Error:
        return False


def _seal_fresh_snapshot(
    source_path: Path,
    source_descriptor: int,
    destination_path: Path,
) -> _OwnedFile | None:
    source = _PinnedDatabase(source_path, source_descriptor)
    target = f"file:/proc/self/fd/{source_descriptor}?mode=rw"
    try:
        fence = sqlite3.connect(target, uri=True, timeout=0.1, isolation_level=None)
    except sqlite3.Error:
        return None
    sealed: list[_OwnedFile] = []
    try:
        _require_connection_identity(fence, source)
        fence.execute("BEGIN IMMEDIATE")
        _require_database_identity(source)
        destination_descriptor = _open_owned_file(destination_path, os.O_RDWR, sealed)
        source_target = f"file:/proc/self/fd/{source_descriptor}?mode=ro"
        destination_target = f"file:/proc/self/fd/{destination_descriptor}?mode=rw"
        with (
            closing(
                sqlite3.connect(source_target, uri=True, timeout=0.1)
            ) as source_connection,
            closing(
                sqlite3.connect(destination_target, uri=True, timeout=0.1)
            ) as destination_connection,
        ):
            _require_connection_identity(source_connection, source)
            source_connection.backup(destination_connection)
            destination_connection.commit()
            _use_rollback_journal(destination_connection)
        _require_database_identity(source)
        if not _sqlite_sidecars_absent(destination_path, sealed[0].parent_descriptor):
            raise BackupError("sealed quarantine contains SQLite sidecars")
        os.fchmod(destination_descriptor, 0o400)
        os.fsync(destination_descriptor)
        if not _path_matches_descriptor(destination_path, destination_descriptor):
            raise BackupError("sealed quarantine identity changed")
        _fsync_owned_parents(sealed)
    except (OSError, sqlite3.Error, BackupError):
        _cleanup_created(sealed)
        sealed.clear()
        return None
    except BaseException:
        _cleanup_created(sealed)
        sealed.clear()
        raise
    finally:
        try:
            try:
                fence.rollback()
            finally:
                fence.close()
        except BaseException:
            _cleanup_created(sealed)
            sealed.clear()
            raise
    return sealed[0]


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
    sidecar_paths = tuple(
        Path(f"{destination}{suffix}") for suffix in _SQLITE_SIDECAR_SUFFIXES
    )
    require_utc(created_at, "backup timestamp")
    _validate_existing_file(source, "source")
    _validate_new_file(destination, "destination")
    _validate_new_file(metadata_path, "metadata destination")
    for sidecar_path in sidecar_paths:
        _validate_new_file(sidecar_path, "destination sidecar")
    pinned_source = _pin_database(
        source,
        "backup source",
        pin_sidecars=True,
        expected_schema_version=expected_schema_version,
    )
    try:
        _require_database_identity(pinned_source)
        owned: list[_OwnedFile] = []
        try:
            for sidecar_path in sidecar_paths:
                _open_owned_file(sidecar_path, os.O_RDWR, owned)
            destination_descriptor = _open_owned_file(destination, os.O_RDWR, owned)
            _sqlite_copy(pinned_source, destination_descriptor)
            _cleanup_created(owned, count=len(sidecar_paths))
            for sidecar_path in sidecar_paths:
                _validate_new_file(sidecar_path, "destination sidecar")
            if after_copy is not None:
                after_copy(destination)
            if not _path_matches_descriptor(destination, destination_descriptor):
                raise BackupError("backup destination identity changed")
            if (
                _schema_version(destination_descriptor, immutable=True)
                != expected_schema_version
            ):
                raise BackupError("backup schema verification failed")
            if not _integrity_ok(destination_descriptor, immutable=True):
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
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            if not all(
                _path_matches_descriptor(output.path, output.descriptor)
                for output in owned
            ):
                raise BackupError("backup output identity changed")
            for sidecar_path in sidecar_paths:
                _validate_new_file(sidecar_path, "destination sidecar")
            _fsync_owned_parents(owned)
        except sqlite3.OperationalError as error:
            _cleanup_created_resumable(owned)
            if _is_busy(error):
                raise BackupBusyError("SQLite source is busy") from error
            raise BackupError("SQLite backup failed") from error
        except BackupError:
            _cleanup_created_resumable(owned)
            raise
        except Exception as error:
            _cleanup_created_resumable(owned)
            raise BackupError("SQLite backup failed") from error
        except BaseException:
            _cleanup_created_resumable(owned)
            raise
        _close_owned(owned)
    finally:
        _close_pinned_database(pinned_source)
    return BackupMetadata(metadata_path, expected_schema_version, created_at)


def _load_metadata(backup: Path, expected_schema_version: int) -> BackupMetadata:
    metadata_path = Path(f"{backup}.metadata.json")
    _validate_existing_file(metadata_path, "backup metadata")
    pinned_metadata = _pin_database(metadata_path, "backup metadata")
    try:
        payload = bytearray()
        while len(payload) <= MAX_BACKUP_METADATA_BYTES:
            chunk = os.read(
                pinned_metadata.descriptor,
                MAX_BACKUP_METADATA_BYTES + 1 - len(payload),
            )
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > MAX_BACKUP_METADATA_BYTES:
            raise BackupError("invalid backup metadata")
        if not _path_matches_descriptor(metadata_path, pinned_metadata.descriptor):
            raise BackupError("invalid backup metadata")
        raw = json.loads(payload.decode("utf-8"))
        created_at = datetime.fromisoformat(raw["created_at"])
        valid = (
            raw["format_version"] == BACKUP_FORMAT_VERSION
            and raw["schema_version"] == expected_schema_version
            and raw["database_filename"] == backup.name
        )
        require_utc(created_at, "backup timestamp")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as error:
        raise BackupError("invalid backup metadata") from error
    finally:
        _close_pinned_database(pinned_metadata)
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
        staging = quarantine.with_name(f".privacy-restore-{uuid4()}.sqlite3")
        pinned_backup = _pin_database(
            backup, "backup", immutable=True, pin_sidecars=True
        )
        owned: list[_OwnedFile] = []
        try:
            _require_database_identity(pinned_backup)
            _load_metadata(backup, self.expected_schema_version)
            try:
                backup_schema_version = _schema_version(
                    pinned_backup.descriptor, immutable=pinned_backup.immutable
                )
            except BackupError as error:
                raise BackupError("unsupported backup schema") from error
            if backup_schema_version != self.expected_schema_version:
                raise BackupError("unsupported backup schema")
            _require_database_identity(pinned_backup)
            try:
                quarantine_descriptor = _open_owned_file(staging, os.O_RDWR, owned)
                _sqlite_copy(pinned_backup, quarantine_descriptor)
                _fsync_owned_parents(owned)
            except (OSError, sqlite3.Error, BackupError) as error:
                _cleanup_created(owned)
                raise BackupError("quarantine copy failed") from error
            except BaseException:
                _cleanup_created(owned)
                raise
        finally:
            _close_pinned_database(pinned_backup)
        try:
            try:
                result = self._finish_restore(
                    staging,
                    quarantine,
                    quarantine_descriptor,
                    ready_path,
                    restored_at,
                    owned,
                )
                if (
                    not result.activation_eligible
                    and owned
                    and owned[0].path != quarantine
                    and _path_matches_descriptor(owned[0].path, owned[0].descriptor)
                ):
                    _publish_owned_file(owned[0].path, quarantine, owned[0])
                return result
            except BaseException:
                if owned and _path_matches_descriptor(
                    owned[0].path, owned[0].descriptor
                ):
                    _cleanup_created(owned)
                    owned.clear()
                raise
        finally:
            _close_owned(owned)

    def _finish_restore(
        self,
        staging: Path,
        quarantine: Path,
        quarantine_descriptor: int,
        ready_path: Path,
        restored_at: datetime,
        owned: list[_OwnedFile],
    ) -> RestoreResult:
        owned_quarantine = owned[0]
        restore_id = f"restore-{uuid4()}"
        try:
            self._require_quarantine_identity(staging, quarantine_descriptor)
            if not _safe_integrity_ok(quarantine_descriptor):
                return self._blocked_restore(
                    restore_id, restored_at, "quarantine_integrity_failed"
                )
            self._require_quarantine_identity(staging, quarantine_descriptor)
            snapshot = self.ledger.reconciliation_snapshot()
            try:
                adapter = self.adapter_factory(
                    Path(f"/proc/self/fd/{quarantine_descriptor}")
                )
            except Exception:
                _cleanup_created_database(owned)
                return self._blocked_restore(
                    restore_id, restored_at, "adapter_initialization_failure"
                )
            except BaseException:
                _cleanup_created_database(owned)
                raise
            try:
                self._require_quarantine_identity(staging, quarantine_descriptor)
            except _QuarantineIdentityError:
                _cleanup_created_database(owned)
                return self._blocked_restore(
                    restore_id, restored_at, "quarantine_identity_changed"
                )
            try:
                reason = self._reconcile(adapter, snapshot)
            except BaseException:
                _cleanup_created_database(owned)
                raise
            if reason is not None:
                _cleanup_created_database(owned)
                return self._blocked_restore(restore_id, restored_at, reason)
            self._require_quarantine_identity(staging, quarantine_descriptor)
            sealed_staging = quarantine.with_name(f".privacy-sealed-{uuid4()}.sqlite3")
            sealed_quarantine = _seal_fresh_snapshot(
                staging,
                quarantine_descriptor,
                sealed_staging,
            )
            if sealed_quarantine is None:
                _cleanup_created_database(owned)
                return self._blocked_restore(
                    restore_id, restored_at, "quarantine_finalization_failed"
                )
            owned.append(sealed_quarantine)
            _cleanup_created_database(owned, count=1)
            staging = sealed_staging
            quarantine_descriptor = sealed_quarantine.descriptor
            owned_quarantine = sealed_quarantine
            self._require_quarantine_identity(staging, quarantine_descriptor)
            try:
                schema_matches = (
                    _schema_version(quarantine_descriptor, immutable=True)
                    == self.expected_schema_version
                )
            except BackupError:
                schema_matches = False
            if not schema_matches:
                return self._blocked_restore(
                    restore_id, restored_at, "post_reconciliation_schema_invalid"
                )
            self._require_quarantine_identity(staging, quarantine_descriptor)
            if not _safe_integrity_ok(quarantine_descriptor, immutable=True):
                return self._blocked_restore(
                    restore_id, restored_at, "post_reconciliation_integrity_failed"
                )
            self._require_quarantine_identity(staging, quarantine_descriptor)
            if self.ledger.control_generation() != snapshot.generation:
                return self._blocked_restore(
                    restore_id,
                    restored_at,
                    "privacy_ledger_changed_during_restore",
                )
            self._require_quarantine_identity(staging, quarantine_descriptor)
        except _QuarantineIdentityError:
            return self._blocked_restore(
                restore_id, restored_at, "quarantine_identity_changed"
            )
        quarantine_identity = os.fstat(quarantine_descriptor)
        quarantine_sha256 = _descriptor_sha256(quarantine_descriptor)
        _publish_owned_file(staging, quarantine, owned_quarantine)
        self._require_quarantine_identity(quarantine, quarantine_descriptor)
        marker = json.dumps(
            {
                "restore_id": restore_id,
                "disposition": "eligible_for_activation",
                "ledger_generation": snapshot.generation,
                "database_device": quarantine_identity.st_dev,
                "database_inode": quarantine_identity.st_ino,
                "database_size": quarantine_identity.st_size,
                "database_mode": "0400",
                "database_sha256": quarantine_sha256,
            },
            sort_keys=True,
        ).encode("utf-8")
        marker_files: list[_OwnedFile] = []
        try:
            descriptor = _open_owned_file(ready_path, os.O_WRONLY, marker_files)
            _write_all(descriptor, marker)
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
            if not _path_matches_descriptor(ready_path, descriptor):
                raise BackupError("readiness marker identity changed")
            self._require_quarantine_identity(quarantine, quarantine_descriptor)
            current_identity = os.fstat(quarantine_descriptor)
            if (
                current_identity.st_dev,
                current_identity.st_ino,
                current_identity.st_size,
                stat.S_IMODE(current_identity.st_mode),
                _descriptor_sha256(quarantine_descriptor),
            ) != (
                quarantine_identity.st_dev,
                quarantine_identity.st_ino,
                quarantine_identity.st_size,
                0o400,
                quarantine_sha256,
            ):
                raise BackupError("quarantine seal changed")
            if not _sqlite_sidecars_absent(
                quarantine, marker_files[0].parent_descriptor
            ):
                raise BackupError("quarantine sidecar appeared")
            if self.ledger.control_generation() != snapshot.generation:
                raise BackupError("privacy ledger changed before readiness")
            _fsync_owned_parents(marker_files)
        except Exception as error:
            _cleanup_created(marker_files)
            raise BackupError("readiness marker write failed") from error
        except BaseException:
            _cleanup_created(marker_files)
            raise
        published_files = owned.copy()
        try:
            recorded = self.ledger._record_restore(
                restore_id,
                restored_at,
                RestoreDisposition.ELIGIBLE_FOR_ACTIVATION,
                "reconciliation_complete",
                expected_generation=snapshot.generation,
            )
        except BaseException:
            _cleanup_created(marker_files)
            raise
        if not recorded:
            _cleanup_created(marker_files)
            return self._blocked_restore(
                restore_id,
                restored_at,
                "privacy_ledger_changed_during_restore",
            )
        owned.clear()
        try:
            _close_owned(marker_files)
        finally:
            _close_owned(published_files)
        return RestoreResult(
            restore_id,
            RestoreDisposition.ELIGIBLE_FOR_ACTIVATION,
            True,
            "reconciliation_complete",
            snapshot.generation,
        )

    def _blocked_restore(
        self, restore_id: str, restored_at: datetime, reason: str
    ) -> RestoreResult:
        result = RestoreResult(
            restore_id,
            RestoreDisposition.BLOCKED,
            False,
            reason,
        )
        self.ledger._record_restore(
            restore_id, restored_at, result.disposition, result.reason
        )
        return result

    @staticmethod
    def _require_quarantine_identity(quarantine: Path, descriptor: int) -> None:
        if not _path_matches_descriptor(quarantine, descriptor):
            raise _QuarantineIdentityError

    def _reconcile(
        self, adapter: RestoreAdapter, snapshot: PrivacyReconciliationSnapshot
    ) -> str | None:
        try:
            reason = self._reconcile_operations(adapter, snapshot)
        finally:
            release_reason = self._release_adapter_writers(adapter)
        return release_reason or reason

    @staticmethod
    def _reconcile_operations(
        adapter: RestoreAdapter, snapshot: PrivacyReconciliationSnapshot
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

    @staticmethod
    def _release_adapter_writers(adapter: RestoreAdapter) -> str | None:
        try:
            if adapter.release_writers() is not True:
                return "adapter_writer_release_incomplete"
        except Exception:
            return "adapter_writer_release_failure"
        return None
