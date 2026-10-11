from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading

import pytest

from app.privacy import recovery as recovery_module

from app.privacy import (
    BackupBusyError,
    BackupError,
    ConsentGrant,
    ConsentPurpose,
    ConsentService,
    ContentCategory,
    DeletionCoordinator,
    DeletionDisposition,
    DeletionRequest,
    ErasureReport,
    NOTICE_RU,
    NOTICE_VERSION,
    NoticeService,
    OperationOrigin,
    PrivacyLedger,
    QuarantinedRestore,
    RestoreDisposition,
    TrustedPrivacyContext,
    create_sqlite_backup,
)


NOW = datetime(2026, 10, 10, 14, 0, tzinfo=UTC)


def context():
    return TrustedPrivacyContext(
        "participant-a", "actor-a", "auth-a", OperationOrigin.CONTROL
    )


def make_source(path, message="synthetic deleted private message CANARY-RESTORE"):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA user_version=3")
    connection.execute(
        "CREATE TABLE schema_metadata(singleton INTEGER PRIMARY KEY, version INTEGER)"
    )
    connection.execute("INSERT INTO schema_metadata VALUES(1, 3)")
    connection.execute(
        "CREATE TABLE synthetic_private(event_id TEXT PRIMARY KEY, content TEXT)"
    )
    connection.execute(
        "INSERT INTO synthetic_private VALUES('event-001', ?)", (message,)
    )
    connection.commit()
    return connection


class RestoreAdapter:
    def __init__(self, path, *, fail=False):
        self.path = path
        self.fail = fail
        self.revocations = []

    def apply_deletion(self, record):
        if self.fail:
            return ErasureReport(False, False, frozenset(), ("integration_blocked",))
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "DELETE FROM synthetic_private WHERE event_id = ?",
                (record.content_reference,),
            )
        return ErasureReport(
            True,
            True,
            frozenset(ContentCategory.required_erasure_categories()),
            (),
        )

    def apply_revocation(self, consent_id, revoked_at):
        self.revocations.append((consent_id, revoked_at))
        return not self.fail

    def rebuild_dependencies(self):
        return not self.fail

    def release_writers(self):
        return True


def pending_deletion(ledger):
    request = DeletionRequest(
        "delete-operation-001",
        "participant-a",
        "event-001",
        ContentCategory.PRIVATE_PAYLOAD,
        NOW + timedelta(minutes=10),
        "actor-a",
        "delete-correlation-001",
        "auth-a",
    )

    class Crash(BaseException):
        pass

    with pytest.raises(Crash):
        DeletionCoordinator(ledger, object()).delete(
            context(), request, after_pending=lambda _: (_ for _ in ()).throw(Crash())
        )


def test_wal_consistent_backup_under_writes_and_owner_only_files(tmp_path):
    source = tmp_path / "source.sqlite3"
    writer = make_source(source, "synthetic baseline")
    stop = threading.Event()

    def write_rows():
        index = 0
        connection = sqlite3.connect(source)
        while not stop.is_set():
            connection.execute(
                "INSERT OR REPLACE INTO synthetic_private VALUES(?, ?)",
                (f"event-{index + 2}", f"synthetic-{index}"),
            )
            connection.commit()
            index += 1
        connection.close()

    thread = threading.Thread(target=write_rows)
    thread.start()
    destination = tmp_path / "backup.sqlite3"
    try:
        metadata = create_sqlite_backup(source, destination, 3, NOW)
    finally:
        stop.set()
        thread.join()
        writer.close()

    assert not Path(f"{destination}-wal").exists()
    assert not Path(f"{destination}-shm").exists()
    with sqlite3.connect(destination) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            restored.execute("SELECT COUNT(*) FROM synthetic_private").fetchone()[0]
            >= 1
        )
    assert metadata.schema_version == 3
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(metadata.path.stat().st_mode) == 0o600


def test_backup_percent_escapes_sqlite_uri_metacharacters(tmp_path):
    source = tmp_path / "source#snapshot?.sqlite3"
    make_source(source, "literal metacharacter source").close()
    decoy = tmp_path / "source"
    make_source(decoy, "wrong database").close()
    destination = tmp_path / "escaped-backup.sqlite3"

    create_sqlite_backup(source, destination, 3, NOW)

    with sqlite3.connect(destination) as connection:
        assert connection.execute(
            "SELECT content FROM synthetic_private WHERE event_id='event-001'"
        ).fetchone() == ("literal metacharacter source",)


