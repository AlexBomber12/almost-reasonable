"""Protected INT-22 authority and worker-owned, monotone operation allowance."""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from app.trusted.openai_credential_files import (
    TrustedTransportConfigurationError,
    read_protected_worker_file,
)


ACTIVATION_VERSION = "private-pilot-activation-v1"
OPERATING_GRANT_VERSION = "private-operating-grant-v1"
MAX_OPERATING_OPERATIONS = 2**63 - 1
MEMORY_ACTIVATION_VERSION = "private-pilot-activation-b03d-v1"
MEMORY_TASK = "synthetic-private-robot-memory-b03d"
COMBINED_ACTIVATION_VERSION = "private-pilot-activation-b03e-v1"
COMBINED_TASK = "synthetic-private-combined-memory-b03e"
PROVIDER_CONFIG_VERSION = "openai-private-pilot-v1"
MODEL = "gpt-6-luna"
MAX_OPERATIONS = 5
MAX_MEMORY_ACCEPTANCE_OPERATIONS = 27
MAX_COMBINED_ACCEPTANCE_OPERATIONS = 20
MAX_WINDOW_SECONDS = 600
MAX_MEMORY_ACCEPTANCE_WINDOW_SECONDS = 3600
_FIELDS = frozenset(
    {
        "version",
        "pilot_id",
        "application_peer_uid",
        "provider_config_version",
        "model",
        "project_id",
        "organization_id",
        "not_before",
        "expires_at",
        "max_operations",
        "max_attempts_per_operation",
    }
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SECOND = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


class PilotAuthorityError(RuntimeError):
    """The pilot has no current, intact authority or allowance state."""


def protected_ancestry(path: Path) -> tuple[tuple[int, int], ...]:
    """Inspect every directory component of a worker-owned protected path."""

    if not isinstance(path, Path) or not path.is_absolute() or path == Path("/"):
        raise PilotAuthorityError("invalid protected path")
    directories = tuple(reversed(path.parent.parents)) + (path.parent,)
    identities = []
    try:
        for directory in directories:
            entry = directory.lstat()
            mode = stat.S_IMODE(entry.st_mode)
            sticky_root = entry.st_uid == 0 and mode == 0o1777
            if (
                not stat.S_ISDIR(entry.st_mode)
                or entry.st_uid not in {0, os.geteuid()}
                or ((mode & 0o022) != 0 and not sticky_root)
            ):
                raise PilotAuthorityError("unsafe protected ancestry")
            identities.append((entry.st_dev, entry.st_ino))
        parent = path.parent.lstat()
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise PilotAuthorityError("unsafe protected parent")
    except OSError:
        raise PilotAuthorityError("protected ancestry unavailable") from None
    return tuple(identities)


def protected_state_identity(
    path: Path,
) -> tuple[tuple[tuple[int, int], ...], int, int]:
    """Pin a worker-owned 0600 regular state file and its ancestry."""

    ancestry = protected_ancestry(path)
    try:
        entry = path.lstat()
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_uid != os.geteuid()
            or stat.S_IMODE(entry.st_mode) != 0o600
            or entry.st_nlink != 1
        ):
            raise PilotAuthorityError("unsafe protected state")
    except OSError:
        raise PilotAuthorityError("protected state unavailable") from None
    if protected_ancestry(path) != ancestry:
        raise PilotAuthorityError("protected ancestry changed")
    return ancestry, entry.st_dev, entry.st_ino


def read_pilot_control(path: Path, *, maximum_bytes: int) -> bytes:
    """Apply the INT-22 ancestry policy around the shared protected-file reader."""

    ancestry = protected_ancestry(path)
    payload = read_protected_worker_file(path, maximum_bytes=maximum_bytes)
    if protected_ancestry(path) != ancestry:
        raise PilotAuthorityError("protected ancestry changed")
    return payload


