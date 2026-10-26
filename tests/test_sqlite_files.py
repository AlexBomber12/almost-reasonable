"""Security checks must operate on pinned inodes, without data descriptors."""

import os
import sqlite3
import stat

import pytest

from app.core import sqlite_files
from app.core.store import EventStore, EventStoreError


@pytest.mark.parametrize("entry", ["symlink", "directory", "fifo", "hardlink"])
@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_store_rejects_unsafe_database_and_sidecar_entries(tmp_path, entry, suffix):
    path = tmp_path / ("events.db" + suffix)
    target = tmp_path / "untouched"
    target.write_text("synthetic")
    target.chmod(0o644)
    if entry == "symlink":
        path.symlink_to(target)
    elif entry == "directory":
        path.mkdir()
    elif entry == "fifo":
        os.mkfifo(path)
    else:
        os.link(target, path)
    with pytest.raises(EventStoreError):
        EventStore(tmp_path / "events.db")
    assert target.read_text() == "synthetic"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_permission_update_does_not_follow_a_substituted_path(tmp_path):
    path = tmp_path / "events.db"
    path.write_text("original")
    descriptor = sqlite_files.sqlite_metadata_handle(path)
    old = tmp_path / "original"
    target = tmp_path / "target"
    target.write_text("target")
    target.chmod(0o644)
    try:
        path.rename(old)
        path.symlink_to(target)
        sqlite_files.chmod_sqlite_metadata(descriptor, 0o600)
        assert stat.S_IMODE(old.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
        assert os.fstat(descriptor).st_ino == old.stat().st_ino
    finally:
        os.close(descriptor)


def test_creation_failure_removes_unpublished_file(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError("synthetic link failure")

    monkeypatch.setattr(sqlite_files, "_publish_closed_file", fail)
    with pytest.raises(PermissionError):
        sqlite_files.sqlite_metadata_handle(tmp_path / "events.db", create=True)
    assert list(tmp_path.iterdir()) == []


def test_store_rejects_foreign_owned_database_inode(tmp_path, monkeypatch):
    real_fstat = os.fstat

    def foreign_owner(descriptor):
        values = list(real_fstat(descriptor))
        values[4] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", foreign_owner)
    with pytest.raises(EventStoreError):
        EventStore(tmp_path / "events.db")


def test_connection_rejects_same_mode_database_replacement(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "events.db")
    real_connect = sqlite3.connect
    original = tmp_path / "original.db"

    def replace(path, *args, **kwargs):
        store.database_path.rename(original)
        replacement = sqlite_files.sqlite_metadata_handle(path, create=True)
        os.close(replacement)
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr("app.core.store.sqlite3.connect", replace)
    with pytest.raises(EventStoreError):
        store._open()
    assert original.exists()


def test_another_store_can_admit_immediately_after_database_publication(
    tmp_path, monkeypatch
):
    path = tmp_path / "events.db"
    publish = sqlite_files._publish_closed_file
    admitted = []

    def admit_before_publisher_cleanup(source, destination, **kwargs):
        publish(source, destination, **kwargs)
        admitted.append(EventStore(path))

    monkeypatch.setattr(
        sqlite_files, "_publish_closed_file", admit_before_publisher_cleanup
    )
    first = EventStore(path)
    assert len(admitted) == 1
    assert first.diagnostics() == admitted[0].diagnostics()


def test_atomic_publication_fails_closed_when_rename_is_unavailable(
    tmp_path, monkeypatch
):
    import ctypes
    import errno

    def unavailable(*args):
        ctypes.set_errno(errno.ENOSYS)
        return -1

    monkeypatch.setattr(sqlite_files, "_renameat2", unavailable)
    with pytest.raises(OSError) as error:
        sqlite_files.sqlite_metadata_handle(tmp_path / "events.db", create=True)
    assert error.value.errno == errno.ENOSYS
    assert list(tmp_path.iterdir()) == []


def test_creation_rejects_embedded_null_without_publishing_a_truncated_name(tmp_path):
    with pytest.raises(ValueError, match="embedded null"):
        sqlite_files.sqlite_metadata_handle(tmp_path / "events.db\0extra", create=True)
    assert list(tmp_path.iterdir()) == []
