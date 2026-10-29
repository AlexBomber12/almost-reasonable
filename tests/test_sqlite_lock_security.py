"""A fresh interpreter must observe locks owned by application connections."""

from contextlib import closing
import subprocess
import sys

import pytest

from app.core.store import EventStore, EventStoreBusyError
from app.owner_control.diagnostics import application_replies
from app.trusted.worker_telemetry import _diagnostics


WRITER = r"""
import sqlite3, sys
print('ready', flush=True)
assert sys.stdin.readline() == 'begin\n'
with sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None) as db:
    try:
        db.execute('BEGIN IMMEDIATE')
    except sqlite3.OperationalError as error:
        assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY, str(error)
        print('blocked', flush=True)
    else:
        print('acquired', flush=True)
        db.rollback()
"""


def writer_result(path):
    # exec a fresh interpreter, never fork an inherited SQLite connection into use.
    with subprocess.Popen(
        [sys.executable, "-c", WRITER, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout.readline() == "ready\n"
        try:
            output, errors = process.communicate("begin\n", timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        assert process.returncode == 0, errors
        return output.strip()


@pytest.mark.parametrize("journal", ["wal", "delete"])
@pytest.mark.parametrize("finish", ["commit", "rollback"])
@pytest.mark.parametrize(
    "check", ["sidecars", "file", "open", "construct", "diagnostics"]
)
def test_security_checks_preserve_independent_writer_exclusion(
    tmp_path, journal, finish, check
):
    store = EventStore(tmp_path / "events.db", busy_timeout_ms=1)
    with closing(store._open()) as owner:
        assert owner.execute(f"PRAGMA journal_mode={journal}").fetchone() == (journal,)
        owner.execute("BEGIN IMMEDIATE")
        owner.execute("UPDATE schema_metadata SET version=version")
        assert writer_result(store.database_path) == "blocked"
        if check == "sidecars":
            store._secure_database_sidecars()
        elif check == "file":
            store._secure_database_file()
        elif check == "open":
            with closing(store._open()) as other:
                assert other.execute(
                    "SELECT version FROM schema_metadata"
                ).fetchone() == (14,)
        elif check == "construct":
            with pytest.raises(EventStoreBusyError):
                EventStore(store.database_path, busy_timeout_ms=1)
        else:
            assert store.diagnostics().schema_version == 14
            assert application_replies(store)["available"]
            assert _diagnostics(store.database_path)["available"]
        assert owner.in_transaction
        assert writer_result(store.database_path) == "blocked"
        getattr(owner, finish)()
        assert writer_result(store.database_path) == "acquired"
    with closing(EventStore(store.database_path)._open()) as reopened:
        assert reopened.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("check", ["sidecars", "configure", "finalize"])
def test_privacy_security_handles_preserve_transaction_locks(tmp_path, check):
    from app.privacy.ledger import PrivacyLedger

    path = tmp_path / "ledger.db"
    ledger = PrivacyLedger(path)
    second = PrivacyLedger(path)
    with closing(ledger._open()) as owner:
        owner.execute("BEGIN IMMEDIATE")
        owner.execute("UPDATE privacy_control_generation SET generation=generation+1")
        assert writer_result(path) == "blocked"
        if check == "sidecars":
            ledger._require_trusted_sidecars()
        elif check == "configure":
            ledger._configure(owner)
        else:
            second._finalizer()
            second._parent_finalizer()
        assert writer_result(path) == "blocked"
        owner.commit()
        assert writer_result(path) == "acquired"
    with closing(PrivacyLedger(path)._open()) as reopened:
        assert reopened.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert reopened.execute(
            "SELECT generation FROM privacy_control_generation"
        ).fetchone() == (1,)


def test_authentication_file_checks_preserve_transaction_locks(tmp_path):
    from app.private_web.auth import AuthStore

    store = AuthStore(tmp_path / "auth.db")
    with closing(store._connect()) as owner:
        owner.execute("BEGIN IMMEDIATE")
        assert writer_result(store.database_path) == "blocked"
        store._secure_file()
        assert writer_result(store.database_path) == "blocked"
        owner.rollback()
        assert writer_result(store.database_path) == "acquired"


def test_authority_journal_finalization_preserves_transaction_locks(tmp_path):
    from app.trusted.research_authority_journal import AuthorityJournal

    tmp_path.chmod(0o700)
    path = tmp_path / "authority.db"
    path.touch(mode=0o600)
    journal = AuthorityJournal(path)
    second = AuthorityJournal(path)
    with journal._open() as owner:
        owner.execute("BEGIN IMMEDIATE")
        assert writer_result(path) == "blocked"
        second._finalizer()
        assert writer_result(path) == "blocked"
    assert writer_result(path) == "acquired"


INITIALIZER = r"""
import sys
sys.path.insert(0, 'tests')
from contextlib import closing
from pathlib import Path
from app.core.store import EventStore
from app.core.provider_spend import ProviderStateStore
from app.privacy.ledger import PrivacyLedger
from test_event_store import principal, private_input
from test_provider_spend import attempt, NOW
print('ready', flush=True)
assert sys.stdin.readline() == 'initialize\n'
path, worker = sys.argv[1:]
store = EventStore(path)
ledger = PrivacyLedger(Path(path).with_name('ledger.db'))
print('initialized', flush=True)
assert sys.stdin.readline() == 'write\n'
provider = ProviderStateStore(store)
for number in range(3):
    identifier = f'worker-{worker}-{number}'
    provider.reserve_attempt(identifier, identifier, NOW)
    provider.record_attempt(attempt(identifier))
    store.append_private_input(principal('a'), private_input(identifier, 'synthetic'))
    assert store.retrieve_private_events(principal('a'))
    assert ledger.control_generation() == 0
    assert store.diagnostics().schema_version == 14
print('done', flush=True)
"""


@pytest.mark.parametrize("initial", ["empty", "v2"])
def test_synchronized_initialization_migration_and_concurrent_durable_writes(
    tmp_path, initial
):
    from test_provider_spend import create_v2_database

    path = tmp_path / "events.db"
    if initial == "v2":
        create_v2_database(path)
    processes = []
    try:
        for number in range(4):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", INITIALIZER, str(path), str(number)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        for process in processes:
            assert process.stdout.readline() == "ready\n"
        for process in processes:
            process.stdin.write("initialize\n")
            process.stdin.flush()
        for process in processes:
            assert process.stdout.readline() == "initialized\n", process.stderr.read()
        for process in processes:
            process.stdin.write("write\n")
            process.stdin.flush()
        for process in processes:
            output, errors = process.communicate(timeout=20)
            assert process.returncode == 0, errors
            assert output == "done\n"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()
    expected_ids = {
        f"worker-{worker}-{number}" for worker in range(4) for number in range(3)
    }
    with closing(EventStore(path)._open()) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            set(
                row[0]
                for row in db.execute("SELECT attempt_id FROM provider_usage_attempts")
            )
            == expected_ids
        )
        assert (
            set(
                row[0]
                for row in db.execute(
                    "SELECT attempt_id FROM provider_attempt_reservations"
                )
            )
            == expected_ids
        )
        assert db.execute(
            "SELECT SUM(total_tokens) FROM provider_usage_attempts"
        ).fetchone() == (180,)
        assert set(
            row[0] for row in db.execute("SELECT event_id FROM event_envelopes")
        ) == {
            *("event-" + identifier for identifier in expected_ids),
            *(["event-v2"] if initial == "v2" else []),
        }