@dataclass(frozen=True, slots=True)
class PilotActivation:
    pilot_id: str
    application_peer_uid: int
    project_id: str
    organization_id: str
    not_before: datetime
    expires_at: datetime
    max_operations: int
    digest: str
    memory_acceptance: bool = False
    combined_acceptance: bool = False
    ordinary: bool = False


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate activation field")
        result[key] = value
    return result


def _utc_second(value: object) -> datetime:
    if type(value) is not str or _SECOND.fullmatch(value) is None:
        raise ValueError("invalid activation timestamp")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def load_activation(
    path: Path,
    *,
    now: datetime | None = None,
    allow_memory_acceptance: bool = False,
    require_current: bool = True,
) -> PilotActivation:
    """Read an exact worker-owned 0400 artifact on every authorization check."""

    try:
        payload = read_pilot_control(path, maximum_bytes=4096)
        value = json.loads(payload, object_pairs_hook=_unique)
        if type(value) is not dict or type(allow_memory_acceptance) is not bool:
            raise ValueError("invalid activation fields")
        ordinary = value.get("version") == OPERATING_GRANT_VERSION
        combined_acceptance = (
            allow_memory_acceptance
            and set(value) == _FIELDS | {"task"}
            and value.get("version") == COMBINED_ACTIVATION_VERSION
            and value.get("task") == COMBINED_TASK
        )
        memory_acceptance = combined_acceptance or (
            allow_memory_acceptance
            and set(value) == _FIELDS | {"task"}
            and value.get("version") == MEMORY_ACTIVATION_VERSION
            and value.get("task") == MEMORY_TASK
        )
        if not memory_acceptance and value.keys() != _FIELDS:
            raise ValueError("invalid activation fields")
        start = _utc_second(value["not_before"])
        end = _utc_second(value["expires_at"])
        current = datetime.now(UTC) if now is None else now
        if type(current) is not datetime or current.tzinfo is None:
            raise ValueError("invalid current time")
        valid = (
            value["version"]
            == (
                OPERATING_GRANT_VERSION
                if ordinary
                else COMBINED_ACTIVATION_VERSION
                if combined_acceptance
                else MEMORY_ACTIVATION_VERSION
                if memory_acceptance
                else ACTIVATION_VERSION
            )
            and type(value["pilot_id"]) is str
            and _IDENTIFIER.fullmatch(value["pilot_id"]) is not None
            and type(value["application_peer_uid"]) is int
            and 0 <= value["application_peer_uid"] <= 2**32 - 2
            and value["provider_config_version"] == PROVIDER_CONFIG_VERSION
            and value["model"] == MODEL
            and type(value["project_id"]) is str
            and value["project_id"].startswith("proj_")
            and _IDENTIFIER.fullmatch(value["project_id"]) is not None
            and type(value["organization_id"]) is str
            and value["organization_id"].startswith("org-")
            and _IDENTIFIER.fullmatch(value["organization_id"]) is not None
            and type(value["max_operations"]) is int
            and 1
            <= value["max_operations"]
            <= (
                MAX_OPERATING_OPERATIONS
                if ordinary
                else MAX_COMBINED_ACCEPTANCE_OPERATIONS
                if combined_acceptance
                else MAX_MEMORY_ACCEPTANCE_OPERATIONS
                if memory_acceptance
                else MAX_OPERATIONS
            )
            and (
                not combined_acceptance
                or value["max_operations"] == MAX_COMBINED_ACCEPTANCE_OPERATIONS
            )
            and value["max_attempts_per_operation"] == 1
            and type(value["max_attempts_per_operation"]) is int
            and start < end
            and (
                ordinary
                or (end - start).total_seconds()
                <= (
                    MAX_MEMORY_ACCEPTANCE_WINDOW_SECONDS
                    if memory_acceptance
                    else MAX_WINDOW_SECONDS
                )
            )
            and (not require_current or start <= current.astimezone(UTC) < end)
        )
        if not valid:
            raise ValueError("invalid or stale activation")
        return PilotActivation(
            value["pilot_id"],
            value["application_peer_uid"],
            value["project_id"],
            value["organization_id"],
            start,
            end,
            value["max_operations"],
            sha256(payload).hexdigest(),
            memory_acceptance,
            combined_acceptance,
            ordinary,
        )
    except (
        PilotAuthorityError,
        TrustedTransportConfigurationError,
        UnicodeDecodeError,
        ValueError,
        TypeError,
    ):
        raise PilotAuthorityError("private pilot activation is unavailable") from None


