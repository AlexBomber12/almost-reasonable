"""Bounded, allowlisted owner observations; never return stored private text."""

from datetime import datetime
import hashlib
import os
import sqlite3
import stat
from pathlib import Path
from time import monotonic

from app.core.store import EventStore, EventStoreError

LIMIT = 10
REPLY_OUTCOMES = frozenset(
    {
        "completed",
        "no_action",
        "failed",
        "invalid",
        "unsupported",
        "skipped",
        "unavailable",
        "stopped",
        "rejected",
        "spend_suspended",
    }
)


def reference(value):
    if type(value) is not str or not value or len(value) > 128:
        raise ValueError("invalid diagnostic identity")
    return hashlib.sha256(b"owner-correlation-v1\0" + value.encode()).hexdigest()[:24]


def safe_time(value):
    if type(value) is not str or len(value) > 40:
        raise ValueError("invalid diagnostic time")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or EventStore._serialize_time(parsed) != value:
        raise ValueError("invalid diagnostic time")
    return value


def application_replies(store):
    """Read the latest ten reply operations under the app's existing store boundary."""
    try:
        path = store.database_path
        store._verify_database_parent_security()
        store._verify_database_file_security()
        identity = path.lstat()
        if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
            raise ValueError("unsafe reply database")
        sidecars = _sidecars(path)
        deadline = monotonic() + 0.5
        connection = sqlite3.connect(
            path.absolute().as_uri() + "?mode=ro",
            uri=True,
            timeout=0.2,
            isolation_level=None,
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=200")
            connection.set_progress_handler(
                lambda: 1 if monotonic() > deadline else 0, 1000
            )
            connection.execute("BEGIN")
            EventStore._verify_current_schema(connection)
            rows = connection.execute(
                """SELECT e.occurred_at, o.correlation_id, r.outcome
                   FROM agent_reply_operations AS o
                   JOIN event_envelopes AS e ON e.event_id=o.initiating_event_id
                   LEFT JOIN agent_reply_resolutions AS r ON r.operation_id=o.operation_id
                   ORDER BY e.occurred_at DESC, o.operation_id DESC LIMIT ?""",
                (LIMIT,),
            ).fetchall()
            connection.execute("ROLLBACK")
            current = path.lstat()
            if (identity.st_dev, identity.st_ino) != (
                current.st_dev,
                current.st_ino,
            ) or any(
                before is not None and before != after
                for before, after in zip(sidecars, _sidecars(path), strict=True)
            ):
                raise ValueError("reply source changed")
        finally:
            connection.close()
        return {
            "available": True,
            "rows": [
                {
                    "time": safe_time(time),
                    "family": "private_reply",
                    "outcome": outcome if outcome in REPLY_OUTCOMES else "unknown",
                    "reason": None,
                    "tokens": None,
                    "reference": reference(correlation),
                }
                for time, correlation, outcome in rows
            ],
        }
    except (OSError, sqlite3.Error, EventStoreError, TypeError, ValueError):
        return {"available": False, "rows": []}


def _sidecars(path: Path):
    result = []
    for suffix in ("-wal", "-shm"):
        try:
            entry = Path(f"{path}{suffix}").lstat()
        except FileNotFoundError:
            result.append(None)
            continue
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_uid != os.geteuid()
            or stat.S_IMODE(entry.st_mode) != 0o600
            or entry.st_nlink != 1
        ):
            raise ValueError("unsafe reply database sidecar")
        result.append((entry.st_dev, entry.st_ino))
    return tuple(result)
