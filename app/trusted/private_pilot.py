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
PROVIDER_CONFIG_VERSION = "openai-private-pilot-v1"
MODEL = "gpt-6-luna"
MAX_OPERATIONS = 5
MAX_WINDOW_SECONDS = 600
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


def load_activation(path: Path, *, now: datetime | None = None) -> PilotActivation:
    """Read an exact worker-owned 0400 artifact on every authorization check."""

    try:
        payload = read_protected_worker_file(path, maximum_bytes=4096)
        value = json.loads(payload, object_pairs_hook=_unique)
        if type(value) is not dict or value.keys() != _FIELDS:
            raise ValueError("invalid activation fields")
        start = _utc_second(value["not_before"])
        end = _utc_second(value["expires_at"])
        current = datetime.now(UTC) if now is None else now
        if type(current) is not datetime or current.tzinfo is None:
            raise ValueError("invalid current time")
        valid = (
            value["version"] == ACTIVATION_VERSION
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
            and 1 <= value["max_operations"] <= MAX_OPERATIONS
            and value["max_attempts_per_operation"] == 1
            and type(value["max_attempts_per_operation"]) is int
            and 0 < (end - start).total_seconds() <= MAX_WINDOW_SECONDS
            and start <= current.astimezone(UTC) < end
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
        )
    except (
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
        self._inspect_file()
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
            parent = path.parent.lstat()
            if (
                not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.geteuid()
                or stat.S_IMODE(parent.st_mode) != 0o700
                or path.exists()
                or path.is_symlink()
            ):
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
            return cls(path, activation)
        except (OSError, sqlite3.Error):
            raise PilotAuthorityError("allowance initialization failed") from None

    def _inspect_file(self) -> None:
        try:
            parent = self.path.parent.lstat()
            entry = self.path.lstat()
            if (
                not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.geteuid()
                or stat.S_IMODE(parent.st_mode) != 0o700
                or not stat.S_ISREG(entry.st_mode)
                or entry.st_uid != os.geteuid()
                or stat.S_IMODE(entry.st_mode) != 0o600
                or entry.st_nlink != 1
            ):
                raise PilotAuthorityError("unsafe allowance file")
        except OSError:
            raise PilotAuthorityError("allowance file unavailable") from None

    def _transact(self, operation_id: str | None) -> int | bool:
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
                if operation_id is None:
                    return max(0, self.activation.max_operations - used)
                if used >= self.activation.max_operations:
                    return False
                existing = connection.execute(
                    "SELECT 1 FROM consumed WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if existing is not None:
                    return False
                connection.execute("INSERT INTO consumed VALUES (?)", (operation_id,))
                return True
        except sqlite3.Error:
            raise PilotAuthorityError("allowance state unavailable") from None

    def remaining(self) -> int:
        return self._transact(None)

    def reserve(self, operation_id: str) -> bool:
        if (
            type(operation_id) is not str
            or re.fullmatch(r"reply:[0-9a-f]{64}", operation_id) is None
        ):
            raise PilotAuthorityError("invalid pilot operation")
        return self._transact(operation_id)