class PilotAllowance:
    """Reserve each logical operation durably before the gateway can contact SDK."""

    def __init__(self, path: Path, activation: PilotActivation):
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or type(activation) is not PilotActivation
        ):
            raise PilotAuthorityError("invalid allowance configuration")
        self.path = path
        self.activation = activation
        self._identity = protected_state_identity(path)
        self.remaining()

    @classmethod
    def initialize(cls, path: Path, activation: PilotActivation) -> "PilotAllowance":
        """One-time operator preparation; never called by worker startup."""

        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or type(activation) is not PilotActivation
        ):
            raise PilotAuthorityError("invalid allowance configuration")
        try:
            protected_ancestry(path)
            if path.exists() or path.is_symlink():
                raise PilotAuthorityError("unsafe allowance location")
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC, 0o600)
            os.close(fd)
            with sqlite3.connect(path) as connection:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(
                    "CREATE TABLE authority (digest TEXT NOT NULL, max_operations INTEGER NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE consumed (operation_id TEXT PRIMARY KEY)"
                )
                connection.execute(
                    "INSERT INTO authority VALUES (?, ?)",
                    (activation.digest, activation.max_operations),
                )
            if activation.ordinary:
                cls.replace(path, activation)
            return cls(path, activation)
        except (OSError, sqlite3.Error):
            raise PilotAuthorityError("allowance initialization failed") from None

    def _inspect_file(self) -> None:
        if protected_state_identity(self.path) != self._identity:
            raise PilotAuthorityError("allowance location changed")

    def _transact(
        self,
        operation_id: str | None,
        extraction_limit: int | None = None,
        reserve_reply: bool = False,
    ) -> int | bool:
        if self.activation.ordinary:
            return self._operating_transact(
                operation_id, extraction_limit, reserve_reply
            )
        self._inspect_file()
        try:
            with sqlite3.connect(
                f"file:{self.path}?mode=rw", uri=True, timeout=5
            ) as connection:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                authority = connection.execute(
                    "SELECT digest, max_operations FROM authority"
                ).fetchall()
                if authority != [
                    (self.activation.digest, self.activation.max_operations)
                ]:
                    raise PilotAuthorityError("allowance binding changed")
                used = connection.execute("SELECT count(*) FROM consumed").fetchone()[0]
                result = max(0, self.activation.max_operations - used)
                if operation_id is not None:
                    if self._has_operating_grants(connection):
                        return False
                    result = False
                    reply_hold = "reply_hold:" + operation_id.removeprefix("reply:")
                    if (
                        operation_id.startswith("reply:")
                        and connection.execute(
                            "SELECT 1 FROM consumed WHERE operation_id=?", (reply_hold,)
                        ).fetchone()
                    ):
                        connection.execute(
                            "DELETE FROM consumed WHERE operation_id=?", (reply_hold,)
                        )
                        connection.execute(
                            "INSERT INTO consumed VALUES (?)", (operation_id,)
                        )
                        result = True
                    elif (
                        used + (2 if reserve_reply else 1)
                        <= self.activation.max_operations
                    ):
                        extracted = (
                            0
                            if extraction_limit is None
                            else connection.execute(
                                "SELECT count(*) FROM consumed WHERE operation_id LIKE 'memory:%' OR operation_id LIKE 'evolution:%' OR operation_id LIKE 'typed:%' OR operation_id LIKE 'sharedgen:%' OR operation_id LIKE 'narrative:%'"
                            ).fetchone()[0]
                        )
                        if extraction_limit is None or extracted < extraction_limit:
                            existing = connection.execute(
                                "SELECT 1 FROM consumed WHERE operation_id=?",
                                (operation_id,),
                            ).fetchone()
                            if existing is None:
                                connection.execute(
                                    "INSERT INTO consumed VALUES (?)", (operation_id,)
                                )
                                if reserve_reply:
                                    connection.execute(
                                        "INSERT INTO consumed VALUES (?)",
                                        (
                                            "reply_hold:"
                                            + operation_id.removeprefix("narrative:"),
                                        ),
                                    )
                                result = True
            self._inspect_file()
            return result
        except sqlite3.Error:
            raise PilotAuthorityError("allowance state unavailable") from None

    @staticmethod
    def _has_operating_grants(connection: sqlite3.Connection) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operating_grants'"
            ).fetchone()
            is not None
        )

    @classmethod
    def replace(cls, path: Path, activation: PilotActivation) -> None:
        """Explicit operator selection; retain legacy rows and retire prior grants."""
        if type(activation) is not PilotActivation or not activation.ordinary:
            raise PilotAuthorityError("ordinary grant required")
        identity = protected_state_identity(path)
        try:
            with sqlite3.connect(path.as_uri() + "?mode=rw", uri=True) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                # Require an existing allowance, never a substitute empty database.
                if (
                    len(
                        db.execute(
                            "SELECT digest, max_operations FROM authority"
                        ).fetchall()
                    )
                    != 1
                ):
                    raise PilotAuthorityError("invalid historical allowance")
                db.execute("SELECT operation_id FROM consumed LIMIT 1")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS operating_grants (digest TEXT PRIMARY KEY, pilot_id TEXT UNIQUE NOT NULL, max_operations INTEGER NOT NULL, last_seen TEXT NOT NULL, expired INTEGER NOT NULL, retired INTEGER NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS operating_consumed (operation_id TEXT PRIMARY KEY, digest TEXT NOT NULL)"
                )
                if db.execute(
                    "SELECT 1 FROM operating_grants WHERE digest=? OR pilot_id=?",
                    (activation.digest, activation.pilot_id),
                ).fetchone():
                    raise PilotAuthorityError("grant already prepared or retired")
                db.execute("UPDATE operating_grants SET retired=1")
                db.execute(
                    "INSERT INTO operating_grants VALUES (?, ?, ?, ?, 0, 0)",
                    (
                        activation.digest,
                        activation.pilot_id,
                        activation.max_operations,
                        activation.not_before.isoformat(),
                    ),
                )
                if protected_state_identity(path) != identity:
                    raise PilotAuthorityError("allowance location changed")
        except sqlite3.Error:
            raise PilotAuthorityError("grant preparation failed") from None

    def _operating_transact(
        self,
        operation_id: str | None,
        extraction_limit: int | None = None,
        reserve_reply: bool = False,
        *,
        now: datetime | None = None,
        observe: bool = True,
    ) -> int | bool:
        self._inspect_file()
        try:
            with sqlite3.connect(
                self.path.as_uri() + "?mode=rw", uri=True, timeout=5
            ) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT pilot_id, max_operations, last_seen, expired, retired FROM operating_grants WHERE digest=?",
                    (self.activation.digest,),
                ).fetchone()
                if row is None or row[:2] != (
                    self.activation.pilot_id,
                    self.activation.max_operations,
                ):
                    raise PilotAuthorityError("allowance binding changed")
                pilot_id, maximum, last_seen, expired, retired = row
                if now is not None:
                    instant = now.astimezone(UTC)
                    watermark = datetime.fromisoformat(last_seen)
                    expired = expired or instant >= self.activation.expires_at
                    if observe:
                        db.execute(
                            "UPDATE operating_grants SET last_seen=?, expired=? WHERE digest=?",
                            (
                                max(instant, watermark).isoformat(),
                                int(expired),
                                self.activation.digest,
                            ),
                        )
                    result = (
                        not retired
                        and not expired
                        and watermark <= instant < self.activation.expires_at
                    )
                else:
                    used = db.execute(
                        "SELECT count(*) FROM operating_consumed WHERE digest=?",
                        (self.activation.digest,),
                    ).fetchone()[0]
                    result = max(0, maximum - used)
                    if operation_id is not None:
                        result = False
                        hold = "reply_hold:" + operation_id.removeprefix("reply:")
                        suffix = operation_id.split(":", 1)[1]
                        existing = db.execute(
                            "SELECT 1 FROM consumed WHERE operation_id IN (?, ?) UNION ALL SELECT 1 FROM operating_consumed WHERE operation_id IN (?, ?)",
                            (
                                operation_id,
                                "reply_hold:" + suffix,
                                operation_id,
                                "reply_hold:" + suffix,
                            ),
                        ).fetchone()
                        own_hold = (
                            operation_id.startswith("reply:")
                            and db.execute(
                                "SELECT 1 FROM operating_consumed WHERE operation_id=? AND digest=?",
                                (hold, self.activation.digest),
                            ).fetchone()
                            is not None
                        )
                        extracted = db.execute(
                            "SELECT count(*) FROM operating_consumed WHERE digest=? AND operation_id NOT LIKE 'reply:%' AND operation_id NOT LIKE 'reply_hold:%'",
                            (self.activation.digest,),
                        ).fetchone()[0]
                        if not retired and not expired:
                            if own_hold:
                                db.execute(
                                    "DELETE FROM operating_consumed WHERE operation_id=?",
                                    (hold,),
                                )
                                db.execute(
                                    "INSERT INTO operating_consumed VALUES (?, ?)",
                                    (operation_id, self.activation.digest),
                                )
                                result = True
                            elif (
                                existing is None
                                and used + (2 if reserve_reply else 1) <= maximum
                                and (
                                    extraction_limit is None
                                    or extracted < extraction_limit
                                )
                            ):
                                db.execute(
                                    "INSERT INTO operating_consumed VALUES (?, ?)",
                                    (operation_id, self.activation.digest),
                                )
                                if reserve_reply:
                                    db.execute(
                                        "INSERT INTO operating_consumed VALUES (?, ?)",
                                        (
                                            "reply_hold:" + suffix,
                                            self.activation.digest,
                                        ),
                                    )
                                result = True
            self._inspect_file()
            return result
        except (sqlite3.Error, ValueError):
            raise PilotAuthorityError("operating allowance state unavailable") from None

    def current(self, now: datetime, *, observe: bool = True) -> bool:
        """Persist time high-water/expiry even on rejection, shared across processes."""
        if self.activation.ordinary:
            return bool(self._operating_transact(None, now=now, observe=observe))
        self._inspect_file()
        try:
            with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as db:
                return not self._has_operating_grants(db)
        except sqlite3.Error:
            raise PilotAuthorityError("allowance state unavailable") from None

    def remaining(self) -> int:
        return self._transact(None)

    def reserve(
        self,
        operation_id: str,
        *,
        extraction_limit: int | None = None,
        reserve_reply: bool = False,
    ) -> bool:
        if (
            type(operation_id) is not str
            or re.fullmatch(
                r"(?:memory|evolution|typed|sharedgen|narrative):[0-9a-f]{64}"
                if extraction_limit is not None
                else r"reply:[0-9a-f]{64}",
                operation_id,
            )
            is None
            or (
                extraction_limit is not None
                and (
                    type(extraction_limit) is not int
                    or not 1 <= extraction_limit <= self.activation.max_operations
                )
            )
            or type(reserve_reply) is not bool
            or (reserve_reply and not operation_id.startswith("narrative:"))
        ):
            raise PilotAuthorityError("invalid pilot operation")
        return self._transact(operation_id, extraction_limit, reserve_reply)
