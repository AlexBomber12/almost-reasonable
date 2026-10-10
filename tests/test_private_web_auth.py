from datetime import UTC, datetime
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from app.private_web.auth import (
    AccountExistsError,
    AuthStore,
    LoginStatus,
    ProvisioningError,
)


class Clock:
    def __init__(self, timestamp=1_800_000_000):
        self.timestamp = timestamp

    def __call__(self):
        return datetime.fromtimestamp(self.timestamp, UTC)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return AuthStore(
        tmp_path / "auth.sqlite3",
        clock=clock,
        session_ttl_seconds=60,
        max_login_failures=3,
        login_window_seconds=30,
        lockout_seconds=40,
    )


def provision(store, username="alice", password="correct horse battery"):
    store.provision_account(username, password, f"participant-{username}")


def test_provision_and_authenticate_two_independent_accounts(store):
    provision(store, "alice", "correct horse battery")
    provision(store, "bob", "another synthetic password")

    alice = store.login("alice", "correct horse battery", "client-a")
    bob = store.login("bob", "another synthetic password", "client-b")

    assert alice.status is LoginStatus.SUCCESS
    assert bob.status is LoginStatus.SUCCESS
    assert alice.session_token != bob.session_token
    assert (
        store.authenticate(alice.session_token).principal_id.value
        == "participant-alice"
    )
    assert store.authenticate(bob.session_token).principal_id.value == "participant-bob"


def test_credentials_and_session_secrets_are_not_stored_in_plaintext(store):
    password = "never-store-this-password"
    provision(store, password=password)
    result = store.login("alice", password, "browser")

    raw = store.database_path.read_bytes()
    assert password.encode() not in raw
    assert result.session_token.encode() not in raw
    assert result.csrf_token.encode() not in raw

    with sqlite3.connect(store.database_path) as connection:
        algorithm, salt, digest = connection.execute(
            "SELECT password_algorithm, password_salt, password_digest FROM accounts"
        ).fetchone()
    assert algorithm == "scrypt-v1"
    assert isinstance(salt, bytes) and len(salt) == 16
    assert isinstance(digest, bytes) and len(digest) == 32


def test_invalid_credentials_are_generic_and_unknown_accounts_follow_same_path(store):
    provision(store)

    wrong = store.login("alice", "incorrect password value", "browser-a")
    unknown = store.login("mallory", "incorrect password value", "browser-b")

    assert wrong.status is LoginStatus.INVALID
    assert unknown.status is LoginStatus.INVALID
    assert wrong.session_token is None
    assert unknown.session_token is None


def test_plaintext_password_is_not_logged(store, caplog):
    password = "incorrect password canary"
    provision(store)
    store.login("alice", password, "browser")
    assert password not in caplog.text


def test_login_attempts_are_bounded_and_recover_after_lockout(store, clock):
    provision(store)
    for _ in range(3):
        assert (
            store.login("alice", "incorrect password value", "browser").status
            is LoginStatus.INVALID
        )

    assert (
        store.login("alice", "correct horse battery", "browser").status
        is LoginStatus.LOCKED
    )
    clock.timestamp += 40
    assert (
        store.login("alice", "correct horse battery", "browser").status
        is LoginStatus.SUCCESS
    )


def test_failure_window_resets_and_is_scoped_by_client(store, clock):
    provision(store)
    assert (
        store.login("alice", "incorrect password value", "a").status
        is LoginStatus.INVALID
    )
    clock.timestamp += 31
    assert (
        store.login("alice", "incorrect password value", "a").status
        is LoginStatus.INVALID
    )
    assert (
        store.login("alice", "correct horse battery", "b").status is LoginStatus.SUCCESS
    )


def test_expired_and_revoked_sessions_are_invalid(store, clock):
    provision(store)
    expired = store.login("alice", "correct horse battery", "browser")
    clock.timestamp += 60
    assert store.authenticate(expired.session_token) is None

    current = store.login("alice", "correct horse battery", "browser")
    assert store.revoke(current.session_token) is True
    assert store.revoke(current.session_token) is False
    assert store.authenticate(current.session_token) is None


