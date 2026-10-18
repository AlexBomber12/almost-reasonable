"""Operator boundary tests use only synthetic EventStore and privacy data."""

import json
import runpy
import sqlite3
import subprocess
import sys

import pytest

from app.core.agent_output_store import AgentOutputStore
from app.core.self_state_store import SelfStateStore
from app.core.store import EventStore, SCHEMA_VERSION
from app.ops.eventstore_backup import main
from app.ops import eventstore_backup
from app.privacy import PrivacyLedger
from app.privacy.application import EventStoreErasureAdapter
from app.privacy.deletion import DeletionCoordinator
from app.privacy.recovery import _schema_version
from app.privacy import recovery
from test_agent_output_store import operation, persist
from test_event_store import principal, private_input
from test_privacy_application import CANARY, setup as privacy_setup
from test_privacy_deletion import NOW, context, request
from test_self_state import goal_change


def invoke(capsys, *args):
    code = main(list(map(str, args)))
    return code, json.loads(capsys.readouterr().out)


def test_help_and_required_arguments(capsys):
    try:
        main(["--help"])
    except SystemExit as error:
        assert error.code == 0
    assert "backup" in capsys.readouterr().out
    for args in ([], ["backup"], ["restore", "--backup", "/tmp/old.db"]):
        try:
            main(args)
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError("missing arguments accepted")
        assert json.loads(capsys.readouterr().out) == {
            "status": "invalid_arguments",
            "reason_code": "invalid_arguments",
        }
    completed = subprocess.run(
        [sys.executable, "-m", "app.ops.eventstore_backup", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "quarantine" in completed.stdout
    with pytest.raises(SystemExit) as exit_result:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sys, "argv", ["eventstore_backup", "--help"])
            runpy.run_path(str(eventstore_backup.__file__), run_name="__main__")
    assert exit_result.value.code == 0
    capsys.readouterr()


def test_wal_backup_and_current_ledger_replay(tmp_path, capsys):
    source = tmp_path / "events.db"
    store = EventStore(source)
    ledger_path = tmp_path / "privacy.db"
    ledger = PrivacyLedger(ledger_path)
    store.append_private_input(principal("a"), private_input("a", "synthetic secret"))
    reader = sqlite3.connect(source)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM event_envelopes").fetchone()
        store.append_private_input(principal("a"), private_input("a2", "WAL content"))
        assert (tmp_path / "events.db-wal").exists()
        backup = tmp_path / "snapshot.db"
        code, output = invoke(
            capsys, "backup", "--source", source, "--destination", backup
        )
        assert (code, output) == (0, {"status": "backup_created", "reason_code": "ok"})
    finally:
        reader.close()
    assert _schema_version(backup, immutable=True) == SCHEMA_VERSION
    with sqlite3.connect(f"file:{backup}?immutable=1", uri=True) as db:
        assert db.execute("SELECT count(*) FROM event_envelopes").fetchone() == (2,)
    assert (backup.stat().st_mode & 0o777) == 0o600
    assert ((tmp_path / "snapshot.db.metadata.json").stat().st_mode & 0o777) == 0o600

    coordinator = DeletionCoordinator(
        ledger, EventStoreErasureAdapter(store), clock=lambda: NOW
    )
    coordinator.delete(context(), request(content_reference="event-a"))
    quarantine = tmp_path / "candidate.db"
    code, output = invoke(
        capsys,
        "restore",
        "--backup",
        backup,
        "--ledger",
        ledger_path,
        "--quarantine",
        quarantine,
    )
    assert (code, output) == (0, {"status": "quarantine_verified", "reason_code": "ok"})
    with sqlite3.connect(f"file:{quarantine}?immutable=1", uri=True) as db:
        assert db.execute("SELECT count(*) FROM event_envelopes").fetchone() == (2,)
        assert db.execute("SELECT count(*) FROM private_payloads").fetchone() == (1,)
    assert (quarantine.stat().st_mode & 0o777) == 0o400
    assert (tmp_path / "candidate.db.ready.json").exists()
    assert store.retrieve_private_events(principal("a"))


def test_existing_and_unsafe_paths_preserve_operator_files(tmp_path, capsys):
    source = tmp_path / "events.db"
    EventStore(source)
    destination = tmp_path / "snapshot.db"
    destination.write_text("operator-owned")
    code, output = invoke(
        capsys, "backup", "--source", source, "--destination", destination
    )
    assert code == 1 and output["reason_code"] == "backup_failed"
    assert destination.read_text() == "operator-owned"
    destination.unlink()
    source.chmod(0o666)
    code, _ = invoke(capsys, "backup", "--source", source, "--destination", destination)
    assert code == 1 and not destination.exists()
    assert source.stat().st_mode & 0o777 == 0o666
    source.chmod(0o600)
    link = tmp_path / "link.db"
    link.symlink_to(source)
    code, _ = invoke(capsys, "backup", "--source", link, "--destination", destination)
    assert code == 1 and not destination.exists()


def test_restore_requires_existing_authoritative_ledger(tmp_path, capsys):
    source = tmp_path / "events.db"
    EventStore(source)
    backup = tmp_path / "snapshot.db"
    assert invoke(capsys, "backup", "--source", source, "--destination", backup)[0] == 0
    absent = tmp_path / "absent-ledger.db"
    quarantine = tmp_path / "candidate.db"
    code, output = invoke(
        capsys,
        "restore",
        "--backup",
        backup,
        "--ledger",
        absent,
        "--quarantine",
        quarantine,
    )
    assert code == 1 and output["reason_code"] == "restore_failed"
    assert not absent.exists() and not quarantine.exists()
    ledger = PrivacyLedger(tmp_path / "privacy.db")
    del ledger
    backup.write_bytes(b"corrupt")
    code, _ = invoke(
        capsys,
        "restore",
        "--backup",
        backup,
        "--ledger",
        tmp_path / "privacy.db",
        "--quarantine",
        quarantine,
    )
    assert code == 1 and not quarantine.exists()


def test_empty_existing_ledger_is_not_initialized(tmp_path, capsys):
    source = tmp_path / "events.db"
    EventStore(source)
    backup = tmp_path / "snapshot.db"
    assert invoke(capsys, "backup", "--source", source, "--destination", backup)[0] == 0
    ledger_path = tmp_path / "empty-ledger.db"
    ledger_path.touch(mode=0o600)
    quarantine = tmp_path / "candidate.db"
    code, output = invoke(
        capsys,
        "restore",
        "--backup",
        backup,
        "--ledger",
        ledger_path,
        "--quarantine",
        quarantine,
    )
    assert (code, output) == (
        1,
        {"status": "restore_failed", "reason_code": "restore_failed"},
    )
    assert ledger_path.read_bytes() == b""
    assert not quarantine.exists()
    assert not (tmp_path / "candidate.db.ready.json").exists()


def test_ledger_permissions_and_blocked_reconciliation(tmp_path, capsys):
    source = tmp_path / "events.db"
    store = EventStore(source)
    backup = tmp_path / "snapshot.db"
    assert invoke(capsys, "backup", "--source", source, "--destination", backup)[0] == 0
    ledger_path = tmp_path / "privacy.db"
    ledger = PrivacyLedger(ledger_path)
    quarantine = tmp_path / "candidate.db"
    ledger_path.chmod(0o644)
    assert (
        invoke(
            capsys,
            "restore",
            "--backup",
            backup,
            "--ledger",
            ledger_path,
            "--quarantine",
            quarantine,
        )[0]
        == 1
    )
    assert ledger_path.stat().st_mode & 0o777 == 0o644
    ledger_path.chmod(0o600)
    sidecar = tmp_path / "privacy.db-journal"
    sidecar.write_text("operator sidecar")
    sidecar.chmod(0o644)
    assert (
        invoke(
            capsys,
            "restore",
            "--backup",
            backup,
            "--ledger",
            ledger_path,
            "--quarantine",
            quarantine,
        )[0]
        == 1
    )
    assert sidecar.stat().st_mode & 0o777 == 0o644
    sidecar.unlink()
    DeletionCoordinator(
        ledger, EventStoreErasureAdapter(store), clock=lambda: NOW
    ).delete(context(), request())
    code, output = invoke(
        capsys,
        "restore",
        "--backup",
        backup,
        "--ledger",
        ledger_path,
        "--quarantine",
        quarantine,
    )
    assert code == 1 and output == {
        "status": "quarantine_blocked",
        "reason_code": "reconciliation_blocked",
    }
    assert not (tmp_path / "candidate.db.ready.json").exists()


def test_partial_backup_cleanup_preserves_preexisting_files(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "events.db"
    EventStore(source)
    destination = tmp_path / "snapshot.db"
    unrelated = tmp_path / "operator-note"
    unrelated.write_text("keep")

    def fail_after_creation(*_args):
        raise OSError("synthetic private failure")

    monkeypatch.setattr(recovery, "_sqlite_copy", fail_after_creation)
    code, output = invoke(
        capsys, "backup", "--source", source, "--destination", destination
    )
    assert code == 1 and output == {
        "status": "backup_failed",
        "reason_code": "backup_failed",
    }
    assert unrelated.read_text() == "keep"
    assert not destination.exists()
    assert not (tmp_path / "snapshot.db.metadata.json").exists()
    for suffix in ("-wal", "-shm", "-journal"):
        assert not (tmp_path / f"snapshot.db{suffix}").exists()


def test_real_derived_state_and_reply_reconcile_through_cli(tmp_path, capsys):
    store, ledger, _, coordinator = privacy_setup.__wrapped__(tmp_path)
    SelfStateStore(store).create_goal_candidate(principal("a"), goal_change("event-a"))
    outputs = AgentOutputStore(store)
    outputs.reserve_reply(principal("a"), operation())
    persist(outputs, content=CANARY)
    backup = tmp_path / "snapshot.db"
    assert (
        invoke(
            capsys, "backup", "--source", store.database_path, "--destination", backup
        )[0]
        == 0
    )
    coordinator.delete(context(), request(content_reference="event-a"))
    quarantine = tmp_path / "candidate.db"
    assert (
        invoke(
            capsys,
            "restore",
            "--backup",
            backup,
            "--ledger",
            ledger.path,
            "--quarantine",
            quarantine,
        )[0]
        == 0
    )
    with sqlite3.connect(f"file:{quarantine}?immutable=1", uri=True) as db:
        assert CANARY not in "\n".join(db.iterdump())
        assert db.execute("SELECT count(*) FROM agent_reply_payloads").fetchone() == (
            0,
        )
    assert store.retrieve_private_events(principal("a"))
