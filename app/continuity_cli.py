"""Explicit local operator interface; no HTTP server, authentication or deployment."""

import argparse
import json
from pathlib import Path

from app.continuity_experiment import Lab, LabError
from app.continuity_sol import Sol, decode


class Recorded:
    kind = "recorded"

    def __init__(self, path):
        self.path = Path(path)

    def complete(self, frame):
        return decode(self.path.read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "event", "context", "step", "inspect", "recover"):
        command = commands.add_parser(name)
        command.add_argument("root")
        if name == "event":
            command.add_argument("id")
            command.add_argument("scope")
            command.add_argument("kind")
            command.add_argument("text")
            command.add_argument("--source", default="")
            command.add_argument("--parent")
        if name in {"context", "step"}:
            if name == "step":
                command.add_argument("operation")
                mode = command.add_mutually_exclusive_group(required=True)
                mode.add_argument("--response")
                mode.add_argument("--live-config")
                command.add_argument("--allow-paid", action="store_true")
            command.add_argument("event")
        if name == "inspect":
            command.add_argument("scope")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            lab = Lab.create(args.root)
            result = dict(version=lab.inspect("public")["version"], live_calls=0)
        else:
            lab = Lab(args.root)
            if args.command == "event":
                lab.add(
                    args.id,
                    args.scope,
                    args.kind,
                    args.text,
                    source=args.source,
                    parent=args.parent,
                )
                result = dict(stored=True)
            elif args.command == "context":
                result = lab.context(args.event)
            elif args.command == "inspect":
                result = lab.inspect(args.scope)
            elif args.command == "recover":
                result = dict(interrupted=lab.recover())
            else:
                if args.live_config and not args.allow_paid:
                    raise LabError("paid_activation_required")
                model = (
                    Sol.from_config(args.live_config, args.root)
                    if args.live_config
                    else Recorded(args.response)
                )
                result = lab.step(args.operation, args.event, model)
        print(json.dumps(result, ensure_ascii=False))
        return int(result.get("status") in {"failed", "unknown"})
    except (OSError, ValueError) as error:
        print(
            json.dumps(
                dict(
                    status="failed",
                    error=str(error) if isinstance(error, LabError) else "unavailable",
                )
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
