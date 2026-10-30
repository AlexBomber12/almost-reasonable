"""Offline plan/report commands. No provider calls or runtime authorization."""

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys

from acceptance.manifest import load_manifest
from acceptance.run import git, source_digest
from app.core.real_acceptance import (
    BINDING_FIELDS,
    _binding,
    _fields,
    assess,
    build_plan,
    decode,
)
from app.trusted.openai_credential_files import read_protected_worker_file
from app.trusted.openai_transport import TrustedTransportConfigurationError


def _read(path, maximum=65536):
    return read_protected_worker_file(Path(path), maximum_bytes=maximum)


def main(argv=None, *, clock=lambda: datetime.now(UTC)):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "report"))
    parser.add_argument(
        "--runtime", required=True, help="protected reviewed runtime digest manifest"
    )
    parser.add_argument("--expires-at")
    parser.add_argument("--trials", type=int)
    parser.add_argument("--calibration-digest")
    parser.add_argument("--plan")
    parser.add_argument("--approval")
    parser.add_argument("--ci")
    parser.add_argument("--evidence", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        runtime = decode(_read(args.runtime))
        _fields(
            runtime,
            {"version", "configuration_digest", "model_digest", "account_digest"},
        )
        if runtime["version"] != "real-acceptance-runtime-v1":
            raise ValueError("unknown runtime binding")
        binding = _binding(
            {
                **{
                    key: runtime[key]
                    for key in BINDING_FIELDS - {"head", "source_digest"}
                },
                "head": git("rev-parse", "HEAD"),
                "source_digest": source_digest(),
            }
        )
        clean = not git("status", "--porcelain")
        manifest, now = load_manifest(), clock()
        if args.command == "plan":
            if (
                not args.expires_at
                or args.plan
                or args.approval
                or args.ci
                or args.evidence
            ):
                raise ValueError("plan requires expiry and no imported evidence")
            result = build_plan(
                manifest,
                binding,
                at=now,
                expires_at=args.expires_at,
                trial_count=args.trials,
                calibration_digest=args.calibration_digest,
                working_tree_clean=clean,
            )
            status = 2  # A preregistration is not acceptance.
        else:
            if (
                not args.plan
                or args.expires_at
                or args.trials is not None
                or args.calibration_digest
                or len(args.evidence) > 512
            ):
                raise ValueError("report requires a frozen plan")
            result = assess(
                decode(_read(args.plan)),
                manifest,
                ci=None if args.ci is None else _read(args.ci, 4194304),
                evidence=[_read(path) for path in args.evidence],
                approval=None
                if args.approval is None
                else decode(_read(args.approval, 4194304)),
                current=binding,
                at=now,
                working_tree_clean=clean,
            )
            status = {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[result["complete_mvp"]]
        if (
            source_digest() != binding["source_digest"]
            or git("rev-parse", "HEAD") != binding["head"]
            or (not git("status", "--porcelain")) != clean
        ):
            raise ValueError("source changed during evidence import")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return status
    except (
        ValueError,
        TypeError,
        KeyError,
        OSError,
        subprocess.SubprocessError,
        TrustedTransportConfigurationError,
    ):
        print(
            "Acceptance input rejected; inspect protected files locally.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