def test_successful_login_rotates_existing_session_and_csrf_is_constant_time_checked(
    store,
):
    provision(store)
    old = store.login("alice", "correct horse battery", "browser")
    new = store.login(
        "alice",
        "correct horse battery",
        "browser",
        existing_session_token=old.session_token,
    )

    assert new.status is LoginStatus.SUCCESS
    assert new.session_token != old.session_token
    assert store.authenticate(old.session_token) is None
    session = store.authenticate(new.session_token)
    assert session.accepts_csrf(new.csrf_token) is True
    assert session.accepts_csrf("wrong-csrf-token") is False


def test_session_survives_process_restart(store, clock):
    provision(store)
    result = store.login("alice", "correct horse battery", "browser")

    reopened = AuthStore(
        store.database_path,
        clock=clock,
        session_ttl_seconds=60,
        max_login_failures=3,
        login_window_seconds=30,
        lockout_seconds=40,
    )

    session = reopened.authenticate(result.session_token)
    assert session is not None
    assert session.accepts_csrf(result.csrf_token)


@pytest.mark.parametrize(
    ("username", "password", "participant_id"),
    [
        ("", "correct horse battery", "participant-a"),
        ("has space", "correct horse battery", "participant-a"),
        ("alice", "short", "participant-a"),
        ("alice", "x" * 1025, "participant-a"),
        ("alice", "correct horse battery", ""),
        ("alice", "correct horse battery", "bad participant"),
    ],
)
def test_invalid_provisioning_input_fails_closed(
    store, username, password, participant_id
):
    with pytest.raises(ProvisioningError):
        store.provision_account(username, password, participant_id)


def test_duplicate_username_or_principal_is_rejected(store):
    provision(store)
    with pytest.raises(AccountExistsError):
        store.provision_account(
            "alice", "different valid password", "participant-other"
        )
    with pytest.raises(AccountExistsError):
        store.provision_account(
            "other", "different valid password", "participant-alice"
        )


@pytest.mark.parametrize("token", [None, "", 123, "x" * 300])
def test_malformed_session_tokens_are_invalid_without_database_errors(store, token):
    assert store.authenticate(token) is None
    assert store.revoke(token) is False


def test_disabled_account_invalidates_existing_session(store):
    provision(store)
    result = store.login("alice", "correct horse battery", "browser")
    assert store.disable_account("alice") is True
    assert store.disable_account("alice") is False
    assert store.authenticate(result.session_token) is None


def test_database_is_versioned_separate_and_owner_only(store):
    assert stat.S_IMODE(os.stat(store.database_path).st_mode) == 0o600
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"accounts", "sessions", "login_attempts", "auth_schema"} <= tables
    assert "event_envelopes" not in tables


def test_preexisting_permissions_are_tightened_and_symlinks_are_rejected(
    tmp_path, clock
):
    loose = tmp_path / "loose.sqlite3"
    loose.touch(mode=0o644)
    AuthStore(loose, clock=clock)
    assert stat.S_IMODE(loose.stat().st_mode) == 0o600

    target = tmp_path / "target.sqlite3"
    target.touch(mode=0o600)
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)
    with pytest.raises(RuntimeError, match="authentication store operation failed"):
        AuthStore(link, clock=clock)


def test_invalid_configuration_and_unsafe_parent_are_rejected(tmp_path, clock):
    with pytest.raises(ValueError):
        AuthStore(tmp_path / "a.sqlite3", clock=clock, session_ttl_seconds=0)
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with pytest.raises(PermissionError):
        AuthStore(unsafe / "auth.sqlite3", clock=clock)


def test_future_schema_is_rejected(tmp_path, clock):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="unsupported authentication schema"):
        AuthStore(path, clock=clock)


