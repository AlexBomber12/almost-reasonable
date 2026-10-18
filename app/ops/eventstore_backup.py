"""Owner-operated EventStore backup and quarantined restore rehearsal."""

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import stat

from app.core.store import SCHEMA_VERSION
from app.privacy.application import EventStoreRestoreAdapter
from app.privacy.ledger import PrivacyLedger
from app.privacy.recovery import (
    BackupError,
    QuarantinedRestore,
    _validate_existing_file,
    create_sqlite_backup,
)


class _SanitizedParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(
            json.dumps(
                {"status": "invalid_arguments", "reason_code": "invalid_arguments"}
            )
        )
        self.exit(2)


def _require_current_ledger(path: Path) -> PrivacyLedger:
    """Do not let PrivacyLedger initialize a missing authority file."""
    _validate_existing_file(path, "current ledger")
    if stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) & 0o077:
        raise BackupError("current ledger permissions are unsafe")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists() or sidecar.is_symlink():
            _validate_existing_file(sidecar, "current ledger sidecar")
            if stat.S_IMODE(sidecar.stat(follow_symlinks=False).st_mode) & 0o077:
                raise BackupError("current ledger sidecar permissions are unsafe")
    return PrivacyLedger(path)


def main(argv: list[str] | None = None) -> int:
    parser = _SanitizedParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser(
        "backup", help="Create a WAL-consistent EventStore snapshot"
    )
    backup.add_argument("--source", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)
    restore = commands.add_parser(
        "restore", help="Reconcile a backup into a new quarantine"
    )
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--ledger", type=Path, required=True)
    restore.add_argument("--quarantine", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            create_sqlite_backup(
                args.source, args.destination, SCHEMA_VERSION, datetime.now(UTC)
            )
            status, reason = "backup_created", "ok"
        else:
            ledger = _require_current_ledger(args.ledger)
            result = QuarantinedRestore(
                ledger, SCHEMA_VERSION, EventStoreRestoreAdapter
            ).restore(args.backup, args.quarantine, datetime.now(UTC))
            if result.activation_eligible:
                status, reason = "quarantine_verified", "ok"
            else:
                status, reason = "quarantine_blocked", "reconciliation_blocked"
    except Exception:
        status = "backup_failed" if args.command == "backup" else "restore_failed"
        reason = status
    print(json.dumps({"status": status, "reason_code": reason}))
    return 0 if reason == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
