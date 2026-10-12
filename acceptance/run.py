"""Run the unchanged full gate plus traceability; emit evidence from that run."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from importlib.metadata import version

from acceptance.behavior import evaluate
from acceptance.manifest import ROOT, load_manifest
from acceptance.report import build_report, readable


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def source_digest():
    paths = git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    digest = hashlib.sha256()
    for name in sorted(paths):
        path = ROOT / name
        digest.update(name.encode() + b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<absent>")
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".artifacts/stage0")
    args = parser.parse_args()
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=True)
    # Never consume old execution/coverage evidence on a failed or interrupted run.
    for name in ("tests.json", "coverage.json", "evidence.json", "report.md"):
        (output / name).unlink(missing_ok=True)
    metadata = {
        "head": git("rev-parse", "HEAD"),
        "base": load_manifest()["base_sha"],
        "origin_main": git("rev-parse", "origin/main"),
        "source_digest": source_digest(),
        "working_tree": "modified" if git("status", "--porcelain") else "clean",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "tools": {
            name: version(name) for name in ("pytest", "pytest-cov", "coverage", "ruff")
        },
        "configuration": load_manifest()["configuration"],
        "baseline": load_manifest()["baseline"],
        "command": "scripts/ci.sh",
    }
    env = {
        **os.environ,
        "INT10_TEST_RESULTS": str(output / "tests.json"),
        "INT10_COVERAGE_RESULTS": str(output / "coverage.json"),
    }
    with (output / "gate.log").open("w") as log:
        gate = subprocess.run(
            ["bash", "scripts/ci.sh"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    if not (output / "tests.json").exists():
        # Ruff/collection interruption is a failed gate, not zero successful tests.
        report = {
            "readiness": "FAIL",
            "deterministic_gate": "FAIL",
            "metadata": metadata,
            "reason": "gate produced no test outcomes; inspect gate.log",
        }
        (output / "evidence.json").write_text(json.dumps(report, indent=2))
        (output / "report.md").write_text(
            "# Stage 0 execution evidence\n\nReadiness: FAIL. No test execution evidence; inspect gate.log.\n"
        )
        return 1
    if (
        source_digest() != metadata["source_digest"]
        or git("rev-parse", "HEAD") != metadata["head"]
    ):
        gate_status = 1
    else:
        gate_status = gate.returncode
    try:
        report = build_report(
            load_manifest(),
            json.loads((output / "tests.json").read_text()),
            json.loads((output / "coverage.json").read_text())
            if (output / "coverage.json").exists()
            else {},
            evaluate(),
            metadata,
            gate_status,
        )
    except ValueError as error:
        report = {
            "readiness": "FAIL",
            "deterministic_gate": "FAIL",
            "metadata": metadata,
            "reason": str(error),
        }
    (output / "evidence.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    (output / "report.md").write_text(
        readable(report)
        if "scenarios" in report
        else "# Stage 0 execution evidence\n\nReadiness: FAIL. Invalid traceability.\n"
    )
    print(f"Stage 0: {report['readiness']}; evidence: {output}")
    # BLOCKED is intentionally a nonzero release-readiness result, distinct from CI.
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[report["readiness"]]


if __name__ == "__main__":
    sys.exit(main())