def test_malformed_sqlite_file_is_sanitized(tmp_path, clock):
    path = tmp_path / "malformed.sqlite3"
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(RuntimeError, match="authentication store operation failed"):
        AuthStore(path, clock=clock)


def test_invalid_clock_and_schema_metadata_fail_closed(tmp_path):
    invalid_clock = AuthStore(
        tmp_path / "bad-clock.sqlite3", clock=lambda: datetime(2030, 1, 1)
    )
    with pytest.raises(RuntimeError, match="aware datetime"):
        invalid_clock.provision_account(
            "alice", "correct horse battery", "participant-alice"
        )

    path = tmp_path / "bad-schema.sqlite3"
    store = AuthStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM auth_schema")
    with pytest.raises(RuntimeError, match="schema is invalid"):
        AuthStore(store.database_path)


def test_filesystem_and_connection_failures_are_sanitized(tmp_path, clock, monkeypatch):
    missing = tmp_path / "missing" / "auth.sqlite3"
    with pytest.raises(PermissionError, match="parent is unavailable"):
        AuthStore(missing, clock=clock)

    monkeypatch.setattr(
        "app.private_web.auth.os.open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(RuntimeError, match="authentication store operation failed"):
        AuthStore(tmp_path / "open-fails.sqlite3", clock=clock)


def test_nonregular_database_and_post_open_stat_failures_are_sanitized(
    tmp_path, clock, monkeypatch
):
    real_fstat = os.fstat
    monkeypatch.setattr(
        "app.private_web.auth.os.fstat",
        lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFDIR, st_uid=os.geteuid()),
    )
    with pytest.raises(RuntimeError, match="authentication store operation failed"):
        AuthStore(tmp_path / "nonregular.sqlite3", clock=clock)

    monkeypatch.setattr("app.private_web.auth.os.fstat", real_fstat)
    store = AuthStore(tmp_path / "stat.sqlite3", clock=clock)
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _path: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o644, st_uid=os.geteuid()
        ),
    )
    with pytest.raises(RuntimeError, match="authentication store operation failed"):
        store.authenticate("synthetic-token")
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _path: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(RuntimeError, match="authentication store operation failed"):
        store.authenticate("synthetic-token")


def test_sqlite_connect_failure_is_sanitized(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(
        "app.private_web.auth.sqlite3.connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError()),
    )
    with pytest.raises(RuntimeError, match="authentication store operation failed"):
        AuthStore(tmp_path / "connect-fails.sqlite3", clock=clock)


@pytest.mark.parametrize(
    ("operation", "table"),
    [
        ("provision", "accounts"),
        ("login", "login_attempts"),
        ("authenticate", "sessions"),
        ("revoke", "sessions"),
        ("disable", "accounts"),
    ],
)
def test_runtime_database_failures_are_sanitized(tmp_path, clock, operation, table):
    store = AuthStore(tmp_path / f"{operation}.sqlite3", clock=clock)
    if operation != "provision":
        provision(store)
    session = (
        store.login("alice", "correct horse battery", "browser")
        if operation in {"authenticate", "revoke"}
        else None
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(f"DROP TABLE {table}")

    with pytest.raises(RuntimeError, match="authentication store operation failed"):
        if operation == "provision":
            provision(store)
        elif operation == "login":
            store.login("alice", "correct horse battery", "browser")
        elif operation == "authenticate":
            store.authenticate(session.session_token)
        elif operation == "revoke":
            store.revoke(session.session_token)
        else:
            store.disable_account("alice")


def test_malformed_login_values_and_password_types_are_generic(store):
    provision(store)
    assert store.login(None, None, None).status is LoginStatus.INVALID
    assert store.login("alice", "x" * 2048, "browser").status is LoginStatus.INVALID
    store.disable_account("alice")
    assert (
        store.login("alice", "correct horse battery", "new-browser").status
        is LoginStatus.INVALID
    )
    with pytest.raises(ProvisioningError, match="password must be text"):
        store.provision_account("bob", None, "participant-bob")
