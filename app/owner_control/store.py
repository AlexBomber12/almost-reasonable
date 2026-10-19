"""Protected owner authentication, durable stop and bounded sanitized audit."""

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import stat

from app.private_web.auth import AuthStore
from app.privacy.files import open_trusted_directory_chain


class OwnerStore:
    """Local OS-authenticated provisioning; never accept a participant AuthStore.

    The dedicated directory and owner schema marker prevent accidental reuse of
    a participant database. Only this protected store supplies owner authority.
    SQLite immediate transactions serialize stop, replay and audit commits.
    """

    def __init__(self, directory, *, clock=lambda: datetime.now(UTC)):
        self.directory = Path(directory).absolute()
        self._clock = clock
        self.directory.mkdir(mode=0o700, exist_ok=True)
        descriptor = open_trusted_directory_chain(self.directory)
        try:
            identity = os.fstat(descriptor)
            if (
                identity.st_uid != os.geteuid()
                or stat.S_IMODE(identity.st_mode) != 0o700
            ):
                raise PermissionError("owner directory must be private")
        finally:
            os.close(descriptor)
        self.path = self.directory / "owner.sqlite"
        existing = self.path.exists()
        if existing:
            with self._connection() as connection:
                if connection.execute(
                    "SELECT version FROM owner_schema"
                ).fetchall() != [(1,)]:
                    raise RuntimeError("unsupported owner schema")
        self._auth = AuthStore(self.path, clock=clock, session_ttl_seconds=3600)
        if not existing:
            with self._connection() as connection:
                connection.execute(
                    "CREATE TABLE owner_schema(version INTEGER NOT NULL)"
                )
                connection.execute("INSERT INTO owner_schema VALUES (1)")
                connection.execute(
                    "CREATE TABLE owner_stop(singleton INTEGER PRIMARY KEY CHECK(singleton=1), active INTEGER NOT NULL CHECK(active IN (0,1)))"
                )
                connection.execute("INSERT INTO owner_stop VALUES (1,1)")
                connection.execute(
                    "CREATE TABLE owner_login_replays(digest TEXT PRIMARY KEY, client_digest TEXT NOT NULL, expires_at INTEGER NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE owner_login_key(secret BLOB NOT NULL CHECK(length(secret)=32))"
                )
                connection.execute(
                    "INSERT INTO owner_login_key VALUES (?)", (secrets.token_bytes(32),)
                )
                connection.execute(
                    "CREATE TABLE owner_operations(operation_id TEXT PRIMARY KEY, session_digest TEXT NOT NULL, expires_at INTEGER NOT NULL, action INTEGER, result TEXT)"
                )
                connection.execute(
                    "CREATE TABLE owner_audit(sequence INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, actor_reference TEXT NOT NULL, action_type TEXT NOT NULL, occurred_at TEXT NOT NULL, result TEXT NOT NULL, rejection_category TEXT NOT NULL)"
                )
        with self._connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS owner_worker_operations(operation_id TEXT PRIMARY KEY, session_digest TEXT NOT NULL, actor_reference TEXT NOT NULL, expires_at INTEGER NOT NULL, application_result TEXT NOT NULL, worker_result TEXT NOT NULL)"
            )

    @contextmanager
    def _connection(self):
        connection = None
        try:
            parent = open_trusted_directory_chain(self.directory)
            try:
                identity = self.path.lstat()
                if (
                    not stat.S_ISREG(identity.st_mode)
                    or identity.st_uid != os.geteuid()
                    or stat.S_IMODE(identity.st_mode) != 0o600
                ):
                    raise RuntimeError("unsafe owner store")
                for suffix in ("-journal", "-wal", "-shm"):
                    sidecar = Path(str(self.path) + suffix)
                    try:
                        status = sidecar.lstat()
                    except FileNotFoundError:
                        # SQLite may remove an optional journal at concurrent commit.
                        # Other filesystem errors and unsafe present entries fail closed.
                        continue
                    if (
                        not stat.S_ISREG(status.st_mode)
                        or status.st_uid != os.geteuid()
                        or stat.S_IMODE(status.st_mode) != 0o600
                    ):
                        raise RuntimeError("unsafe owner sidecar")
                connection = sqlite3.connect(self.path, timeout=2)
                if (self.path.lstat().st_dev, self.path.lstat().st_ino) != (
                    identity.st_dev,
                    identity.st_ino,
                ):
                    raise RuntimeError("owner store changed")
                with connection:
                    yield connection
            finally:
                os.close(parent)
        except PermissionError:
            raise PermissionError("owner access denied") from None
        except (OSError, sqlite3.Error):
            raise RuntimeError("owner store unavailable") from None
        finally:
            if connection is not None:
                connection.close()

    def _login_signature(self, message):
        with self._connection() as connection:
            key = connection.execute("SELECT secret FROM owner_login_key").fetchone()[0]
        return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()

    def login_challenge(self, client):
        # Issuance is read-only: anonymous traffic cannot evict live challenges.
        expires = int(self._clock().timestamp()) + 300
        message = f"{expires}.{secrets.token_hex(16)}.{self._digest(client)}"
        return message + "." + self._login_signature(message)

    def consume_login_challenge(self, token, client):
        if type(token) is not str or len(token) > 256:
            return False
        now = int(self._clock().timestamp())
        try:
            expires, nonce, client_digest, signature = token.split(".")
            expiry = int(expires)
            message = f"{expires}.{nonce}.{client_digest}"
            if (
                not now < expiry <= now + 300
                or client_digest != self._digest(client)
                or not hmac.compare_digest(
                    signature.encode(), self._login_signature(message).encode()
                )
            ):
                return False
        except ValueError:
            return False
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM owner_login_replays WHERE expires_at <= ?", (now,)
            )
            if (
                connection.execute(
                    "SELECT count(*) FROM owner_login_replays WHERE client_digest=?",
                    (client_digest,),
                ).fetchone()[0]
                >= 32
            ):
                return False
            cursor = connection.execute(
                "INSERT OR IGNORE INTO owner_login_replays VALUES (?,?,?)",
                (self._digest(token), client_digest, expiry),
            )
            return cursor.rowcount == 1

    def provision(self, username, password):
        # No supplied conversational principal or owner flag is accepted.
        self._auth.provision_account(
            username, password, "owner:" + secrets.token_hex(16)
        )

    def rotate_password(self, username, password):
        """OS-authenticated recovery preserves owner identity, Stop and audit."""
        self._auth.rotate_password(username, password)

    def login(self, username, password, client):
        return self._auth.login(username, password, client)

    def authenticate(self, token):
        return self._auth.authenticate(token)

    def revoke(self, token):
        return self._auth.revoke(token)

    def active(self):
        """Trusted fail-closed callback; store errors mean stop is active."""
        try:
            with self._connection() as connection:
                return connection.execute(
                    "SELECT active FROM owner_stop WHERE singleton=1"
                ).fetchone() != (0,)
        except (RuntimeError, PermissionError):
            return True

    def issue(self, token):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self.authenticate(token)
            if session is None:
                raise PermissionError("owner authentication required")
            now = int(self._clock().timestamp())
            connection.execute(
                "DELETE FROM owner_operations WHERE expires_at <= ? AND action IS NULL",
                (now,),
            )
            count = connection.execute(
                "SELECT count(*) FROM owner_operations WHERE session_digest=? AND action IS NULL",
                (self._digest(token),),
            ).fetchone()[0]
            if count >= 32:
                raise ValueError("too many pending operations")
            operation = secrets.token_hex(16)
            connection.execute(
                "INSERT INTO owner_operations VALUES (?,?,?,NULL,NULL)",
                (operation, self._digest(token), min(now + 300, session.expires_at)),
            )
            return operation

    def issue_worker_stop(self, token):
        """Create a session bound, single purpose operation before any control IPC."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self.authenticate(token)
            if session is None:
                raise PermissionError("owner authentication required")
            now = int(self._clock().timestamp())
            if (
                connection.execute(
                    "SELECT count(*) FROM owner_worker_operations WHERE session_digest=? AND expires_at>?",
                    (self._digest(token), now),
                ).fetchone()[0]
                >= 32
            ):
                raise ValueError("too many worker operations")
            operation = secrets.token_hex(16)
            connection.execute(
                "INSERT INTO owner_worker_operations VALUES (?,?,?,?,?,?)",
                (
                    operation,
                    self._digest(token),
                    str(session.principal_id),
                    min(now + 300, session.expires_at),
                    "pending",
                    "pending",
                ),
            )
            return operation

    def worker_operation(self, token, operation):
        if type(operation) is not str or len(operation) != 32:
            raise ValueError("invalid worker operation")
        with self._connection() as connection:
            session = self.authenticate(token)
            if session is None:
                raise PermissionError("owner authentication required")
            row = connection.execute(
                "SELECT session_digest, expires_at, application_result, worker_result FROM owner_worker_operations WHERE operation_id=?",
                (operation,),
            ).fetchone()
            if (
                row is None
                or row[0] != self._digest(token)
                or row[1] <= int(self._clock().timestamp())
            ):
                raise ValueError("invalid or expired worker operation")
            return {"application": row[2], "worker": row[3]}

    def latest_worker_stop(self, token):
        session = self.authenticate(token)
        if session is None:
            raise PermissionError("owner authentication required")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT application_result,worker_result FROM owner_worker_operations WHERE actor_reference=? ORDER BY rowid DESC LIMIT 1",
                (str(session.principal_id),),
            ).fetchone()
        return None if row is None else {"application": row[0], "worker": row[1]}

    def record_worker_outcome(self, token, operation, field, result):
        if field not in {"application_result", "worker_result"} or result not in {
            "confirmed",
            "uncertain",
            "unavailable",
        }:
            raise ValueError("invalid worker outcome")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.worker_operation(token, operation)
            cursor = connection.execute(
                f"UPDATE owner_worker_operations SET {field}=? WHERE operation_id=? AND {field}!='confirmed' AND {field}!=?",
                (result, operation, result),
            )
            if cursor.rowcount == 0:
                return
            session = self.authenticate(token)
            connection.execute(
                "INSERT INTO owner_audit(operation_id,actor_reference,action_type,occurred_at,result,rejection_category) VALUES (?,?,?,?,?,?)",
                (
                    operation,
                    str(session.principal_id),
                    "worker_stop" if field == "worker_result" else "application_stop",
                    self._clock().isoformat(),
                    result,
                    "none",
                ),
            )

    def assert_application_stop(self, token, operation):
        self.worker_operation(token, operation)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE owner_stop SET active=1 WHERE singleton=1"
            )
            if cursor.rowcount != 1:
                raise RuntimeError("owner stop state unavailable")

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def change(self, token, operation, active):
        if (
            type(operation) is not str
            or len(operation) != 32
            or type(active) is not bool
        ):
            raise ValueError("invalid owner operation")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self.authenticate(token)
            if session is None:
                raise PermissionError("owner authentication required")
            row = connection.execute(
                "SELECT session_digest, expires_at, action, result FROM owner_operations WHERE operation_id=?",
                (operation,),
            ).fetchone()
            now = self._clock()
            if (
                row is None
                or row[0] != self._digest(token)
                or int(now.timestamp()) >= row[1]
            ):
                result = "rejected"
                category = "invalid_or_expired_operation"
            elif row[2] is not None:
                if row[2] == int(active):
                    return row[3]
                result = "conflict"
                category = "conflicting_replay"
            else:
                result = "applied"
                category = "none"
                cursor = connection.execute(
                    "UPDATE owner_stop SET active=? WHERE singleton=1", (int(active),)
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("owner stop state unavailable")
                connection.execute(
                    "UPDATE owner_operations SET action=?,result=? WHERE operation_id=?",
                    (int(active), result, operation),
                )
            connection.execute(
                "INSERT INTO owner_audit(operation_id,actor_reference,action_type,occurred_at,result,rejection_category) VALUES (?,?,?,?,?,?)",
                (
                    operation if result == "applied" else secrets.token_hex(16),
                    str(session.principal_id),
                    "stop" if active else "resume",
                    now.isoformat(),
                    result,
                    category,
                ),
            )
            return result

    def reject(self, token, action):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self.authenticate(token)
            if session is None:
                raise PermissionError("owner authentication required")
            connection.execute(
                "INSERT INTO owner_audit(operation_id,actor_reference,action_type,occurred_at,result,rejection_category) VALUES (?,?,?,?,?,?)",
                (
                    secrets.token_hex(16),
                    str(session.principal_id),
                    "stop_control"
                    if action == "/stop"
                    else "worker_stop_control"
                    if action in {"/worker-stop-operation", "/emergency-stop"}
                    else "session_control",
                    self._clock().isoformat(),
                    "rejected",
                    "request_admission",
                ),
            )

    def audit(self):
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT operation_id,actor_reference,action_type,occurred_at,result,rejection_category FROM owner_audit ORDER BY sequence DESC LIMIT 50"
            ).fetchall()
        return [
            dict(
                zip(
                    (
                        "operation_id",
                        "actor_reference",
                        "action_type",
                        "occurred_at",
                        "result",
                        "rejection_category",
                    ),
                    row,
                    strict=True,
                )
            )
            for row in rows
        ]