def test_schema_version_accepts_a_database_path(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()

    assert recovery_module._schema_version(source) == 3


@pytest.mark.parametrize(
    ("mode", "uid", "has_descendant", "expected"),
    [
        (0o700, os.geteuid(), False, True),
        (0o700, os.geteuid() + 1, False, False),
        (0o770, os.geteuid(), False, False),
        (0o1777, 0, True, True),
        (0o1777, 0, False, False),
    ],
)
def test_recovery_directory_trust_policy(mode, uid, has_descendant, expected):
    identity = os.stat_result((stat.S_IFDIR | mode, 0, 0, 0, uid, 0, 0, 0, 0, 0))
    assert (
        recovery_module._directory_is_trusted(identity, has_descendant=has_descendant)
        is expected
    )


def test_backup_rejects_direct_or_ancestor_writable_by_other_accounts(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    unsafe = tmp_path / "unsafe"
    nested = unsafe / "nested"
    nested.mkdir(parents=True)
    unsafe.chmod(0o777)

    for destination in (unsafe / "direct.sqlite3", nested / "nested.sqlite3"):
        with pytest.raises(BackupError, match="parent is unsafe"):
            create_sqlite_backup(source, destination, 3, NOW)


@pytest.mark.parametrize("input_kind", ["source", "backup", "metadata"])
def test_backup_and_restore_reject_inputs_writable_by_other_accounts(
    tmp_path, input_kind
):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"

    if input_kind == "source":
        source.chmod(0o620)
        with pytest.raises(BackupError, match="source file is unsafe"):
            create_sqlite_backup(source, backup, 3, NOW)
        assert not backup.exists()
        return

    create_sqlite_backup(source, backup, 3, NOW)
    target = backup if input_kind == "backup" else Path(f"{backup}.metadata.json")
    target.chmod(0o602)
    quarantine = tmp_path / "quarantine.sqlite3"
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")

    with pytest.raises(BackupError, match=f"{input_kind} file is unsafe"):
        QuarantinedRestore(ledger, 3, RestoreAdapter).restore(backup, quarantine, NOW)

    assert not quarantine.exists()


def test_backup_pin_rejects_a_source_owned_by_another_account(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    source_identity = source.stat()
    destination = tmp_path / "backup.sqlite3"
    real_fstat = os.fstat

    def report_foreign_owner(descriptor):
        identity = real_fstat(descriptor)
        if (identity.st_dev, identity.st_ino) == (
            source_identity.st_dev,
            source_identity.st_ino,
        ):
            values = list(identity)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return identity

    monkeypatch.setattr(recovery_module.os, "fstat", report_foreign_owner)

    with pytest.raises(BackupError, match="source identity"):
        create_sqlite_backup(source, destination, 3, NOW)

    assert not destination.exists()


def test_backup_rejects_an_unsafe_source_wal_before_copying(tmp_path):
    source = tmp_path / "source.sqlite3"
    writer = make_source(source, "row-present-only-through-wal")
    wal = Path(f"{source}-wal")
    wal.chmod(0o666)
    destination = tmp_path / "backup.sqlite3"

    try:
        with pytest.raises(BackupError, match="source identity"):
            create_sqlite_backup(source, destination, 3, NOW)
    finally:
        writer.close()

    assert not destination.exists()


def test_backup_rejects_an_unsafe_source_wal_created_after_pinning(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    destination = tmp_path / "backup.sqlite3"
    real_copy = recovery_module._sqlite_copy
    writers = []

    def create_wal_after_pinning(pinned_source, destination_descriptor):
        writer = sqlite3.connect(source)
        writers.append(writer)
        writer.execute(
            "INSERT INTO synthetic_private VALUES('event-late', 'unsafe WAL row')"
        )
        writer.commit()
        Path(f"{source}-wal").chmod(0o666)
        real_copy(pinned_source, destination_descriptor)

    monkeypatch.setattr(recovery_module, "_sqlite_copy", create_wal_after_pinning)
    try:
        with pytest.raises(BackupError, match="source identity"):
            create_sqlite_backup(source, destination, 3, NOW)
    finally:
        for writer in writers:
            writer.close()

    assert not destination.exists()


def test_backup_rejects_a_source_swap_during_sidecar_repinning(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    make_source(source, "intended source").close()
    held_source = tmp_path / "held-source.sqlite3"
    substitute = tmp_path / "substitute.sqlite3"
    make_source(substitute, "substitute source").close()
    pinned_source = recovery_module._pin_database(
        source, "backup source", pin_sidecars=True
    )
    real_pin = recovery_module._pin_database

    def swap_before_repinning(path, label, **kwargs):
        source.rename(held_source)
        substitute.rename(source)
        return real_pin(path, label, **kwargs)

    monkeypatch.setattr(recovery_module, "_pin_database", swap_before_repinning)
    try:
        with pytest.raises(BackupError, match="source identity"):
            recovery_module._require_database_identity(pinned_source)
    finally:
        recovery_module._close_pinned_database(pinned_source)

    with sqlite3.connect(source) as connection:
        assert connection.execute(
            "SELECT content FROM synthetic_private WHERE event_id='event-001'"
        ).fetchone() == ("substitute source",)


def test_backup_pin_closes_source_and_sidecars_when_validation_errors(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite3"
    writer = make_source(source)
    source_identity = source.stat()
    destination = tmp_path / "backup.sqlite3"
    real_fstat = os.fstat

    def fail_source_fstat(descriptor):
        identity = real_fstat(descriptor)
        if (identity.st_dev, identity.st_ino) == (
            source_identity.st_dev,
            source_identity.st_ino,
        ):
            raise OSError("source identity unavailable")
        return identity

    monkeypatch.setattr(recovery_module.os, "fstat", fail_source_fstat)
    try:
        with pytest.raises(BackupError, match="source identity"):
            create_sqlite_backup(source, destination, 3, NOW)
    finally:
        writer.close()

    assert not destination.exists()


def test_backup_rejects_foreign_key_violations(tmp_path):
    source = tmp_path / "source.sqlite3"
    with make_source(source) as connection:
        connection.execute("CREATE TABLE parents(parent_id TEXT PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE children(parent_id TEXT REFERENCES parents(parent_id))"
        )
        connection.execute("INSERT INTO children VALUES('missing-parent')")

    destination = tmp_path / "backup.sqlite3"
    with pytest.raises(BackupError, match="consistency verification failed"):
        create_sqlite_backup(source, destination, 3, NOW)
    assert not destination.exists()


def test_restore_blocks_foreign_key_violations_before_reconciliation(tmp_path):
    source = tmp_path / "source.sqlite3"
    with make_source(source) as connection:
        connection.execute("CREATE TABLE parents(parent_id TEXT PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE children(parent_id TEXT REFERENCES parents(parent_id))"
        )
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    with sqlite3.connect(backup) as connection:
        connection.execute("INSERT INTO children VALUES('missing-parent')")

    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    result = QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
        backup, tmp_path / "quarantine.sqlite3", NOW
    )

    assert result.disposition is RestoreDisposition.BLOCKED
    assert result.reason == "quarantine_integrity_failed"


def test_backup_rejects_unsafe_paths_schema_overwrite_and_interruptions(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    with pytest.raises(BackupError, match="absolute"):
        create_sqlite_backup(Path("relative.sqlite3"), tmp_path / "x.sqlite3", 3, NOW)
    with pytest.raises(BackupError, match="absolute"):
        create_sqlite_backup(source, Path("relative.sqlite3"), 3, NOW)
    symlink = tmp_path / "source-link.sqlite3"
    symlink.symlink_to(source)
    with pytest.raises(BackupError, match="symbolic"):
        create_sqlite_backup(symlink, tmp_path / "x.sqlite3", 3, NOW)
    source_directory = tmp_path / "source-directory"
    source_directory.mkdir()
    nested_source = source_directory / "source.sqlite3"
    make_source(nested_source).close()
    source_parent_link = tmp_path / "source-parent-link"
    source_parent_link.symlink_to(source_directory, target_is_directory=True)
    with pytest.raises(BackupError, match="parent"):
        create_sqlite_backup(
            source_parent_link / nested_source.name,
            tmp_path / "ancestor-source.sqlite3",
            3,
            NOW,
        )
    dangling = tmp_path / "dangling.sqlite3"
    dangling.symlink_to(tmp_path / "missing-target")
    with pytest.raises(BackupError, match="symbolic"):
        create_sqlite_backup(source, dangling, 3, NOW)
    directory = tmp_path / "directory.sqlite3"
    directory.mkdir()
    with pytest.raises(BackupError, match="regular"):
        create_sqlite_backup(directory, tmp_path / "x.sqlite3", 3, NOW)
    missing_source = tmp_path / "missing-source.sqlite3"
    with pytest.raises(BackupError, match="regular"):
        create_sqlite_backup(missing_source, tmp_path / "x.sqlite3", 3, NOW)
    with pytest.raises(BackupError, match="parent"):
        create_sqlite_backup(source, tmp_path / "missing" / "x.sqlite3", 3, NOW)
    existing = tmp_path / "existing.sqlite3"
    existing.touch()
    with pytest.raises(BackupError, match="exists"):
        create_sqlite_backup(source, existing, 3, NOW)
    with pytest.raises(BackupError, match="schema"):
        create_sqlite_backup(source, tmp_path / "wrong.sqlite3", 4, NOW)

    interrupted = tmp_path / "interrupted.sqlite3"

    def interrupt(_):
        raise RuntimeError("private backup content")

    with pytest.raises(BackupError, match="backup failed") as caught:
        create_sqlite_backup(source, interrupted, 3, NOW, after_copy=interrupt)
    assert "private backup content" not in str(caught.value)
    assert not interrupted.exists()
    assert not Path(f"{interrupted}.metadata.json").exists()


def test_backup_interrupt_cleans_owned_files_and_descriptors(tmp_path, monkeypatch):
    class SimulatedInterrupt(BaseException):
        pass

    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    destination = tmp_path / "interrupted.sqlite3"
    opened = []
    real_open_owned_file = recovery_module._open_owned_file

    def track_open(path, access, owned):
        descriptor = real_open_owned_file(path, access, owned)
        opened.append(owned[-1])
        return descriptor

    monkeypatch.setattr(recovery_module, "_open_owned_file", track_open)
    with pytest.raises(SimulatedInterrupt):
        create_sqlite_backup(
            source,
            destination,
            3,
            NOW,
            after_copy=lambda _: (_ for _ in ()).throw(SimulatedInterrupt()),
        )

    assert not destination.exists()
    assert not Path(f"{destination}.metadata.json").exists()
    for owned_file in opened:
        with pytest.raises(OSError):
            os.fstat(owned_file.descriptor)
        with pytest.raises(OSError):
            os.fstat(owned_file.parent_descriptor)


def test_restore_copy_interrupt_cleans_quarantine_and_descriptors(
    tmp_path, monkeypatch
):
    class SimulatedInterrupt(BaseException):
        pass

    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    quarantine = tmp_path / "quarantine.sqlite3"
    opened = []
    real_open_owned_file = recovery_module._open_owned_file

    def track_open(path, access, owned):
        descriptor = real_open_owned_file(path, access, owned)
        opened.append(owned[-1])
        return descriptor

    monkeypatch.setattr(recovery_module, "_open_owned_file", track_open)
    monkeypatch.setattr(
        recovery_module,
        "_sqlite_copy",
        lambda *_args: (_ for _ in ()).throw(SimulatedInterrupt()),
    )
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")

    with pytest.raises(SimulatedInterrupt):
        QuarantinedRestore(ledger, 3, RestoreAdapter).restore(backup, quarantine, NOW)

    assert not quarantine.exists()
    assert not Path(f"{quarantine}.ready.json").exists()
    for owned_file in opened:
        with pytest.raises(OSError):
            os.fstat(owned_file.descriptor)
        with pytest.raises(OSError):
            os.fstat(owned_file.parent_descriptor)


@pytest.mark.parametrize(
    "race_target",
    [
        "destination",
        "metadata",
        "wal_sidecar",
        "shm_sidecar",
        "journal_sidecar",
    ],
)
def test_backup_race_never_deletes_a_concurrently_created_file(
    tmp_path, monkeypatch, race_target
):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    destination = tmp_path / "backup.sqlite3"
    metadata = Path(f"{destination}.metadata.json")
    raced_path = {
        "destination": destination,
        "metadata": metadata,
        "wal_sidecar": Path(f"{destination}-wal"),
        "shm_sidecar": Path(f"{destination}-shm"),
        "journal_sidecar": Path(f"{destination}-journal"),
    }[race_target]
    real_open_owned_file = recovery_module._open_owned_file

    def race(path, access, owned):
        if path == raced_path:
            path.write_text(f"concurrent-{race_target}")
        return real_open_owned_file(path, access, owned)

    monkeypatch.setattr(recovery_module, "_open_owned_file", race)
    with pytest.raises(BackupError, match="backup failed"):
        create_sqlite_backup(source, destination, 3, NOW)

    assert raced_path.read_text() == f"concurrent-{race_target}"
    if race_target != "destination":
        assert not destination.exists()


def test_backup_rechecks_sidecars_after_the_copy_hook(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    destination = tmp_path / "backup.sqlite3"
    sidecar = Path(f"{destination}-wal")

    def create_sidecar(_destination):
        sidecar.write_text("concurrent-sidecar")

    with pytest.raises(BackupError, match="sidecar"):
        create_sqlite_backup(source, destination, 3, NOW, after_copy=create_sidecar)

    assert sidecar.read_text() == "concurrent-sidecar"
    assert not destination.exists()
    assert not Path(f"{destination}.metadata.json").exists()


@pytest.mark.parametrize("mutation", ["replace", "remove"])
def test_backup_cleanup_only_unlinks_the_inode_it_created(tmp_path, mutation):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    destination = tmp_path / "backup.sqlite3"

    def interrupt(path):
        path.unlink()
        if mutation == "replace":
            path.write_text("concurrent-replacement")
        raise RuntimeError("interrupted")

    with pytest.raises(BackupError, match="backup failed"):
        create_sqlite_backup(source, destination, 3, NOW, after_copy=interrupt)

    if mutation == "replace":
        assert destination.read_text() == "concurrent-replacement"
    else:
        assert not destination.exists()


def test_quarantine_publication_rejects_a_replaced_staging_path(tmp_path):
    staging = tmp_path / "staging.sqlite3"
    destination = tmp_path / "quarantine.sqlite3"
    held = tmp_path / "held.sqlite3"
    owned = []
    recovery_module._open_owned_file(staging, os.O_RDWR, owned)
    staging.rename(held)
    staging.write_text("concurrent-staging")

    with pytest.raises(BackupError, match="staging identity"):
        recovery_module._publish_owned_file(staging, destination, owned[0])

    assert staging.read_text() == "concurrent-staging"
    assert not destination.exists()
    recovery_module._close_owned(owned)


def test_quarantine_publication_rejects_a_substituted_destination(
    tmp_path, monkeypatch
):
    staging = tmp_path / "staging.sqlite3"
    destination = tmp_path / "quarantine.sqlite3"
    owned = []
    recovery_module._open_owned_file(staging, os.O_RDWR, owned)
    real_matches = recovery_module._path_matches_descriptor

    def reject_destination(path, descriptor):
        if path == destination:
            return False
        return real_matches(path, descriptor)

    monkeypatch.setattr(recovery_module, "_path_matches_descriptor", reject_destination)
    with pytest.raises(BackupError, match="publication identity"):
        recovery_module._publish_owned_file(staging, destination, owned[0])

    assert staging.exists()
    assert destination.exists()
    recovery_module._cleanup_created(owned)
    assert not staging.exists()
    assert not destination.exists()


@pytest.mark.parametrize("interrupted_close", ["descriptor", "parent"])
def test_owned_cleanup_can_resume_after_an_interrupted_close(
    tmp_path, monkeypatch, interrupted_close
):
    class SimulatedInterrupt(BaseException):
        pass

    owned = []
    paths = [tmp_path / f"owned-{index}" for index in range(3)]
    for path in paths:
        recovery_module._open_owned_file(path, os.O_RDWR, owned)
    first = owned[0]
    interrupted_descriptor = (
        first.descriptor
        if interrupted_close == "descriptor"
        else first.parent_descriptor
    )
    real_close = recovery_module.os.close
    interrupted = False

    def close_then_interrupt(descriptor):
        nonlocal interrupted
        real_close(descriptor)
        if not interrupted and descriptor == interrupted_descriptor:
            interrupted = True
            raise SimulatedInterrupt()

    monkeypatch.setattr(recovery_module.os, "close", close_then_interrupt)

    with pytest.raises(SimulatedInterrupt):
        recovery_module._cleanup_created(owned)

    assert interrupted is True
    assert not paths[0].exists()
    assert len(owned) == 2
    with pytest.raises(OSError):
        os.fstat(first.descriptor)
    with pytest.raises(OSError):
        os.fstat(first.parent_descriptor)
    recovery_module._cleanup_created(owned)

    assert owned == []
    assert not any(path.exists() for path in paths)


def test_backup_failure_cleanup_resumes_after_an_interrupted_close(
    tmp_path, monkeypatch
):
    class SimulatedInterrupt(BaseException):
        pass

    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    destination = tmp_path / "backup.sqlite3"
    metadata = Path(f"{destination}.metadata.json")
    sidecars = [
        Path(f"{destination}{suffix}")
        for suffix in recovery_module._SQLITE_SIDECAR_SUFFIXES
    ]
    real_open_owned_file = recovery_module._open_owned_file
    opened = []

    def track_owned_file(path, access, owned):
        descriptor = real_open_owned_file(path, access, owned)
        opened.append(owned[-1])
        return descriptor

    real_copy = recovery_module._sqlite_copy

    def fail_copy(_source, _destination_descriptor):
        raise recovery_module.BackupError("synthetic copy failure")

    real_close = recovery_module.os.close
    interrupted = False

    def close_then_interrupt(descriptor):
        nonlocal interrupted
        real_close(descriptor)
        if opened and not interrupted and descriptor == opened[0].descriptor:
            interrupted = True
            raise SimulatedInterrupt()

    monkeypatch.setattr(recovery_module, "_open_owned_file", track_owned_file)
    monkeypatch.setattr(recovery_module, "_sqlite_copy", fail_copy)
    monkeypatch.setattr(recovery_module.os, "close", close_then_interrupt)

    with pytest.raises(SimulatedInterrupt):
        create_sqlite_backup(source, destination, 3, NOW)

    assert interrupted is True
    assert not destination.exists()
    assert not metadata.exists()
    assert not any(sidecar.exists() for sidecar in sidecars)
    for output in opened:
        with pytest.raises(OSError):
            os.fstat(output.descriptor)
        with pytest.raises(OSError):
            os.fstat(output.parent_descriptor)

    monkeypatch.setattr(recovery_module, "_sqlite_copy", real_copy)
    created = create_sqlite_backup(source, destination, 3, NOW)
    assert destination.exists()
    assert created.path == metadata


@pytest.mark.parametrize("timing", ["before", "after"])
def test_restore_cleans_both_publication_links_when_staging_unlink_is_interrupted(
    tmp_path, monkeypatch, timing
):
    class SimulatedInterrupt(BaseException):
        pass

    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    manager = QuarantinedRestore(ledger, 3, RestoreAdapter)
    quarantine = tmp_path / "quarantine.sqlite3"
    real_unlink = recovery_module.os.unlink
    interrupted = False

    def interrupt_sealed_staging_unlink(path, *args, **kwargs):
        nonlocal interrupted
        if not interrupted and str(path).startswith(".privacy-sealed-"):
            if timing == "after":
                real_unlink(path, *args, **kwargs)
            interrupted = True
            raise SimulatedInterrupt()
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(recovery_module.os, "unlink", interrupt_sealed_staging_unlink)

    with pytest.raises(SimulatedInterrupt):
        manager.restore(backup, quarantine, NOW)

    assert interrupted is True
    assert not quarantine.exists()
    assert not Path(f"{quarantine}.ready.json").exists()
    assert not list(tmp_path.glob(".privacy-restore-*.sqlite3"))
    assert not list(tmp_path.glob(".privacy-sealed-*.sqlite3"))

    result = manager.restore(backup, quarantine, NOW)
    assert result.disposition is RestoreDisposition.ELIGIBLE_FOR_ACTIVATION


def test_restore_cleans_publication_when_link_succeeds_then_interrupts(
    tmp_path, monkeypatch
):
    class SimulatedInterrupt(BaseException):
        pass

    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    manager = QuarantinedRestore(ledger, 3, RestoreAdapter)
    quarantine = tmp_path / "quarantine.sqlite3"
    real_link = recovery_module.os.link
    interrupted = False

    def link_then_interrupt(*args, **kwargs):
        nonlocal interrupted
        result = real_link(*args, **kwargs)
        if not interrupted:
            interrupted = True
            raise SimulatedInterrupt()
        return result

    monkeypatch.setattr(recovery_module.os, "link", link_then_interrupt)

    with pytest.raises(SimulatedInterrupt):
        manager.restore(backup, quarantine, NOW)

    assert interrupted is True
    assert not quarantine.exists()
    assert not Path(f"{quarantine}.ready.json").exists()
    assert not list(tmp_path.glob(".privacy-restore-*.sqlite3"))
    assert not list(tmp_path.glob(".privacy-sealed-*.sqlite3"))
    result = manager.restore(backup, quarantine, NOW)
    assert result.disposition is RestoreDisposition.ELIGIBLE_FOR_ACTIVATION


def test_backup_copy_cannot_follow_a_replaced_destination_path(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    make_source(source, "intended-source").close()
    victim = tmp_path / "victim.sqlite3"
    make_source(victim, "unchanged-victim").close()
    destination = tmp_path / "backup.sqlite3"
    real_copy = recovery_module._sqlite_copy

    def replace_before_copy(source_path, destination_descriptor):
        destination.unlink()
        destination.symlink_to(victim)
        return real_copy(source_path, destination_descriptor)

    monkeypatch.setattr(recovery_module, "_sqlite_copy", replace_before_copy)
    with pytest.raises(BackupError, match="backup failed"):
        create_sqlite_backup(source, destination, 3, NOW)

    with sqlite3.connect(victim) as connection:
        assert connection.execute(
            "SELECT content FROM synthetic_private WHERE event_id='event-001'"
        ).fetchone() == ("unchanged-victim",)
    assert destination.is_symlink()


def test_backup_copy_cannot_follow_a_replaced_source_path(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    make_source(source, "intended-source").close()
    substitute = tmp_path / "substitute.sqlite3"
    make_source(substitute, "substitute-source").close()
    destination = tmp_path / "backup.sqlite3"
    real_copy = recovery_module._sqlite_copy

    def replace_before_copy(pinned_source, destination_descriptor):
        source.unlink()
        source.symlink_to(substitute)
        return real_copy(pinned_source, destination_descriptor)

    monkeypatch.setattr(recovery_module, "_sqlite_copy", replace_before_copy)
    with pytest.raises(BackupError, match="source identity"):
        create_sqlite_backup(source, destination, 3, NOW)

    assert source.is_symlink()
    assert not destination.exists()
    with sqlite3.connect(substitute) as connection:
        assert connection.execute(
            "SELECT content FROM synthetic_private WHERE event_id='event-001'"
        ).fetchone() == ("substitute-source",)


def test_backup_source_connection_rejects_aba_path_replacement(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    make_source(source, "intended-source").close()
    substitute = tmp_path / "substitute.sqlite3"
    make_source(substitute, "substitute-source").close()
    held_source = tmp_path / "held-source.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    real_connect = recovery_module.sqlite3.connect
    replaced = False

    def replace_during_connect(target, *args, **kwargs):
        nonlocal replaced
        if not replaced and str(target).startswith("file:/proc/self/fd/"):
            replaced = True
            source.rename(held_source)
            substitute.rename(source)
            connection = real_connect(target, *args, **kwargs)
            source.rename(substitute)
            held_source.rename(source)
            return connection
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(recovery_module.sqlite3, "connect", replace_during_connect)
    with pytest.raises(BackupError, match="connection identity"):
        create_sqlite_backup(source, destination, 3, NOW)

    assert not destination.exists()
    with sqlite3.connect(source) as connection:
        assert connection.execute(
            "SELECT content FROM synthetic_private WHERE event_id='event-001'"
        ).fetchone() == ("intended-source",)
    with sqlite3.connect(substitute) as connection:
        assert connection.execute(
            "SELECT content FROM synthetic_private WHERE event_id='event-001'"
        ).fetchone() == ("substitute-source",)


@pytest.mark.parametrize("race", ["open", "identity"])
def test_backup_source_pin_rejects_path_races(tmp_path, monkeypatch, race):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    destination = tmp_path / "backup.sqlite3"
    real_open = recovery_module.os.open

    def race_open(path, flags, *args, **kwargs):
        if path == source.name and flags == (
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        ):
            if race == "open":
                source.unlink()
                return real_open(path, flags, *args, **kwargs)
            descriptor = real_open(path, flags, *args, **kwargs)
            source.unlink()
            source.write_text("replacement")
            return descriptor
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(recovery_module.os, "open", race_open)
    with pytest.raises(BackupError, match="identity"):
        create_sqlite_backup(source, destination, 3, NOW)
    assert not destination.exists()


def test_backup_source_pin_rejects_an_ancestor_race(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    destination = tmp_path / "backup.sqlite3"

    def race_parent(_directory):
        raise OSError("ancestor replaced")

    monkeypatch.setattr(recovery_module, "_validate_parent_chain", lambda *_: None)
    monkeypatch.setattr(recovery_module, "_open_directory_chain", race_parent)
    with pytest.raises(BackupError, match="identity"):
        create_sqlite_backup(source, destination, 3, NOW)
    assert not destination.exists()


@pytest.mark.parametrize("mutation", ["replace", "remove"])
def test_backup_verifies_the_owned_destination_not_a_substitute(tmp_path, mutation):
    source = tmp_path / "source.sqlite3"
    make_source(source, "intended-source").close()
    substitute = tmp_path / "substitute.sqlite3"
    make_source(substitute, "unchanged-substitute").close()
    destination = tmp_path / "backup.sqlite3"

    def substitute_after_copy(path):
        path.unlink()
        if mutation == "replace":
            path.symlink_to(substitute)

    with pytest.raises(BackupError, match="identity"):
        create_sqlite_backup(
            source, destination, 3, NOW, after_copy=substitute_after_copy
        )

    with sqlite3.connect(substitute) as connection:
        assert connection.execute(
            "SELECT content FROM synthetic_private WHERE event_id='event-001'"
        ).fetchone() == ("unchanged-substitute",)
    if mutation == "replace":
        assert destination.is_symlink()
    else:
        assert not destination.exists()


def test_backup_verifies_owned_metadata_before_success(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    destination = tmp_path / "backup.sqlite3"
    metadata = Path(f"{destination}.metadata.json")
    real_fsync = os.fsync

    def replace_metadata(descriptor):
        real_fsync(descriptor)
        metadata.unlink()
        metadata.write_text("concurrent-metadata")

    monkeypatch.setattr(recovery_module.os, "fsync", replace_metadata)
    with pytest.raises(BackupError, match="identity"):
        create_sqlite_backup(source, destination, 3, NOW)

    assert metadata.read_text() == "concurrent-metadata"
    assert not destination.exists()


def test_backup_and_readiness_marker_retry_short_writes(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    real_write = os.write
    writes = []

    def short_write(descriptor, payload):
        count = max(1, len(payload) // 2)
        writes.append(count)
        return real_write(descriptor, payload[:count])

    monkeypatch.setattr(recovery_module.os, "write", short_write)
    metadata = create_sqlite_backup(source, backup, 3, NOW)
    assert json.loads(metadata.path.read_text())["database_filename"] == backup.name

    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    quarantine = tmp_path / "quarantine.sqlite3"
    result = QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
        backup, quarantine, NOW
    )
    assert result.activation_eligible is True
    assert json.loads(Path(f"{quarantine}.ready.json").read_text())["restore_id"] == (
        result.restore_id
    )
    assert len(writes) > 2


def test_backup_restore_and_readiness_fsync_parent_directories(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    real_fsync = os.fsync
    directory_syncs = []

    def track_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_syncs.append(os.fstat(descriptor).st_ino)
        real_fsync(descriptor)

    monkeypatch.setattr(recovery_module.os, "fsync", track_fsync)
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    result = QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
        backup, tmp_path / "quarantine.sqlite3", NOW
    )

    assert result.activation_eligible is True
    assert len(directory_syncs) >= 4


def test_zero_length_writes_fail_closed_and_remove_partial_outputs(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    failed_backup = tmp_path / "failed.sqlite3"
    monkeypatch.setattr(recovery_module.os, "write", lambda _descriptor, _data: 0)
    with pytest.raises(BackupError, match="backup failed"):
        create_sqlite_backup(source, failed_backup, 3, NOW)
    assert not failed_backup.exists()
    assert not Path(f"{failed_backup}.metadata.json").exists()
    assert not Path(f"{failed_backup}-wal").exists()
    assert not Path(f"{failed_backup}-shm").exists()

    monkeypatch.undo()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    quarantine = tmp_path / "quarantine.sqlite3"
    monkeypatch.setattr(recovery_module.os, "write", lambda _descriptor, _data: 0)
    with pytest.raises(BackupError, match="readiness marker write failed"):
        QuarantinedRestore(ledger, 3, RestoreAdapter).restore(backup, quarantine, NOW)
    assert not quarantine.exists()
    assert not Path(f"{quarantine}.ready.json").exists()


def test_restore_verifies_owned_readiness_marker_before_success(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    quarantine = tmp_path / "quarantine.sqlite3"
    marker = Path(f"{quarantine}.ready.json")
    real_fsync = os.fsync

    def replace_marker(descriptor):
        real_fsync(descriptor)
        if marker.exists() and not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            marker.unlink()
            marker.write_text("concurrent-marker")

    monkeypatch.setattr(recovery_module.os, "fsync", replace_marker)
    with pytest.raises(BackupError, match="readiness marker write failed"):
        QuarantinedRestore(ledger, 3, RestoreAdapter).restore(backup, quarantine, NOW)
    assert marker.read_text() == "concurrent-marker"


def test_readiness_failure_preserves_a_replaced_quarantine(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    quarantine = tmp_path / "quarantine.sqlite3"

    def replace_quarantine_and_fail(_descriptor, _payload):
        quarantine.unlink()
        quarantine.write_text("concurrent-quarantine")
        raise OSError("marker write interrupted")

    monkeypatch.setattr(recovery_module, "_write_all", replace_quarantine_and_fail)

    with pytest.raises(BackupError, match="readiness marker write failed"):
        QuarantinedRestore(ledger, 3, RestoreAdapter).restore(backup, quarantine, NOW)

    assert quarantine.read_text() == "concurrent-quarantine"
    assert not Path(f"{quarantine}.ready.json").exists()


@pytest.mark.parametrize("mutation", ["seal", "sidecar", "generation"])
def test_readiness_revalidates_every_bound_input(tmp_path, monkeypatch, mutation):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    quarantine = tmp_path / "quarantine.sqlite3"
    marker = Path(f"{quarantine}.ready.json")
    sidecar = Path(f"{quarantine}-wal")
    real_fsync = os.fsync
    mutated = False

    def mutate_after_marker_write(descriptor):
        nonlocal mutated
        real_fsync(descriptor)
        if mutated or not marker.exists():
            return
        marker_identity = marker.stat(follow_symlinks=False)
        descriptor_identity = os.fstat(descriptor)
        if (marker_identity.st_dev, marker_identity.st_ino) != (
            descriptor_identity.st_dev,
            descriptor_identity.st_ino,
        ):
            return
        mutated = True
        if mutation == "seal":
            quarantine.chmod(0o600)
            with quarantine.open("ab") as database:
                database.write(b"changed")
        elif mutation == "sidecar":
            sidecar.write_text("concurrent-sidecar")
        else:
            ledger.insert_deletion(
                context(),
                DeletionRequest(
                    "delete-before-readiness",
                    "participant-a",
                    "event-before-readiness",
                    ContentCategory.PRIVATE_PAYLOAD,
                    NOW,
                    "actor-a",
                    "delete-before-readiness-correlation",
                    "auth-a",
                ),
                DeletionDisposition.PENDING,
                "awaiting_erasure",
            )

    monkeypatch.setattr(recovery_module.os, "fsync", mutate_after_marker_write)
    with pytest.raises(BackupError, match="readiness marker write failed"):
        QuarantinedRestore(ledger, 3, RestoreAdapter).restore(backup, quarantine, NOW)

    assert mutated is True
    assert not marker.exists()
    with sqlite3.connect(ledger.path) as connection:
        assert (
            connection.execute(
                "SELECT disposition, reason FROM restore_outcomes"
            ).fetchall()
            == []
        )
    if mutation == "sidecar":
        assert sidecar.read_text() == "concurrent-sidecar"


def test_restore_generation_check_is_atomic_with_eligibility_record(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    quarantine = tmp_path / "quarantine.sqlite3"
    real_record_restore = ledger._record_restore
    generation_advanced = False

    def advance_before_record(*args, **kwargs):
        nonlocal generation_advanced
        if args[2] is RestoreDisposition.ELIGIBLE_FOR_ACTIVATION:
            assert generation_advanced is False
            generation_advanced = True
            ledger.insert_deletion(
                context(),
                DeletionRequest(
                    "delete-at-eligibility",
                    "participant-a",
                    "event-at-eligibility",
                    ContentCategory.PRIVATE_PAYLOAD,
                    NOW,
                    "actor-a",
                    "delete-at-eligibility-correlation",
                    "auth-a",
                ),
                DeletionDisposition.PENDING,
                "awaiting_erasure",
            )
        return real_record_restore(*args, **kwargs)

    monkeypatch.setattr(ledger, "_record_restore", advance_before_record)
    result = QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
        backup, quarantine, NOW
    )

    assert generation_advanced is True
    assert result.disposition is RestoreDisposition.BLOCKED
    assert result.reason == "privacy_ledger_changed_during_restore"
    assert not Path(f"{quarantine}.ready.json").exists()
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute(
            "SELECT disposition, reason FROM restore_outcomes"
        ).fetchall() == [("blocked", "privacy_ledger_changed_during_restore")]


def test_readiness_marker_interrupt_cleans_marker_and_descriptors(
    tmp_path, monkeypatch
):
    class SimulatedInterrupt(BaseException):
        pass

    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    quarantine = tmp_path / "quarantine.sqlite3"
    marker = Path(f"{quarantine}.ready.json")
    opened = []
    closed = []
    real_open_owned_file = recovery_module._open_owned_file
    real_close = os.close

    def track_open(path, access, owned):
        descriptor = real_open_owned_file(path, access, owned)
        opened.append(owned[-1])
        return descriptor

    def track_close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(recovery_module, "_open_owned_file", track_open)
    monkeypatch.setattr(recovery_module.os, "close", track_close)
    monkeypatch.setattr(
        recovery_module,
        "_write_all",
        lambda *_args: (_ for _ in ()).throw(SimulatedInterrupt()),
    )

    with pytest.raises(SimulatedInterrupt):
        QuarantinedRestore(ledger, 3, RestoreAdapter).restore(backup, quarantine, NOW)

    assert not marker.exists()
    for owned_file in opened:
        assert owned_file.descriptor in closed
        assert owned_file.parent_descriptor in closed


def test_readiness_close_interrupt_preserves_committed_artifacts(tmp_path, monkeypatch):
    class SimulatedInterrupt(BaseException):
        pass

    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    quarantine = tmp_path / "quarantine.sqlite3"
    marker = Path(f"{quarantine}.ready.json")
    real_close_owned = recovery_module._close_owned
    interrupted = False

    def interrupt_after_marker_close(owned):
        nonlocal interrupted
        real_close_owned(owned)
        if owned and owned[0].path == marker:
            interrupted = True
            raise SimulatedInterrupt()

    monkeypatch.setattr(recovery_module, "_close_owned", interrupt_after_marker_close)

    with pytest.raises(SimulatedInterrupt):
        QuarantinedRestore(ledger, 3, RestoreAdapter).restore(backup, quarantine, NOW)

    assert interrupted is True
    assert quarantine.exists()
    assert marker.exists()
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute(
            "SELECT disposition, reason FROM restore_outcomes"
        ).fetchall() == [("eligible_for_activation", "reconciliation_complete")]


def test_backup_rejects_malformed_database_and_verification_faults(
    tmp_path, monkeypatch
):
    missing_schema = tmp_path / "missing-schema.sqlite3"
    sqlite3.connect(missing_schema).close()
    with pytest.raises(BackupError, match="schema"):
        create_sqlite_backup(missing_schema, tmp_path / "missing-out.sqlite3", 3, NOW)

    malformed_schema = tmp_path / "malformed-schema.sqlite3"
    with sqlite3.connect(malformed_schema) as connection:
        connection.execute(
            "CREATE TABLE schema_metadata(singleton INTEGER PRIMARY KEY, version TEXT)"
        )
        connection.execute("INSERT INTO schema_metadata VALUES(1, 'three')")
    with pytest.raises(BackupError, match="schema"):
        create_sqlite_backup(
            malformed_schema, tmp_path / "malformed-out.sqlite3", 3, NOW
        )

    empty_schema = tmp_path / "empty-schema.sqlite3"
    with sqlite3.connect(empty_schema) as connection:
        connection.execute(
            "CREATE TABLE schema_metadata(singleton INTEGER PRIMARY KEY, version INTEGER)"
        )
        connection.execute("PRAGMA user_version=3")
    with pytest.raises(BackupError, match="schema"):
        create_sqlite_backup(empty_schema, tmp_path / "empty-out.sqlite3", 3, NOW)

    mismatched_schema = tmp_path / "mismatched-schema.sqlite3"
    make_source(mismatched_schema).close()
    with sqlite3.connect(mismatched_schema) as connection:
        connection.execute("PRAGMA user_version=4")
    with pytest.raises(BackupError, match="schema"):
        create_sqlite_backup(
            mismatched_schema, tmp_path / "mismatched-out.sqlite3", 3, NOW
        )

    source = tmp_path / "source.sqlite3"
    make_source(source).close()

    def alter_schema(destination):
        with sqlite3.connect(destination) as connection:
            connection.execute("UPDATE schema_metadata SET version=4")
            connection.execute("PRAGMA user_version=4")

    with pytest.raises(BackupError, match="schema"):
        create_sqlite_backup(
            source, tmp_path / "changed.sqlite3", 3, NOW, after_copy=alter_schema
        )

    monkeypatch.setattr(
        recovery_module, "_integrity_ok", lambda _path, **_kwargs: False
    )
    with pytest.raises(BackupError, match="consistency"):
        create_sqlite_backup(source, tmp_path / "bad-integrity.sqlite3", 3, NOW)


def test_backup_rejects_an_unfinalized_wal_snapshot():
    class WalConnection:
        def execute(self, statement):
            assert statement == "PRAGMA journal_mode=DELETE"
            return self

        def fetchone(self):
            return ("wal",)

    with pytest.raises(sqlite3.OperationalError, match="finalize"):
        recovery_module._use_rollback_journal(WalConnection())


@pytest.mark.parametrize("sqlite_code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_backup_classifies_busy_without_leaking_sqlite_details(
    tmp_path, monkeypatch, sqlite_code
):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()

    class BusyConnection:
        def execute(self, statement):
            assert statement == "PRAGMA database_list"
            return self

        def fetchone(self):
            return (0, "main", str(source))

        def backup(self, _destination, *, pages, progress, sleep):
            assert pages == recovery_module.BACKUP_PAGE_BATCH
            assert sleep == recovery_module.BACKUP_BUSY_SLEEP_SECONDS
            for _ in range(recovery_module.MAX_BACKUP_BUSY_ATTEMPTS):
                progress(sqlite_code, 1, 1)

        def close(self):
            pass

    real_connect = sqlite3.connect

    def connect(target, *args, **kwargs):
        if "mode=ro" in str(target):
            return BusyConnection()
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr("app.privacy.recovery.sqlite3.connect", connect)
    with pytest.raises(BackupBusyError, match="busy") as caught:
        create_sqlite_backup(source, tmp_path / "busy.sqlite3", 3, NOW)
    assert "private-canary" not in str(caught.value)

    def io_failure(_source, _destination):
        error = sqlite3.OperationalError("I/O private-canary")
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        raise error

    if sqlite_code == sqlite3.SQLITE_BUSY:
        monkeypatch.setattr(recovery_module, "_sqlite_copy", io_failure)
        monkeypatch.setattr(recovery_module.sqlite3, "connect", real_connect)
        with pytest.raises(BackupError, match="backup failed") as nonbusy:
            create_sqlite_backup(source, tmp_path / "io-error.sqlite3", 3, NOW)
        assert "private-canary" not in str(nonbusy.value)


def test_old_snapshot_is_reconciled_in_quarantine_before_eligibility(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "old.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)

    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    pending_deletion(ledger)
    consent = ConsentService(ledger)
    consent.grant(
        context(),
        ConsentGrant(
            "consent-001",
            "grant-operation-001",
            "participant-a",
            "event-001",
            "public-threads",
            ConsentPurpose.IDENTIFIABLE_QUOTATION,
            NOW,
            NOW + timedelta(hours=1),
            "actor-a",
            1,
            "grant-correlation",
            "auth-a",
        ),
    )
    consent.revoke(
        context(),
        consent_id="consent-001",
        operation_id="revoke-operation-001",
        revoked_at=NOW + timedelta(minutes=11),
        correlation_id="revoke-correlation",
    )

    quarantine = tmp_path / "quarantine.sqlite3"
    adapters = []

    def adapter_factory(path):
        adapter = RestoreAdapter(path)
        adapters.append(adapter)
        return adapter

    result = QuarantinedRestore(ledger, 3, adapter_factory).restore(
        backup, quarantine, NOW + timedelta(minutes=20)
    )
    assert result.disposition is RestoreDisposition.ELIGIBLE_FOR_ACTIVATION
    assert result.activation_eligible is True
    assert result.ledger_generation == ledger.control_generation()
    assert not Path(f"{quarantine}-wal").exists()
    assert not Path(f"{quarantine}-shm").exists()
    with sqlite3.connect(quarantine) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM synthetic_private").fetchone()[0]
            == 0
        )
    assert adapters[0].revocations[0][0] == "consent-001"
    assert ledger.deletion("delete-operation-001").disposition.value == "pending"
    marker = json.loads(Path(f"{quarantine}.ready.json").read_text())
    assert marker["restore_id"] == result.restore_id
    assert marker["ledger_generation"] == result.ledger_generation
    quarantine_identity = quarantine.stat()
    assert stat.S_IMODE(quarantine_identity.st_mode) == 0o400
    assert stat.S_IMODE(Path(f"{quarantine}.ready.json").stat().st_mode) == 0o400
    assert marker["database_device"] == quarantine_identity.st_dev
    assert marker["database_inode"] == quarantine_identity.st_ino
    assert marker["database_size"] == quarantine_identity.st_size
    assert marker["database_mode"] == "0400"
    assert (
        marker["database_sha256"] == hashlib.sha256(quarantine.read_bytes()).hexdigest()
    )

    ledger.insert_deletion(
        context(),
        DeletionRequest(
            "delete-operation-after-readiness",
            "participant-a",
            "event-after-readiness",
            ContentCategory.PRIVATE_PAYLOAD,
            NOW + timedelta(minutes=21),
            "actor-a",
            "delete-after-readiness",
            "auth-a",
        ),
        DeletionDisposition.PENDING,
        "awaiting_erasure",
    )
    assert result.ledger_generation != ledger.control_generation()


def test_restore_seals_away_from_a_long_lived_wal_adapter(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    pending_deletion(ledger)
    adapters = []

    class LongLivedWalAdapter(RestoreAdapter):
        def __init__(self, path):
            super().__init__(path)
            self.connection = sqlite3.connect(path)
            assert self.connection.execute("PRAGMA journal_mode=WAL").fetchone() == (
                "wal",
            )

        def apply_deletion(self, record):
            self.connection.execute(
                "DELETE FROM synthetic_private WHERE event_id=?",
                (record.content_reference,),
            )
            self.connection.commit()
            return ErasureReport(
                True,
                True,
                frozenset(ContentCategory.required_erasure_categories()),
                (),
            )

    def adapter_factory(path):
        adapter = LongLivedWalAdapter(path)
        adapters.append(adapter)
        return adapter

    quarantine = tmp_path / "quarantine.sqlite3"
    result = QuarantinedRestore(ledger, 3, adapter_factory).restore(
        backup, quarantine, NOW
    )

    assert (result.disposition, result.reason) == (
        RestoreDisposition.ELIGIBLE_FOR_ACTIVATION,
        "reconciliation_complete",
    )
    assert quarantine.exists()
    assert list(tmp_path.glob(".privacy-restore-*.sqlite3-wal"))
    assert list(tmp_path.glob(".privacy-restore-*.sqlite3-shm"))
    assert Path(f"{quarantine}.ready.json").exists()
    with sqlite3.connect(quarantine) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM synthetic_private WHERE event_id='event-001'"
        ).fetchone() == (0,)
    adapters[0].connection.close()


def test_restore_releases_wal_writers_after_incomplete_reconciliation(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    pending_deletion(ledger)
    adapters = []

    class IncompleteWalAdapter(RestoreAdapter):
        def __init__(self, path):
            super().__init__(path)
            self.connection = sqlite3.connect(path)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.reader = sqlite3.connect(path)
            self.reader.execute("BEGIN")
            self.reader.execute("SELECT * FROM synthetic_private").fetchall()
            self.released = False

        def apply_deletion(self, record):
            self.connection.execute(
                "DELETE FROM synthetic_private WHERE event_id=?",
                (record.content_reference,),
            )
            self.connection.commit()
            return ErasureReport(False, False, frozenset(), ("incomplete",))

        def release_writers(self):
            self.connection.close()
            self.released = True
            return True

    def adapter_factory(path):
        adapter = IncompleteWalAdapter(path)
        adapters.append(adapter)
        return adapter

    quarantine = tmp_path / "quarantine.sqlite3"
    result = QuarantinedRestore(ledger, 3, adapter_factory).restore(
        backup, quarantine, NOW
    )

    assert (result.disposition, result.reason) == (
        RestoreDisposition.BLOCKED,
        "deletion_reconciliation_incomplete",
    )
    assert adapters[0].released is True
    assert not list(tmp_path.glob(".privacy-restore-*.sqlite3-wal"))
    assert not list(tmp_path.glob(".privacy-restore-*.sqlite3-shm"))
    assert not quarantine.exists()
    assert not Path(f"{quarantine}.ready.json").exists()
    retried = QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
        backup, quarantine, NOW
    )
    assert retried.activation_eligible is True
    adapters[0].reader.close()


def test_restore_rejects_a_backup_with_committed_wal_state(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    quarantine = tmp_path / "quarantine.sqlite3"
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    writer = sqlite3.connect(backup)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("DELETE FROM synthetic_private")
        writer.commit()
        assert writer.execute("SELECT COUNT(*) FROM synthetic_private").fetchone() == (
            0,
        )
        assert Path(f"{backup}-wal").exists()

        with pytest.raises(BackupError, match="sidecar"):
            QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
                backup, quarantine, NOW
            )
    finally:
        writer.close()

    assert not quarantine.exists()
    assert not Path(f"{quarantine}.ready.json").exists()


def test_restore_rejects_a_backup_with_a_rollback_journal(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    journal = Path(f"{backup}-journal")
    journal.write_bytes(b"unrecovered rollback journal")
    quarantine = tmp_path / "quarantine.sqlite3"
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")

    with pytest.raises(BackupError, match="sidecar"):
        QuarantinedRestore(ledger, 3, RestoreAdapter).restore(backup, quarantine, NOW)

    assert journal.read_bytes() == b"unrecovered rollback journal"
    assert not quarantine.exists()
    assert not Path(f"{quarantine}.ready.json").exists()


def test_restore_seals_an_inode_never_exposed_to_a_retained_adapter(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    pending_deletion(ledger)
    adapters = []

    class RetainedRollbackAdapter(RestoreAdapter):
        def __init__(self, path):
            super().__init__(path)
            self.connection = sqlite3.connect(path)
            assert self.connection.execute("PRAGMA journal_mode").fetchone() == (
                "delete",
            )

        def apply_deletion(self, record):
            self.connection.execute(
                "DELETE FROM synthetic_private WHERE event_id=?",
                (record.content_reference,),
            )
            self.connection.commit()
            return ErasureReport(
                True,
                True,
                frozenset(ContentCategory.required_erasure_categories()),
                (),
            )

    def adapter_factory(path):
        adapter = RetainedRollbackAdapter(path)
        adapters.append(adapter)
        return adapter

    real_descriptor_sha256 = recovery_module._descriptor_sha256
    retained_write_attempted = False

    def write_through_retained_connection(descriptor):
        nonlocal retained_write_attempted
        if adapters and not retained_write_attempted:
            retained_write_attempted = True
            try:
                adapters[0].connection.execute(
                    "INSERT INTO synthetic_private VALUES"
                    "('event-001', 'retained-write')"
                )
                adapters[0].connection.commit()
            except sqlite3.Error:
                adapters[0].connection.rollback()
        return real_descriptor_sha256(descriptor)

    monkeypatch.setattr(
        recovery_module, "_descriptor_sha256", write_through_retained_connection
    )
    quarantine = tmp_path / "quarantine.sqlite3"
    try:
        result = QuarantinedRestore(ledger, 3, adapter_factory).restore(
            backup, quarantine, NOW
        )
        assert result.activation_eligible is True
    finally:
        adapters[0].connection.close()

    assert retained_write_attempted is True
    with sqlite3.connect(quarantine) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM synthetic_private WHERE event_id='event-001'"
        ).fetchone() == (0,)
    marker = json.loads(Path(f"{quarantine}.ready.json").read_text())
    assert (
        marker["database_sha256"] == hashlib.sha256(quarantine.read_bytes()).hexdigest()
    )


def test_restore_has_no_write_gap_before_the_exclusive_seal(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    pending_deletion(ledger)
    adapters = []

    class RetainedRollbackAdapter(RestoreAdapter):
        def __init__(self, path):
            super().__init__(path)
            self.connection = sqlite3.connect(path, timeout=0.01)

        def apply_deletion(self, record):
            self.connection.execute(
                "DELETE FROM synthetic_private WHERE event_id=?",
                (record.content_reference,),
            )
            self.connection.commit()
            return ErasureReport(
                True,
                True,
                frozenset(ContentCategory.required_erasure_categories()),
                (),
            )

        def release_writers(self):
            return True

    def adapter_factory(path):
        adapter = RetainedRollbackAdapter(path)
        adapters.append(adapter)
        return adapter

    real_require_identity = recovery_module._require_database_identity
    retained_write_blocked = False

    def attempt_write_after_fence(database):
        nonlocal retained_write_blocked
        if adapters and not retained_write_blocked:
            try:
                adapters[0].connection.execute(
                    "INSERT INTO synthetic_private VALUES('event-001', 'late write')"
                )
                adapters[0].connection.commit()
            except sqlite3.Error:
                retained_write_blocked = True
                adapters[0].connection.rollback()
        return real_require_identity(database)

    monkeypatch.setattr(
        recovery_module,
        "_require_database_identity",
        attempt_write_after_fence,
    )
    quarantine = tmp_path / "quarantine.sqlite3"
    try:
        result = QuarantinedRestore(ledger, 3, adapter_factory).restore(
            backup, quarantine, NOW
        )
    finally:
        adapters[0].connection.close()

    assert result.activation_eligible is True
    assert retained_write_blocked is True
    with sqlite3.connect(quarantine) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM synthetic_private WHERE event_id='event-001'"
        ).fetchone() == (0,)


def test_restore_rejects_a_sidecar_created_during_finalization(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    quarantine = tmp_path / "quarantine.sqlite3"
    real_use_rollback_journal = recovery_module._use_rollback_journal
    calls = 0

    def create_sidecar(connection):
        nonlocal calls
        calls += 1
        real_use_rollback_journal(connection)
        if calls == 2:
            database_path = Path(
                connection.execute("PRAGMA database_list").fetchone()[2]
            ).resolve()
            Path(f"{database_path}-wal").write_text("concurrent-sidecar")

    monkeypatch.setattr(recovery_module, "_use_rollback_journal", create_sidecar)
    result = QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
        backup, quarantine, NOW
    )

    assert (result.disposition, result.reason) == (
        RestoreDisposition.BLOCKED,
        "quarantine_finalization_failed",
    )
    assert not list(tmp_path.glob(".privacy-restore-*.sqlite3*"))
    assert not quarantine.exists()
    assert not Path(f"{quarantine}.ready.json").exists()
    retried = QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
        backup, quarantine, NOW
    )
    assert retried.activation_eligible is True


def test_fresh_snapshot_seal_rejects_a_fence_connection_failure(tmp_path, monkeypatch):
    source = tmp_path / "snapshot.sqlite3"
    make_source(source).close()
    descriptor = os.open(source, os.O_RDWR)
    monkeypatch.setattr(
        recovery_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("fence unavailable")
        ),
    )
    try:
        assert (
            recovery_module._seal_fresh_snapshot(
                source, descriptor, tmp_path / "sealed.sqlite3"
            )
            is None
        )
    finally:
        os.close(descriptor)


def test_fresh_snapshot_seal_cleans_an_identity_mismatch(tmp_path, monkeypatch):
    source = tmp_path / "snapshot.sqlite3"
    make_source(source).close()
    descriptor = os.open(source, os.O_RDWR)
    sealed = tmp_path / "sealed.sqlite3"
    real_matches = recovery_module._path_matches_descriptor

    def reject_sealed(path, candidate_descriptor):
        if path == sealed:
            return False
        return real_matches(path, candidate_descriptor)

    monkeypatch.setattr(recovery_module, "_path_matches_descriptor", reject_sealed)
    try:
        assert recovery_module._seal_fresh_snapshot(source, descriptor, sealed) is None
    finally:
        os.close(descriptor)

    assert not sealed.exists()


def test_fresh_snapshot_seal_cleans_an_interruption(tmp_path, monkeypatch):
    class SimulatedInterrupt(BaseException):
        pass

    source = tmp_path / "snapshot.sqlite3"
    make_source(source).close()
    descriptor = os.open(source, os.O_RDWR)
    sealed = tmp_path / "sealed.sqlite3"
    monkeypatch.setattr(
        recovery_module,
        "_use_rollback_journal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SimulatedInterrupt()),
    )
    try:
        with pytest.raises(SimulatedInterrupt):
            recovery_module._seal_fresh_snapshot(source, descriptor, sealed)
    finally:
        os.close(descriptor)

    assert not sealed.exists()


@pytest.mark.parametrize("failure", ["rollback", "close"])
def test_fresh_snapshot_seal_cleans_a_fence_teardown_failure(
    tmp_path, monkeypatch, failure
):
    class SimulatedInterrupt(BaseException):
        pass

    source = tmp_path / "snapshot.sqlite3"
    make_source(source).close()
    descriptor = os.open(source, os.O_RDWR)
    sealed = tmp_path / "sealed.sqlite3"
    real_connect = recovery_module.sqlite3.connect
    real_open_owned_file = recovery_module._open_owned_file
    fence_connections = []
    sealed_outputs = []

    class FenceConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def rollback(self):
            if failure == "rollback":
                raise SimulatedInterrupt()
            self.connection.rollback()

        def close(self):
            self.connection.close()
            if failure == "close":
                raise SimulatedInterrupt()

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        if not fence_connections:
            fence_connections.append(connection)
            return FenceConnection(connection)
        return connection

    def track_sealed_output(path, access, owned):
        output_descriptor = real_open_owned_file(path, access, owned)
        if path == sealed:
            sealed_outputs.append(owned[-1])
        return output_descriptor

    monkeypatch.setattr(recovery_module.sqlite3, "connect", connect)
    monkeypatch.setattr(recovery_module, "_open_owned_file", track_sealed_output)
    try:
        with pytest.raises(SimulatedInterrupt):
            recovery_module._seal_fresh_snapshot(source, descriptor, sealed)
    finally:
        os.close(descriptor)

    assert not sealed.exists()
    assert len(sealed_outputs) == 1
    with pytest.raises(OSError):
        os.fstat(sealed_outputs[0].descriptor)
    with pytest.raises(OSError):
        os.fstat(sealed_outputs[0].parent_descriptor)
    with pytest.raises(sqlite3.ProgrammingError):
        fence_connections[0].execute("SELECT 1")


def test_restore_tracks_sealed_inode_before_releasing_adapter_staging(
    tmp_path, monkeypatch
):
    class SimulatedInterrupt(BaseException):
        pass

    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    quarantine = tmp_path / "quarantine.sqlite3"
    real_cleanup = recovery_module._cleanup_created
    interrupted = False

    def interrupt_adapter_staging_cleanup(outputs):
        nonlocal interrupted
        if (
            not interrupted
            and len(outputs) == 1
            and outputs[0].path.name.startswith(".privacy-restore-")
            and list(tmp_path.glob(".privacy-sealed-*.sqlite3"))
        ):
            interrupted = True
            raise SimulatedInterrupt()
        real_cleanup(outputs)

    monkeypatch.setattr(
        recovery_module, "_cleanup_created", interrupt_adapter_staging_cleanup
    )

    with pytest.raises(SimulatedInterrupt):
        QuarantinedRestore(ledger, 3, RestoreAdapter).restore(backup, quarantine, NOW)

    assert interrupted is True
    assert not quarantine.exists()
    assert not list(tmp_path.glob(".privacy-sealed-*.sqlite3"))


def test_restore_never_blesses_a_public_path_created_before_publication(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    quarantine = tmp_path / "quarantine.sqlite3"
    real_seal = recovery_module._seal_fresh_snapshot

    def create_public_path_before_publish(staging, descriptor, destination):
        assert not quarantine.exists()
        sealed = real_seal(staging, descriptor, destination)
        quarantine.write_text("concurrent-public-file")
        return sealed

    monkeypatch.setattr(
        recovery_module,
        "_seal_fresh_snapshot",
        create_public_path_before_publish,
    )
    with pytest.raises(BackupError, match="publication"):
        QuarantinedRestore(ledger, 3, RestoreAdapter).restore(backup, quarantine, NOW)

    assert quarantine.read_text() == "concurrent-public-file"
    assert not Path(f"{quarantine}.ready.json").exists()
    assert not list(tmp_path.glob(".privacy-restore-*.sqlite3"))


def test_restore_blocks_when_privacy_ledger_changes_during_reconciliation(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")

    class RacingAdapter(RestoreAdapter):
        def rebuild_dependencies(self):
            ledger.insert_deletion(
                context(),
                DeletionRequest(
                    "delete-operation-race",
                    "participant-a",
                    "event-race",
                    ContentCategory.PRIVATE_PAYLOAD,
                    NOW + timedelta(minutes=1),
                    "actor-a",
                    "delete-race-correlation",
                    "auth-a",
                ),
                DeletionDisposition.PENDING,
                "awaiting_erasure",
            )
            return True

    quarantine = tmp_path / "quarantine-race.sqlite3"
    result = QuarantinedRestore(ledger, 3, RacingAdapter).restore(
        backup, quarantine, NOW
    )

    assert (result.disposition, result.reason) == (
        RestoreDisposition.BLOCKED,
        "privacy_ledger_changed_during_restore",
    )
    assert not Path(f"{quarantine}.ready.json").exists()


def test_restore_fails_closed_for_adapter_or_metadata_problem(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "old.sqlite3"
    metadata = create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    pending_deletion(ledger)

    blocked_path = tmp_path / "blocked.sqlite3"
    blocked = QuarantinedRestore(
        ledger, 3, lambda path: RestoreAdapter(path, fail=True)
    ).restore(backup, blocked_path, NOW + timedelta(minutes=20))
    assert blocked.disposition is RestoreDisposition.BLOCKED
    assert blocked.activation_eligible is False
    assert not Path(f"{blocked_path}.ready.json").exists()

    valid_metadata = metadata.path.read_bytes()
    metadata.path.write_bytes(b"x" * (recovery_module.MAX_BACKUP_METADATA_BYTES + 1))
    with pytest.raises(BackupError, match="metadata"):
        QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
            backup, tmp_path / "oversized-metadata.sqlite3", NOW
        )

    metadata.path.write_bytes(valid_metadata)
    metadata.path.write_text("{}")
    with pytest.raises(BackupError, match="metadata"):
        QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
            backup, tmp_path / "bad-metadata.sqlite3", NOW
        )


def test_restore_metadata_read_rejects_a_replaced_path(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    metadata = create_sqlite_backup(source, backup, 3, NOW)
    replacement = tmp_path / "replacement.metadata.json"
    replacement.write_bytes(metadata.path.read_bytes())
    held_metadata = tmp_path / "held.metadata.json"
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    real_read = os.read
    replaced = False

    def replace_before_read(descriptor, size):
        nonlocal replaced
        if not replaced:
            replaced = True
            metadata.path.rename(held_metadata)
            replacement.rename(metadata.path)
        return real_read(descriptor, size)

    monkeypatch.setattr(recovery_module.os, "read", replace_before_read)
    quarantine = tmp_path / "quarantine.sqlite3"
    with pytest.raises(BackupError, match="metadata"):
        QuarantinedRestore(ledger, 3, RestoreAdapter).restore(backup, quarantine, NOW)
    assert not quarantine.exists()


def test_restore_does_not_publish_readiness_when_outcome_recording_fails(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    quarantine = tmp_path / "quarantine.sqlite3"

    def fail_record(*_args, **_kwargs):
        raise RuntimeError("restore outcome unavailable")

    monkeypatch.setattr(ledger, "_record_restore", fail_record)
    with pytest.raises(RuntimeError, match="outcome unavailable"):
        QuarantinedRestore(ledger, 3, RestoreAdapter).restore(backup, quarantine, NOW)
    assert not Path(f"{quarantine}.ready.json").exists()


@pytest.mark.parametrize("integrity_outcome", [False, sqlite3.DatabaseError("corrupt")])
def test_restore_checks_quarantine_integrity_before_adapter_or_readiness(
    tmp_path, monkeypatch, integrity_outcome
):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    adapter_called = False

    def adapter_factory(path):
        nonlocal adapter_called
        adapter_called = True
        return RestoreAdapter(path)

    def integrity(_path, **_kwargs):
        if isinstance(integrity_outcome, Exception):
            raise integrity_outcome
        return integrity_outcome

    monkeypatch.setattr(recovery_module, "_integrity_ok", integrity)
    quarantine = tmp_path / "quarantine.sqlite3"
    result = QuarantinedRestore(ledger, 3, adapter_factory).restore(
        backup, quarantine, NOW
    )

    assert (result.disposition, result.reason) == (
        RestoreDisposition.BLOCKED,
        "quarantine_integrity_failed",
    )
    assert adapter_called is False
    assert not Path(f"{quarantine}.ready.json").exists()


@pytest.mark.parametrize(
    "post_integrity_outcome", [False, sqlite3.DatabaseError("post-write corrupt")]
)
def test_restore_rechecks_integrity_after_reconciliation(
    tmp_path, monkeypatch, post_integrity_outcome
):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    outcomes = iter((True, post_integrity_outcome))

    def integrity(_path, **_kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(recovery_module, "_integrity_ok", integrity)
    quarantine = tmp_path / "quarantine.sqlite3"
    result = QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
        backup, quarantine, NOW
    )

    assert (result.disposition, result.reason) == (
        RestoreDisposition.BLOCKED,
        "post_reconciliation_integrity_failed",
    )
    assert not Path(f"{quarantine}.ready.json").exists()


@pytest.mark.parametrize("schema_mutation", ["change", "drop", "pragma"])
def test_restore_rechecks_schema_after_reconciliation(tmp_path, schema_mutation):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")

    class SchemaMutatingAdapter(RestoreAdapter):
        def rebuild_dependencies(self):
            with sqlite3.connect(self.path) as connection:
                if schema_mutation == "change":
                    connection.execute("UPDATE schema_metadata SET version=4")
                    connection.execute("PRAGMA user_version=4")
                elif schema_mutation == "drop":
                    connection.execute("DROP TABLE schema_metadata")
                else:
                    connection.execute("PRAGMA user_version=4")
            return True

    quarantine = tmp_path / f"quarantine-{schema_mutation}.sqlite3"
    result = QuarantinedRestore(ledger, 3, SchemaMutatingAdapter).restore(
        backup, quarantine, NOW
    )

    assert (result.disposition, result.reason) == (
        RestoreDisposition.BLOCKED,
        "post_reconciliation_schema_invalid",
    )
    assert not Path(f"{quarantine}.ready.json").exists()


def test_restore_validates_the_pinned_backup_schema_before_adapters(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    with sqlite3.connect(backup) as connection:
        connection.execute("UPDATE schema_metadata SET version=4")
        connection.execute("PRAGMA user_version=4")

    real_schema_version = recovery_module._schema_version
    validated_targets = []

    def schema_version(target, **kwargs):
        validated_targets.append(target)
        if target == backup:
            return 3
        return real_schema_version(target, **kwargs)

    adapter_called = False

    def adapter_factory(path):
        nonlocal adapter_called
        adapter_called = True
        return RestoreAdapter(path)

    monkeypatch.setattr(recovery_module, "_schema_version", schema_version)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    with pytest.raises(BackupError, match="unsupported backup schema"):
        QuarantinedRestore(ledger, 3, adapter_factory).restore(
            backup, tmp_path / "quarantine.sqlite3", NOW
        )

    assert len(validated_targets) == 1
    assert type(validated_targets[0]) is int
    assert adapter_called is False


def test_restore_outcome_recording_is_not_public(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")

    with pytest.raises(AttributeError):
        ledger.record_restore(
            "restore-00000000-0000-4000-8000-000000000001",
            NOW,
            RestoreDisposition.ELIGIBLE_FOR_ACTIVATION,
            "reconciliation_complete",
            expected_generation=ledger.control_generation(),
        )

    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM restore_outcomes"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("restore_id", "occurred_at", "disposition", "reason", "message"),
    [
        (
            "private restore id",
            NOW,
            RestoreDisposition.BLOCKED,
            "quarantine_integrity_failed",
            "restore id",
        ),
        (
            "restore-not-a-uuid",
            NOW,
            RestoreDisposition.BLOCKED,
            "quarantine_integrity_failed",
            "restore id",
        ),
        (
            "00000000-0000-4000-8000-000000000001",
            NOW,
            RestoreDisposition.BLOCKED,
            "quarantine_integrity_failed",
            "restore id",
        ),
        (
            "restore-00000000-0000-4000-8000-000000000001",
            datetime(2026, 10, 10),
            RestoreDisposition.BLOCKED,
            "quarantine_integrity_failed",
            "restore timestamp",
        ),
        (
            "restore-00000000-0000-4000-8000-000000000001",
            NOW,
            "blocked",
            "quarantine_integrity_failed",
            "restore disposition",
        ),
        (
            "restore-00000000-0000-4000-8000-000000000001",
            NOW,
            RestoreDisposition.ELIGIBLE_FOR_ACTIVATION,
            "quarantine_integrity_failed",
            "restore reason",
        ),
        (
            "restore-00000000-0000-4000-8000-000000000001",
            NOW,
            RestoreDisposition.BLOCKED,
            "private restore reason",
            "restore reason",
        ),
    ],
)
def test_restore_outcomes_reject_untrusted_fields(
    tmp_path, restore_id, occurred_at, disposition, reason, message
):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")

    with pytest.raises(ValueError, match=message):
        ledger._record_restore(restore_id, occurred_at, disposition, reason)

    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM restore_outcomes"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("disposition", "reason", "expected_generation"),
    [
        (
            RestoreDisposition.ELIGIBLE_FOR_ACTIVATION,
            "reconciliation_complete",
            None,
        ),
        (
            RestoreDisposition.ELIGIBLE_FOR_ACTIVATION,
            "reconciliation_complete",
            True,
        ),
        (
            RestoreDisposition.ELIGIBLE_FOR_ACTIVATION,
            "reconciliation_complete",
            -1,
        ),
        (
            RestoreDisposition.BLOCKED,
            "quarantine_integrity_failed",
            0,
        ),
    ],
)
def test_restore_outcomes_reject_invalid_generation_guards(
    tmp_path, disposition, reason, expected_generation
):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")

    with pytest.raises(ValueError, match="restore generation"):
        ledger._record_restore(
            "restore-00000000-0000-4000-8000-000000000001",
            NOW,
            disposition,
            reason,
            expected_generation=expected_generation,
        )


def test_restore_eligibility_fails_when_control_generation_is_missing(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("DELETE FROM privacy_control_generation")

    with pytest.raises(RuntimeError, match="generation"):
        ledger._record_restore(
            "restore-00000000-0000-4000-8000-000000000001",
            NOW,
            RestoreDisposition.ELIGIBLE_FOR_ACTIVATION,
            "reconciliation_complete",
            expected_generation=0,
        )

    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM restore_outcomes"
        ).fetchone() == (0,)


def test_restore_schema_copy_and_reconciliation_failures_are_closed(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    metadata = create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    manager = QuarantinedRestore(ledger, 3, RestoreAdapter)

    raw = json.loads(metadata.path.read_text())
    raw["database_filename"] = "wrong.sqlite3"
    metadata.path.write_text(json.dumps(raw))
    with pytest.raises(BackupError, match="metadata"):
        manager.restore(backup, tmp_path / "wrong-name.sqlite3", NOW)

    raw["database_filename"] = backup.name
    metadata.path.write_text(json.dumps(raw))
    with sqlite3.connect(backup) as connection:
        connection.execute("UPDATE schema_metadata SET version=4")
        connection.execute("PRAGMA user_version=4")
    with pytest.raises(BackupError, match="schema"):
        manager.restore(backup, tmp_path / "wrong-schema.sqlite3", NOW)

    with sqlite3.connect(backup) as connection:
        connection.execute("UPDATE schema_metadata SET version=3")
        connection.execute("PRAGMA user_version=3")

    def copy_failure(_source, _destination_descriptor):
        raise sqlite3.OperationalError("private restore content")

    monkeypatch.setattr(recovery_module, "_sqlite_copy", copy_failure)
    failed_path = tmp_path / "copy-failed.sqlite3"
    with pytest.raises(BackupError, match="quarantine copy failed"):
        manager.restore(backup, failed_path, NOW)
    assert not failed_path.exists()


def test_restore_rejects_a_replaced_quarantine_before_reconciliation(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite3"
    make_source(source, "intended-backup").close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    substitute = tmp_path / "substitute.sqlite3"
    make_source(substitute, "substitute-quarantine").close()
    quarantine = tmp_path / "quarantine.sqlite3"
    held_quarantine = tmp_path / "held-quarantine.sqlite3"
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    real_copy = recovery_module._sqlite_copy

    def replace_after_copy(pinned_source, destination_descriptor):
        real_copy(pinned_source, destination_descriptor)
        staging = Path(os.readlink(f"/proc/self/fd/{destination_descriptor}"))
        staging.rename(held_quarantine)
        substitute.rename(staging)

    monkeypatch.setattr(recovery_module, "_sqlite_copy", replace_after_copy)
    result = QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
        backup, quarantine, NOW
    )

    assert (result.disposition, result.reason) == (
        RestoreDisposition.BLOCKED,
        "quarantine_identity_changed",
    )
    assert not quarantine.exists()
    assert not Path(f"{quarantine}.ready.json").exists()
    staging = next(tmp_path.glob(".privacy-restore-*.sqlite3"))
    with sqlite3.connect(staging) as connection:
        assert connection.execute(
            "SELECT content FROM synthetic_private WHERE event_id='event-001'"
        ).fetchone() == ("substitute-quarantine",)


def test_restore_adapter_uses_the_owned_quarantine_during_an_aba_swap(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source, "intended-backup").close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    substitute = tmp_path / "substitute.sqlite3"
    make_source(substitute, "substitute-quarantine").close()
    quarantine = tmp_path / "quarantine.sqlite3"
    held_quarantine = tmp_path / "held-quarantine.sqlite3"
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")

    class SwappingAdapter(RestoreAdapter):
        def rebuild_dependencies(self):
            staging = Path(os.readlink(self.path))
            staging.rename(held_quarantine)
            substitute.rename(staging)
            try:
                with sqlite3.connect(self.path) as connection:
                    content = connection.execute(
                        "SELECT content FROM synthetic_private "
                        "WHERE event_id='event-001'"
                    ).fetchone()
            finally:
                staging.rename(substitute)
                held_quarantine.rename(staging)
            return content == ("intended-backup",)

    result = QuarantinedRestore(ledger, 3, SwappingAdapter).restore(
        backup, quarantine, NOW
    )

    assert result.disposition is RestoreDisposition.ELIGIBLE_FOR_ACTIVATION
    with sqlite3.connect(substitute) as connection:
        assert connection.execute(
            "SELECT content FROM synthetic_private WHERE event_id='event-001'"
        ).fetchone() == ("substitute-quarantine",)


def test_restore_adapter_factory_failure_is_sanitized_and_recorded(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")

    def failed_factory(path):
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "INSERT INTO synthetic_private VALUES('event-init', 'private fault')"
            )
        raise RuntimeError("synthetic private adapter detail")

    quarantine = tmp_path / "quarantine.sqlite3"
    result = QuarantinedRestore(ledger, 3, failed_factory).restore(
        backup, quarantine, NOW
    )

    assert (result.disposition, result.reason) == (
        RestoreDisposition.BLOCKED,
        "adapter_initialization_failure",
    )
    assert "synthetic private adapter detail" not in repr(result)
    assert not quarantine.exists()
    assert not Path(f"{quarantine}.ready.json").exists()

    retried = QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
        backup, quarantine, NOW
    )
    assert retried.activation_eligible is True


def test_restore_adapter_factory_interruption_cleans_wal_staging(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    connections = []

    class SimulatedInterrupt(BaseException):
        pass

    def interrupted_factory(path):
        connection = sqlite3.connect(path)
        connections.append(connection)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "INSERT INTO synthetic_private VALUES('event-init-interrupt', 'private')"
        )
        connection.commit()
        raise SimulatedInterrupt()

    quarantine = tmp_path / "quarantine.sqlite3"
    with pytest.raises(SimulatedInterrupt):
        QuarantinedRestore(ledger, 3, interrupted_factory).restore(
            backup, quarantine, NOW
        )

    assert not quarantine.exists()
    assert not Path(f"{quarantine}.ready.json").exists()
    assert not list(tmp_path.glob(".privacy-restore-*.sqlite3*"))
    retried = QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
        backup, quarantine, NOW
    )
    assert retried.activation_eligible is True
    connections[0].close()


@pytest.mark.parametrize(
    ("behavior", "reason"),
    [
        ("false", "adapter_writer_release_incomplete"),
        ("raise", "adapter_writer_release_failure"),
    ],
)
def test_restore_writer_release_failure_does_not_publish_wal_staging(
    tmp_path, behavior, reason
):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    adapters = []

    class FailedReleaseWalAdapter(RestoreAdapter):
        def __init__(self, path):
            super().__init__(path)
            self.connection = sqlite3.connect(path)
            self.connection.execute("PRAGMA journal_mode=WAL")

        def rebuild_dependencies(self):
            self.connection.execute(
                "INSERT INTO synthetic_private VALUES('event-release', 'private')"
            )
            self.connection.commit()
            return True

        def release_writers(self):
            if behavior == "raise":
                raise RuntimeError("private release failure")
            return False

    def adapter_factory(path):
        adapter = FailedReleaseWalAdapter(path)
        adapters.append(adapter)
        return adapter

    quarantine = tmp_path / "quarantine.sqlite3"
    result = QuarantinedRestore(ledger, 3, adapter_factory).restore(
        backup, quarantine, NOW
    )

    assert (result.disposition, result.reason) == (RestoreDisposition.BLOCKED, reason)
    assert not quarantine.exists()
    assert not Path(f"{quarantine}.ready.json").exists()
    assert not list(tmp_path.glob(".privacy-restore-*.sqlite3*"))
    retried = QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
        backup, quarantine, NOW
    )
    assert retried.activation_eligible is True
    adapters[0].connection.close()


def test_restore_writer_release_interruption_cleans_wal_staging(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    adapters = []

    class SimulatedInterrupt(BaseException):
        pass

    class InterruptedReleaseWalAdapter(RestoreAdapter):
        def __init__(self, path):
            super().__init__(path)
            self.connection = sqlite3.connect(path)
            self.connection.execute("PRAGMA journal_mode=WAL")

        def rebuild_dependencies(self):
            self.connection.execute(
                "INSERT INTO synthetic_private VALUES('event-interrupt', 'private')"
            )
            self.connection.commit()
            return True

        def release_writers(self):
            raise SimulatedInterrupt()

    def adapter_factory(path):
        adapter = InterruptedReleaseWalAdapter(path)
        adapters.append(adapter)
        return adapter

    quarantine = tmp_path / "quarantine.sqlite3"
    with pytest.raises(SimulatedInterrupt):
        QuarantinedRestore(ledger, 3, adapter_factory).restore(backup, quarantine, NOW)

    assert not quarantine.exists()
    assert not Path(f"{quarantine}.ready.json").exists()
    assert not list(tmp_path.glob(".privacy-restore-*.sqlite3*"))
    retried = QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
        backup, quarantine, NOW
    )
    assert retried.activation_eligible is True
    adapters[0].connection.close()


def test_restore_sidecar_cleanup_resumes_after_an_unlink_interruption(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    adapters = []

    class SimulatedInterrupt(BaseException):
        pass

    class FailedReleaseWalAdapter(RestoreAdapter):
        def __init__(self, path):
            super().__init__(path)
            self.connection = sqlite3.connect(path)
            self.connection.execute("PRAGMA journal_mode=WAL")

        def rebuild_dependencies(self):
            self.connection.execute(
                "INSERT INTO synthetic_private VALUES('event-cleanup', 'private')"
            )
            self.connection.commit()
            return True

        def release_writers(self):
            return False

    def adapter_factory(path):
        adapter = FailedReleaseWalAdapter(path)
        adapters.append(adapter)
        return adapter

    real_unlink = recovery_module.os.unlink
    interrupted = False

    def unlink_then_interrupt(path, *args, **kwargs):
        nonlocal interrupted
        result = real_unlink(path, *args, **kwargs)
        if not interrupted and str(path).endswith("-wal"):
            interrupted = True
            raise SimulatedInterrupt()
        return result

    monkeypatch.setattr(recovery_module.os, "unlink", unlink_then_interrupt)
    quarantine = tmp_path / "quarantine.sqlite3"
    with pytest.raises(SimulatedInterrupt):
        QuarantinedRestore(ledger, 3, adapter_factory).restore(backup, quarantine, NOW)

    assert interrupted is True
    assert not quarantine.exists()
    assert not Path(f"{quarantine}.ready.json").exists()
    assert not list(tmp_path.glob(".privacy-restore-*.sqlite3*"))
    retried = QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
        backup, quarantine, NOW
    )
    assert retried.activation_eligible is True
    adapters[0].connection.close()


def test_restore_cleanup_preserves_a_replacement_database_and_its_wal(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    replacement = tmp_path / "replacement.sqlite3"
    make_source(replacement, "replacement-main").close()
    held_staging = tmp_path / "held-staging.sqlite3"
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    adapters = []

    class ReplacementAdapter(RestoreAdapter):
        def rebuild_dependencies(self):
            self.staging = Path(os.readlink(self.path))
            self.staging.rename(held_staging)
            replacement.rename(self.staging)
            writer = sqlite3.connect(self.staging)
            writer.execute("PRAGMA journal_mode=WAL")
            self.reader = sqlite3.connect(self.staging)
            self.reader.execute("BEGIN")
            self.reader.execute("SELECT * FROM synthetic_private").fetchall()
            writer.execute(
                "UPDATE synthetic_private SET content='replacement-wal' "
                "WHERE event_id='event-001'"
            )
            writer.commit()
            writer.close()
            return False

        def release_writers(self):
            return True

    def adapter_factory(path):
        adapter = ReplacementAdapter(path)
        adapters.append(adapter)
        return adapter

    quarantine = tmp_path / "quarantine.sqlite3"
    result = QuarantinedRestore(ledger, 3, adapter_factory).restore(
        backup, quarantine, NOW
    )

    assert (result.disposition, result.reason) == (
        RestoreDisposition.BLOCKED,
        "dependency_rebuild_incomplete",
    )
    assert not quarantine.exists()
    assert Path(f"{adapters[0].staging}-wal").exists()
    with sqlite3.connect(adapters[0].staging) as connection:
        assert connection.execute(
            "SELECT content FROM synthetic_private WHERE event_id='event-001'"
        ).fetchone() == ("replacement-wal",)
    adapters[0].reader.close()


def test_restore_cleanup_removes_sidecars_after_adapter_unlinks_main(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    adapters = []

    class UnlinkingWalAdapter(RestoreAdapter):
        def __init__(self, path):
            super().__init__(path)
            self.staging = Path(os.readlink(path))
            self.connection = sqlite3.connect(path)
            self.connection.execute("PRAGMA journal_mode=WAL")

        def rebuild_dependencies(self):
            self.connection.execute(
                "INSERT INTO synthetic_private VALUES('event-unlinked', 'private')"
            )
            self.connection.commit()
            self.staging.unlink()
            return False

        def release_writers(self):
            return True

    def adapter_factory(path):
        adapter = UnlinkingWalAdapter(path)
        adapters.append(adapter)
        return adapter

    quarantine = tmp_path / "quarantine.sqlite3"
    result = QuarantinedRestore(ledger, 3, adapter_factory).restore(
        backup, quarantine, NOW
    )

    assert (result.disposition, result.reason) == (
        RestoreDisposition.BLOCKED,
        "dependency_rebuild_incomplete",
    )
    assert not quarantine.exists()
    assert not Path(f"{quarantine}.ready.json").exists()
    assert not list(tmp_path.glob(".privacy-restore-*.sqlite3*"))
    retried = QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
        backup, quarantine, NOW
    )
    assert retried.activation_eligible is True
    adapters[0].connection.close()


def test_restore_cleanup_preserves_a_replaced_sidecar(tmp_path, monkeypatch):
    staging = tmp_path / "staging.sqlite3"
    make_source(staging).close()
    sidecar = Path(f"{staging}-wal")
    sidecar.write_text("owned-sidecar")
    held_sidecar = tmp_path / "held-wal"
    replacement_sidecar = tmp_path / "replacement-wal"
    replacement_sidecar.write_text("replacement-sidecar")
    descriptor = os.open(staging, os.O_RDWR)
    parent_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owned = [recovery_module._OwnedFile(staging, descriptor, parent_descriptor)]
    real_path_matches = recovery_module._path_matches_descriptor
    sidecar_checks = 0

    def replace_after_pinning(path, pinned_descriptor):
        nonlocal sidecar_checks
        if path == sidecar:
            sidecar_checks += 1
            if sidecar_checks == 2:
                sidecar.rename(held_sidecar)
                replacement_sidecar.rename(sidecar)
        return real_path_matches(path, pinned_descriptor)

    monkeypatch.setattr(
        recovery_module, "_path_matches_descriptor", replace_after_pinning
    )
    recovery_module._cleanup_created_database(owned)

    assert sidecar_checks == 2
    assert sidecar.read_text() == "replacement-sidecar"
    assert held_sidecar.read_text() == "owned-sidecar"
    assert not staging.exists()


def test_restore_cleanup_preserves_main_replaced_during_repin(tmp_path, monkeypatch):
    staging = tmp_path / "staging.sqlite3"
    make_source(staging, "owned-main").close()
    sidecar = Path(f"{staging}-wal")
    sidecar.write_text("replacement-sidecar")
    replacement = tmp_path / "replacement.sqlite3"
    make_source(replacement, "replacement-main").close()
    held_staging = tmp_path / "held-staging.sqlite3"
    descriptor = os.open(staging, os.O_RDWR)
    parent_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owned = [recovery_module._OwnedFile(staging, descriptor, parent_descriptor)]
    real_pin_database = recovery_module._pin_database
    replaced = False

    def replace_before_repin(path, label, **kwargs):
        nonlocal replaced
        if label == "staging database" and not replaced:
            replaced = True
            staging.rename(held_staging)
            replacement.rename(staging)
        return real_pin_database(path, label, **kwargs)

    monkeypatch.setattr(recovery_module, "_pin_database", replace_before_repin)
    recovery_module._cleanup_created_database(owned)

    assert replaced is True
    with sqlite3.connect(staging) as connection:
        assert connection.execute(
            "SELECT content FROM synthetic_private WHERE event_id='event-001'"
        ).fetchone() == ("replacement-main",)
    assert sidecar.read_text() == "replacement-sidecar"


def test_restore_cleanup_preserves_sidecar_if_main_reappears(tmp_path, monkeypatch):
    staging = tmp_path / "staging.sqlite3"
    make_source(staging, "owned-main").close()
    descriptor = os.open(staging, os.O_RDWR)
    parent_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owned = [recovery_module._OwnedFile(staging, descriptor, parent_descriptor)]
    staging.unlink()
    sidecar = Path(f"{staging}-wal")
    sidecar.write_text("replacement-sidecar")
    replacement = tmp_path / "replacement.sqlite3"
    make_source(replacement, "replacement-main").close()
    real_open = recovery_module.os.open
    replaced = False

    def replace_main_after_sidecar_open(path, *args, **kwargs):
        nonlocal replaced
        opened_descriptor = real_open(path, *args, **kwargs)
        if str(path).endswith("-wal") and not replaced:
            replaced = True
            replacement.rename(staging)
        return opened_descriptor

    monkeypatch.setattr(recovery_module.os, "open", replace_main_after_sidecar_open)
    recovery_module._cleanup_created_database(owned)

    assert replaced is True
    with sqlite3.connect(staging) as connection:
        assert connection.execute(
            "SELECT content FROM synthetic_private WHERE event_id='event-001'"
        ).fetchone() == ("replacement-main",)
    assert sidecar.read_text() == "replacement-sidecar"


@pytest.mark.parametrize(
    ("method", "behavior", "reason"),
    [
        ("apply_deletion", "raise", "deletion_adapter_failure"),
        ("apply_deletion", "malformed", "invalid_erasure_report"),
        ("apply_revocation", "false", "revocation_reconciliation_incomplete"),
        ("apply_revocation", "string_false", "revocation_reconciliation_incomplete"),
        ("apply_revocation", "raise", "revocation_adapter_failure"),
        ("rebuild_dependencies", "false", "dependency_rebuild_incomplete"),
        ("rebuild_dependencies", "string_false", "dependency_rebuild_incomplete"),
        ("rebuild_dependencies", "raise", "dependency_rebuild_failure"),
        ("release_writers", "false", "adapter_writer_release_incomplete"),
        ("release_writers", "string_false", "adapter_writer_release_incomplete"),
        ("release_writers", "raise", "adapter_writer_release_failure"),
    ],
)
def test_restore_adapter_fault_matrix(tmp_path, method, behavior, reason):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    pending_deletion(ledger)
    consent = ConsentService(ledger)
    consent.grant(
        context(),
        ConsentGrant(
            "consent-fault",
            "grant-fault",
            "participant-a",
            "event-001",
            "public-threads",
            ConsentPurpose.IDENTIFIABLE_DISCLOSURE,
            NOW,
            NOW + timedelta(hours=1),
            "actor-a",
            1,
            "grant-fault-correlation",
            "auth-a",
        ),
    )
    consent.revoke(
        context(),
        consent_id="consent-fault",
        operation_id="revoke-fault",
        revoked_at=NOW + timedelta(minutes=1),
        correlation_id="revoke-fault-correlation",
    )

    class FaultAdapter(RestoreAdapter):
        def apply_deletion(self, record):
            if method == "apply_deletion":
                if behavior == "raise":
                    raise RuntimeError("private fault")
                return object()
            return super().apply_deletion(record)

        def apply_revocation(self, consent_id, revoked_at):
            if method == "apply_revocation":
                if behavior == "raise":
                    raise RuntimeError("private fault")
                return "false" if behavior == "string_false" else False
            return super().apply_revocation(consent_id, revoked_at)

        def rebuild_dependencies(self):
            if method == "rebuild_dependencies":
                if behavior == "raise":
                    raise RuntimeError("private fault")
                return "false" if behavior == "string_false" else False
            return super().rebuild_dependencies()

        def release_writers(self):
            if method == "release_writers":
                if behavior == "raise":
                    raise RuntimeError("private fault")
                return "false" if behavior == "string_false" else False
            return super().release_writers()

    result = QuarantinedRestore(ledger, 3, FaultAdapter).restore(
        backup, tmp_path / f"quarantine-{method}-{behavior}.sqlite3", NOW
    )
    assert (result.disposition, result.reason) == (RestoreDisposition.BLOCKED, reason)
    assert ledger.deletion("delete-operation-001").disposition.value == "pending"


def test_russian_notice_and_versioned_acknowledgement_are_explicit(tmp_path):
    for required in ("OpenAI", "Cloudflare", "Meta", "владелец", "удал", "вывод"):
        assert required.lower() in NOTICE_RU.lower()
    assert "гарант" not in NOTICE_RU.lower()
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    rejection_time = NOW + timedelta(minutes=5)
    service = NoticeService(ledger, clock=lambda: rejection_time)
    first = service.acknowledge(context(), NOTICE_VERSION, NOW, "notice-correlation")
    second = service.acknowledge(context(), NOTICE_VERSION, NOW, "notice-correlation")
    assert (first.created, second.created) == (True, False)
    assert service.has_acknowledged("participant-a", NOTICE_VERSION)
    rejected = service.acknowledge(
        TrustedPrivacyContext(
            "participant-a", "actor-a", "auth-a", OperationOrigin.CHAT
        ),
        NOTICE_VERSION,
        NOW,
        "notice-chat",
    )
    assert (rejected.created, rejected.reason) == (False, "invalid_authentication")
    audit = ledger.audit_records()[-1]
    assert (audit.actor_reference, audit.action_type, audit.result_code) == (
        "actor-a",
        "notice_acknowledgement",
        "invalid_authentication",
    )
    assert audit.occurred_at == rejection_time
    rejected_unknown = service.acknowledge(
        TrustedPrivacyContext(
            "participant-a", "actor-a", "auth-a", OperationOrigin.MODEL
        ),
        "unknown",
        datetime(2026, 10, 10),
        "notice model unknown",
    )
    assert (rejected_unknown.created, rejected_unknown.reason) == (
        False,
        "invalid_authentication",
    )
    assert ledger.audit_records()[-1].result_code == "invalid_authentication"
    assert ledger.audit_records()[-1].correlation_id == "auth-a"
    with pytest.raises(ValueError, match="notice version"):
        service.acknowledge(context(), "unknown", NOW, "notice-bad-version")
    unsupported = ledger.audit_records()[-1]
    assert unsupported.result_code == "unsupported_notice_version"
    assert unsupported.occurred_at == rejection_time


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"principal_id": "private principal"}, "principal id"),
        ({"notice_version": "private notice"}, "notice version"),
        ({"actor_reference": "private actor"}, "actor reference"),
        ({"authentication_event_id": "private auth"}, "authentication event"),
        ({"acknowledged_at": datetime(2026, 10, 10)}, "timestamp"),
        ({"correlation_id": "private correlation"}, "correlation id"),
    ],
)
def test_ledger_rejects_malformed_direct_notice_acknowledgements(
    tmp_path, changes, message
):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    values = {
        "context": context(),
        "principal_id": "participant-a",
        "notice_version": NOTICE_VERSION,
        "actor_reference": "actor-a",
        "authentication_event_id": "auth-a",
        "acknowledged_at": NOW,
        "correlation_id": "notice-direct",
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        ledger.acknowledge_notice(**values)

    assert not ledger.has_notice("participant-a", NOTICE_VERSION)


def test_ledger_rejects_unauthenticated_direct_notice_acknowledgement(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    untrusted = TrustedPrivacyContext(
        "participant-a", "actor-a", "auth-a", OperationOrigin.CHAT
    )

    with pytest.raises(ValueError, match="authentication"):
        ledger.acknowledge_notice(
            untrusted,
            "participant-a",
            NOTICE_VERSION,
            "actor-a",
            "auth-a",
            NOW,
            "notice-direct",
        )

    assert not ledger.has_notice("participant-a", NOTICE_VERSION)


def test_restore_rejects_existing_or_symlink_quarantine(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "old.sqlite3"
    create_sqlite_backup(source, backup, 3, NOW)
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    existing = tmp_path / "existing.sqlite3"
    existing.touch()
    manager = QuarantinedRestore(ledger, 3, RestoreAdapter)
    with pytest.raises(BackupError, match="exists"):
        manager.restore(backup, existing, NOW)
    link = tmp_path / "link.sqlite3"
    link.symlink_to(source)
    with pytest.raises(BackupError, match="symbolic"):
        manager.restore(backup, link, NOW)


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_recovery_rejects_symlinked_output_ancestors(tmp_path, operation):
    source = tmp_path / "source.sqlite3"
    make_source(source).close()
    backup = tmp_path / "backup.sqlite3"
    if operation == "restore":
        create_sqlite_backup(source, backup, 3, NOW)
    target_directory = tmp_path / "redirected"
    nested_target = target_directory / "nested"
    nested_target.mkdir(parents=True)
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(target_directory, target_is_directory=True)
    redirected_output = parent_link / "nested" / f"{operation}.sqlite3"

    with pytest.raises(BackupError, match="parent"):
        if operation == "backup":
            create_sqlite_backup(source, redirected_output, 3, NOW)
        else:
            ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
            QuarantinedRestore(ledger, 3, RestoreAdapter).restore(
                backup, redirected_output, NOW
            )

    assert not (nested_target / f"{operation}.sqlite3").exists()
