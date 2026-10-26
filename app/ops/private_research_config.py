"""Derive a research app command from protected resolved Compose JSON, offline."""

import argparse
import json
import os
from pathlib import Path
import sys

from app.core.provider_contract import require_identifier


def extension_command(config, scope):
    """Preserve the effective app flags and leave inference authority untouched."""
    require_identifier("application scope", scope)
    try:
        app = config["services"]["private-app"]
        worker = config["services"]["provider-worker"]
        command = app["command"]
        provider_command = worker["command"]
        if (
            app.get("entrypoint")
            or app["user"] != "11022:11022"
            or worker["user"] != "11021:11021"
            or type(command) is not list
            or command[:5] != ["python", "-B", "-m", "app.private_web.cli", "serve"]
            or "--worker-socket" not in command
            or any("public-research" in item for item in command)
            or provider_command[provider_command.index("--provider-database") + 1]
            != "/var/lib/pilot-worker/provider.sqlite3"
        ):
            raise ValueError("incompatible existing pilot configuration")
    except (KeyError, TypeError, IndexError, AttributeError):
        raise ValueError("resolved ordinary pilot configuration required") from None
    return {
        "services": {
            "private-app": {
                "command": command
                + [
                    "--enable-public-research",
                    "--public-research-socket",
                    "/run/pilot-research-ipc/research.sock",
                    "--public-research-worker-uid",
                    "11021",
                    "--public-research-profile",
                    "/run/pilot-research-application/profile.json",
                    "--public-research-scope",
                    scope,
                ]
            }
        }
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = extension_command(json.load(sys.stdin), args.scope)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(result, output, indent=2)
        output.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
