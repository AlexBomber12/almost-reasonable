"""Dedicated local account and session storage for the private web boundary."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat

from app.core.events import (
    AuthenticatedSourceReference,
    ParticipantId,
    TrustedPrincipalContext,
)


SCHEMA_VERSION = 1
MIN_PASSWORD_UTF8_BYTES = 12
MAX_PASSWORD_UTF8_BYTES = 1024
MAX_SESSION_TOKEN_LENGTH = 256
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_DUMMY_SALT = bytes.fromhex("b10cf9d05842a27794a20cfb5b38ad80")
_DUMMY_DIGEST = hashlib.scrypt(
    b"nonexistent-account-password",
    salt=_DUMMY_SALT,
    n=_SCRYPT_N,
    r=_SCRYPT_R,
    p=_SCRYPT_P,
    dklen=_SCRYPT_DKLEN,
    maxmem=_SCRYPT_MAXMEM,
)


class ProvisioningError(ValueError):
    """An operator-supplied account field is invalid."""


class AccountExistsError(ProvisioningError):
    """The requested login name or participant identity is already provisioned."""


class LoginStatus(StrEnum):
    SUCCESS = "success"
    INVALID = "invalid"
    LOCKED = "locked"


@dataclass(frozen=True, slots=True)
class LoginResult:
    status: LoginStatus
    session_token: str | None = field(default=None, repr=False)
    csrf_token: str | None = field(default=None, repr=False)
    expires_at: int | None = None


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    username: str
    principal_id: ParticipantId
    authenticated_source: AuthenticatedSourceReference
    expires_at: int
    _csrf_digest: bytes = field(repr=False)

    @property
    def trusted_context(self) -> TrustedPrincipalContext:
        return TrustedPrincipalContext(
            principal_id=self.principal_id,
            authenticated_source=self.authenticated_source,
        )

    def accepts_csrf(self, token: object) -> bool:
        supplied = _secret_digest(token)
        return supplied is not None and hmac.compare_digest(supplied, self._csrf_digest)


_SCHEMA = (
    """
    CREATE TABLE auth_schema (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        version INTEGER NOT NULL CHECK (version = 1)
    )
    """,
    """
    CREATE TABLE accounts (
        account_id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        participant_id TEXT NOT NULL UNIQUE,
        password_algorithm TEXT NOT NULL CHECK (password_algorithm = 'scrypt-v1'),
        password_salt BLOB NOT NULL CHECK (length(password_salt) = 16),
        password_digest BLOB NOT NULL CHECK (length(password_digest) = 32),
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE sessions (
        token_digest BLOB PRIMARY KEY CHECK (length(token_digest) = 32),
        account_id INTEGER NOT NULL REFERENCES accounts(account_id),
        csrf_digest BLOB NOT NULL CHECK (length(csrf_digest) = 32),
        source_reference TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        revoked_at INTEGER,
        CHECK (expires_at > created_at),
        CHECK (revoked_at IS NULL OR revoked_at >= created_at)
    )
    """,
    """
    CREATE TABLE login_attempts (
        attempt_key BLOB PRIMARY KEY CHECK (length(attempt_key) = 32),
        failure_count INTEGER NOT NULL CHECK (failure_count >= 1),
        window_started_at INTEGER NOT NULL,
        blocked_until INTEGER
    )
    """,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _secret_digest(value: object) -> bytes | None:
    if type(value) is not str or not value or len(value) > MAX_SESSION_TOKEN_LENGTH:
        return None
    return hashlib.sha256(value.encode("utf-8")).digest()


class AuthStore:
    """Versioned SQLite authentication store, separate from the EventStore."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
        session_ttl_seconds: int = 8 * 60 * 60,
        max_login_failures: int = 5,
        login_window_seconds: int = 5 * 60,
        lockout_seconds: int = 15 * 60,
    ):
        settings = (
            session_ttl_seconds,
            max_login_failures,
            login_window_seconds,
            lockout_seconds,
        )
        if any(type(value) is not int or value < 1 for value in settings):
            raise ValueError("authentication timing settings must be positive integers")
        self._database_path = Path(os.path.abspath(database_path))
        self._clock = clock
        self._session_ttl_seconds = session_ttl_seconds
        self._max_login_failures = max_login_failures
        self._login_window_seconds = login_window_seconds
        self._lockout_seconds = lockout_seconds
        self._verify_parent()
        self._secure_file()
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    @property
    def lockout_seconds(self) -> int:
        return self._lockout_seconds

    def _now(self) -> int:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("authentication clock must return an aware datetime")
        return int(value.timestamp())

    def _verify_parent(self) -> None:
        try:
            status = self._database_path.parent.stat()
        except OSError:
            raise PermissionError(
                "authentication database parent is unavailable"
            ) from None
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise PermissionError("authentication database parent is not private")

    def _secure_file(self) -> None:
        try:
            descriptor = os.open(
                self._database_path,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode) or status.st_uid != os.geteuid():
                    raise RuntimeError("authentication store operation failed")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        except OSError:
            raise RuntimeError("authentication store operation failed") from None

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self._database_path)
            status = self._database_path.lstat()
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != os.geteuid()
                or stat.S_IMODE(status.st_mode) != 0o600
            ):
                connection.close()
                raise RuntimeError("authentication store operation failed")
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except OSError:
            raise RuntimeError("authentication store operation failed") from None
        except sqlite3.Error:
            raise RuntimeError("authentication store operation failed") from None

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                with connection:
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO auth_schema(singleton, version) VALUES (1, ?)",
                        (SCHEMA_VERSION,),
                    )
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported authentication schema version: {version}"
                )
            else:
                metadata = connection.execute(
                    "SELECT version FROM auth_schema WHERE singleton = 1"
                ).fetchone()
                if metadata != (SCHEMA_VERSION,):
                    raise RuntimeError("authentication store schema is invalid")
        except sqlite3.Error:
            raise RuntimeError("authentication store operation failed") from None
        finally:
            connection.close()

    def provision_account(
        self, username: str, password: str, participant_id: str
    ) -> None:
        self._require_token("username", username)
        self._require_token("participant_id", participant_id)
        password_bytes = self._password_bytes(password)
        salt = secrets.token_bytes(16)
        digest = self._password_digest(password_bytes, salt)
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO accounts(
                        username, participant_id, password_algorithm,
                        password_salt, password_digest, created_at
                    ) VALUES (?, ?, 'scrypt-v1', ?, ?, ?)
                    """,
                    (username, participant_id, salt, digest, self._now()),
                )
        except sqlite3.IntegrityError:
            raise AccountExistsError("account or participant already exists") from None
        except sqlite3.Error:
            raise RuntimeError("authentication store operation failed") from None
        finally:
            connection.close()

    def login(
        self,
        username: object,
        password: object,
        client_key: object,
        *,
        existing_session_token: object = None,
    ) -> LoginResult:
        normalized_username = username if type(username) is str else ""
        normalized_client = client_key if type(client_key) is str else ""
        client_attempt_key = self._attempt_key("client", normalized_client)
        account_attempt_key = self._attempt_key("account", normalized_username)
        now = self._now()
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    DELETE FROM login_attempts
                    WHERE (blocked_until IS NULL AND window_started_at <= ?)
                       OR (blocked_until IS NOT NULL AND blocked_until <= ?)
                    """,
                    (now - self._login_window_seconds, now),
                )
                attempt_rows = connection.execute(
                    """
                    SELECT attempt_key, failure_count, window_started_at, blocked_until
                    FROM login_attempts WHERE attempt_key IN (?, ?)
                    """,
                    (client_attempt_key, account_attempt_key),
                ).fetchall()
                attempts = {row[0]: row[1:] for row in attempt_rows}
                if any(
                    attempt is not None and attempt[2] is not None and now < attempt[2]
                    for attempt in (
                        attempts.get(client_attempt_key),
                        attempts.get(account_attempt_key),
                    )
                ):
                    return LoginResult(LoginStatus.LOCKED)
                row = connection.execute(
                    """
                    SELECT account_id, participant_id, password_salt,
                           password_digest, enabled
                    FROM accounts WHERE username = ?
                    """,
                    (normalized_username,),
                ).fetchone()
                supplied, supplied_is_valid = self._password_bytes_for_login(password)
                salt = row[2] if row is not None else _DUMMY_SALT
                expected = row[3] if row is not None else _DUMMY_DIGEST
                calculated = self._password_digest(supplied, salt)
                digest_matches = hmac.compare_digest(calculated, expected)
                valid = (
                    row is not None
                    and row[4] == 1
                    and supplied_is_valid
                    and digest_matches
                )
                if not valid:
                    self._record_failure(
                        connection,
                        client_attempt_key,
                        attempts.get(client_attempt_key),
                        now,
                    )
                    self._record_failure(
                        connection,
                        account_attempt_key,
                        attempts.get(account_attempt_key),
                        now,
                    )
                    return LoginResult(LoginStatus.INVALID)

                connection.execute(
                    "DELETE FROM login_attempts WHERE attempt_key = ?",
                    (account_attempt_key,),
                )
                existing_digest = _secret_digest(existing_session_token)
                if existing_digest is not None:
                    connection.execute(
                        "UPDATE sessions SET revoked_at = ? "
                        "WHERE token_digest = ? AND revoked_at IS NULL",
                        (now, existing_digest),
                    )
                session_token = secrets.token_urlsafe(32)
                csrf_token = secrets.token_urlsafe(32)
                expires_at = now + self._session_ttl_seconds
                source_reference = f"web-session:{secrets.token_urlsafe(18)}"
                connection.execute(
                    """
                    INSERT INTO sessions(
                        token_digest, account_id, csrf_digest, source_reference,
                        created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _secret_digest(session_token),
                        row[0],
                        _secret_digest(csrf_token),
                        source_reference,
                        now,
                        expires_at,
                    ),
                )
                return LoginResult(
                    LoginStatus.SUCCESS, session_token, csrf_token, expires_at
                )
        except sqlite3.Error:
            raise RuntimeError("authentication store operation failed") from None
        finally:
            connection.close()

    def _record_failure(
        self,
        connection: sqlite3.Connection,
        attempt_key: bytes,
        attempt: tuple[int, int, int | None] | None,
        now: int,
    ) -> None:
        if attempt is None or now - attempt[1] >= self._login_window_seconds:
            count = 1
            window_started = now
        else:
            count = attempt[0] + 1
            window_started = attempt[1]
        blocked_until = (
            now + self._lockout_seconds if count >= self._max_login_failures else None
        )
        connection.execute(
            """
            INSERT INTO login_attempts(
                attempt_key, failure_count, window_started_at, blocked_until
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(attempt_key) DO UPDATE SET
                failure_count = excluded.failure_count,
                window_started_at = excluded.window_started_at,
                blocked_until = excluded.blocked_until
            """,
            (attempt_key, count, window_started, blocked_until),
        )

    def authenticate(self, session_token: object) -> AuthenticatedSession | None:
        digest = _secret_digest(session_token)
        if digest is None:
            return None
        now = self._now()
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    """
                    SELECT account.username, account.participant_id,
                           session.source_reference, session.expires_at,
                           session.csrf_digest, session.revoked_at, account.enabled
                    FROM sessions AS session
                    JOIN accounts AS account ON account.account_id = session.account_id
                    WHERE session.token_digest = ?
                    """,
                    (digest,),
                ).fetchone()
                if row is None or row[5] is not None or row[6] != 1:
                    return None
                if now >= row[3]:
                    connection.execute(
                        "UPDATE sessions SET revoked_at = ? WHERE token_digest = ?",
                        (now, digest),
                    )
                    return None
                return AuthenticatedSession(
                    username=row[0],
                    principal_id=ParticipantId(row[1]),
                    authenticated_source=AuthenticatedSourceReference(row[2]),
                    expires_at=row[3],
                    _csrf_digest=row[4],
                )
        except sqlite3.Error:
            raise RuntimeError("authentication store operation failed") from None
        finally:
            connection.close()

    def revoke(self, session_token: object) -> bool:
        digest = _secret_digest(session_token)
        if digest is None:
            return False
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE sessions SET revoked_at = ? "
                    "WHERE token_digest = ? AND revoked_at IS NULL",
                    (self._now(), digest),
                )
                return cursor.rowcount == 1
        except sqlite3.Error:
            raise RuntimeError("authentication store operation failed") from None
        finally:
            connection.close()

    def disable_account(self, username: str) -> bool:
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE accounts SET enabled = 0 WHERE username = ? AND enabled = 1",
                    (username,),
                )
                return cursor.rowcount == 1
        except sqlite3.Error:
            raise RuntimeError("authentication store operation failed") from None
        finally:
            connection.close()

    @staticmethod
    def _require_token(field_name: str, value: object) -> None:
        if type(value) is not str or _TOKEN.fullmatch(value) is None:
            raise ProvisioningError(f"{field_name} must be a bounded opaque token")

    @staticmethod
    def _password_bytes(password: object) -> bytes:
        if type(password) is not str:
            raise ProvisioningError("password must be text")
        encoded = password.encode("utf-8")
        if not MIN_PASSWORD_UTF8_BYTES <= len(encoded) <= MAX_PASSWORD_UTF8_BYTES:
            raise ProvisioningError("password length is outside the supported range")
        return encoded

    @staticmethod
    def _attempt_key(kind: str, *parts: str) -> bytes:
        serialized = "\0".join((kind, *parts)).encode("utf-8")
        return hashlib.sha256(serialized).digest()

    @staticmethod
    def _password_bytes_for_login(password: object) -> tuple[bytes, bool]:
        if type(password) is not str:
            return b"", False
        encoded = password.encode("utf-8")
        if len(encoded) > MAX_PASSWORD_UTF8_BYTES:
            return encoded[:MAX_PASSWORD_UTF8_BYTES], False
        return encoded, True

    @staticmethod
    def _password_digest(password: bytes, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password,
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=_SCRYPT_DKLEN,
            maxmem=_SCRYPT_MAXMEM,
        )
