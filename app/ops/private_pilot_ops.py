"""Readiness and one-time allowance preparation without provider credentials."""

import argparse
import json
from pathlib import Path

from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.trusted.private_pilot import (
    PilotAllowance,
    PilotAuthorityError,
    load_activation,
)
from app.trusted.private_pilot_worker import PilotStop, _require_provider_database


def prepare(activation_path: Path, allowance_path: Path) -> None:
    PilotAllowance.initialize(allowance_path, load_activation(activation_path))


def inspect(
    activation_path: Path,
    allowance_path: Path,
    stop_path: Path,
    provider_database: Path,
) -> dict[str, object]:
    """Read worker-owned readiness state without opening credentials or changing controls."""

    activation = load_activation(activation_path)
    remaining = PilotAllowance(allowance_path, activation).remaining()
    _require_provider_database(provider_database)
    suspended = ProviderStateStore(EventStore(provider_database)).is_spend_suspended()
    stopped = PilotStop(stop_path).active()
    return {
        "pilot_id": activation.pilot_id,
        "application_peer_uid": activation.application_peer_uid,
        "expires_at": activation.expires_at.isoformat(),
        "remaining_operations": remaining,
        "worker_stopped": stopped,
        "worker_spend_suspended": suspended,
        "ready": remaining > 0 and not stopped and not suspended,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or prepare INT-22 pilot allowance"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "inspect"):
        command = commands.add_parser(name)
        command.add_argument("--activation", required=True, type=Path)
        command.add_argument("--allowance", required=True, type=Path)
        if name == "inspect":
            command.add_argument("--stop", required=True, type=Path)
            command.add_argument("--provider-database", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args.activation, args.allowance)
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
