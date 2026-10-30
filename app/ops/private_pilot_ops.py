"""Readiness and one-time allowance preparation without provider credentials."""

import argparse
from contextlib import closing
from datetime import UTC, datetime
import sqlite3
import json
from pathlib import Path

from app.core.store import EventStore, EventStoreError
from app.trusted.private_pilot import (
    PilotAllowance,
    PilotAuthorityError,
    load_activation,
)
from app.trusted.private_pilot_worker import PilotStop, _require_provider_database


def plan(activation_path: Path, *, now: datetime | None = None) -> dict[str, object]:
    """Inspect a candidate offline; install nothing and require no database."""
    activation = load_activation(activation_path, now=now, require_current=False)
    current = datetime.now(UTC) if now is None else now
    return {
        "pilot_id": activation.pilot_id,
        "model": activation.model,
        "digest": activation.digest,
        "ordinary": activation.ordinary,
        "not_before": activation.not_before.isoformat(),
        "expires_at": activation.expires_at.isoformat(),
        "max_operations": activation.max_operations,
        "current": activation.not_before <= current < activation.expires_at,
    }


def replace(activation_path: Path, allowance_path: Path) -> None:
    """Explicitly select a separately approved grant in the retained allowance."""
    PilotAllowance.replace(
        allowance_path, load_activation(activation_path, require_current=False)
    )


def prepare(
    activation_path: Path, allowance_path: Path, *, memory_acceptance: bool = False
) -> None:
    activation = load_activation(
        activation_path,
        allow_memory_acceptance=memory_acceptance,
        require_current=False,
    )
    if not activation.ordinary:
        load_activation(activation_path, allow_memory_acceptance=memory_acceptance)
    if memory_acceptance and not activation.memory_acceptance:
        raise PilotAuthorityError("memory acceptance activation required")
    PilotAllowance.initialize(allowance_path, activation)


def inspect(
    activation_path: Path,
    allowance_path: Path,
    stop_path: Path,
    provider_database: Path,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Read worker-owned readiness state without opening credentials or changing controls."""

    activation = load_activation(activation_path, require_current=False)
    allowance = PilotAllowance(allowance_path, activation)
    remaining = allowance.remaining()
    instant = datetime.now(UTC) if now is None else now
    current = activation.not_before <= instant < activation.expires_at
    current = allowance.current(instant, observe=False) and current
    _require_provider_database(provider_database)
    try:
        with closing(
            sqlite3.connect(provider_database.as_uri() + "?mode=ro", uri=True)
        ) as db:
            EventStore._verify_current_schema(db)
            suspended = (
                db.execute(
                    "SELECT 1 FROM provider_spend_suspensions WHERE provider='openai' LIMIT 1"
                ).fetchone()
                is not None
            )
    except (sqlite3.Error, EventStoreError):
        raise PilotAuthorityError("provider state unavailable") from None
    stopped = PilotStop(stop_path).active()
    return {
        "pilot_id": activation.pilot_id,
        "model": activation.model,
        "application_peer_uid": activation.application_peer_uid,
        "expires_at": activation.expires_at.isoformat(),
        "remaining_operations": remaining,
        "worker_stopped": stopped,
        "worker_spend_suspended": suspended,
        "authority_current": current,
        "ready": current and remaining > 0 and not stopped and not suspended,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or prepare INT-22 pilot allowance"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan")
    planning.add_argument("--activation", required=True, type=Path)
    for name in ("prepare", "replace", "inspect"):
        command = commands.add_parser(name)
        command.add_argument("--activation", required=True, type=Path)
        command.add_argument("--allowance", required=True, type=Path)
        if name == "prepare":
            command.add_argument("--memory-acceptance", action="store_true")
        if name == "inspect":
            command.add_argument("--stop", required=True, type=Path)
            command.add_argument("--provider-database", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            print(json.dumps(plan(args.activation)))
        elif args.command == "replace":
            replace(args.activation, args.allowance)
            print("Operating grant selected; historical consumption and Stop retained.")
        elif args.command == "prepare":
            prepare(
                args.activation,
                args.allowance,
                memory_acceptance=args.memory_acceptance,
            )
            print("Pilot allowance prepared; worker stop unchanged.")
        else:
            print(
                json.dumps(
                    inspect(
                        args.activation,
                        args.allowance,
                        args.stop,
                        args.provider_database,
                    )
                )
            )
    except PilotAuthorityError as error:
        parser.exit(1, f"Readiness failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
