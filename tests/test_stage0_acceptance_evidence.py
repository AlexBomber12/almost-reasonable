"""Negative controls for traceability, recorded assessment aggregation and readiness."""

from copy import deepcopy
import json
import subprocess
import sys

import pytest

from acceptance.behavior import evaluate
from acceptance.manifest import ROOT, load_manifest, validate_manifest
from acceptance.report import build_report, readable, test_status as status


def manifest_nodes(manifest):
    return {
        node
        for row in manifest["scenarios"]
        for item in row["evidence"]
        for node in item["node_ids"]
    }


def test_manifest_matches_actual_full_pytest_collection():
    # Static source-name matching would miss parameter IDs and collection failures.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests"],
        capture_output=True,
        text=True,
        check=True,
    )
    nodes = {line for line in result.stdout.splitlines() if line.startswith("tests/")}
    manifest = load_manifest()
    assert validate_manifest(manifest, nodes) == manifest
    assert {row["id"] for row in manifest["scenarios"]} == {
        f"SCN-{i:02d}" for i in range(1, 13)
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_scenario",
        "stale_node",
        "missing_as_pass",
        "unknown_layer",
        "unknown_status",
        "unknown_capability",
        "missing_assurance",
        "missing_sink",
        "no_evidence",
        "unknown_risk",
        "mvp_pass",
        "missing_scope",
    ],
)
def test_manifest_corruption_fails_closed(mutation):
    manifest = deepcopy(load_manifest())
    nodes = manifest_nodes(manifest) | {
        node for group in manifest["release_checks"].values() for node in group
    }
    row = manifest["scenarios"][0]
    if mutation == "missing_scenario":
        manifest["scenarios"].pop()
    elif mutation == "stale_node":
        row["evidence"][0]["node_ids"].append("tests/absent.py::test_no_evidence")
    elif mutation == "missing_as_pass":
        row["missing_connections"] = ["absent control ingress"]
    elif mutation == "unknown_layer":
        row["evidence"][0]["layer"] = "complete MVP"
    elif mutation == "unknown_status":
        row["stage0_connection"] = "NOT_APPLICABLE"
    elif mutation == "unknown_capability":
        row["capabilities"] = ["M7"]
    elif mutation == "missing_assurance":
        del row["assurance"]["M"]
    elif mutation == "missing_sink":
        row["evidence"][0]["sink"] = ""
    elif mutation == "no_evidence":
        row["evidence"] = []
    elif mutation == "unknown_risk":
        row["evidence"][0]["risks"] = ["imaginary"]
    elif mutation == "mvp_pass":
        row["complete_mvp"] = "PASS"
    else:
        row["synthetic_scope"] = ""
    with pytest.raises(ValueError):
        validate_manifest(manifest, nodes)


def input_evidence():
    manifest = load_manifest()
    nodes = manifest_nodes(manifest) | {
        node for group in manifest["release_checks"].values() for node in group
    }
    execution = {
        "collected": sorted(nodes),
        "exit_code": 0,
        "results": {
            node: {"setup": "passed", "call": "passed", "teardown": "passed"}
            for node in nodes
        },
    }
    coverage = {
        "files": {"app/oauth_server.py": {}},
        "totals": {
            "num_statements": 100,
            "num_branches": 40,
            "missing_lines": 0,
            "missing_branches": 0,
        },
    }
    metadata = {
        "head": "synthetic-head",
        "base": "synthetic-base",
        "source_digest": "fixture-only",
    }
    return manifest, execution, coverage, evaluate(), metadata


def test_even_all_green_synthetic_fixtures_cannot_declare_release_ready():
    report = build_report(*input_evidence(), 0)
    assert report["deterministic_gate"] == "PASS"
    assert report["readiness"] == "BLOCKED"
    assert report["complete_mvp"] == "BLOCKED"
    assert report["stage1_smoke"] == "NOT_RUN"
    assert all(row["stage0_connection"] == "PASS" for row in report["scenarios"])
    assert "human calibration absent" in readable(report)
    summary = (
        (ROOT / "docs/stage0-acceptance.md").read_text().split("## Reproduce", 1)[0]
    )
    assert "connected synthetic SCN-12 cross-channel matrix is complete" in summary
    assert "calibration and operator threat/risk review" in summary
    assert "Missing complete cross-channel attack" not in summary


def test_scn12_requires_every_connected_case_and_effect_control():
    manifest, execution, coverage, behavior, metadata = input_evidence()
    scenario = next(row for row in manifest["scenarios"] if row["id"] == "SCN-12")
    connected = scenario["evidence"][0]["node_ids"]
    assert len(connected) == 10
    assert (
        "tests/test_cross_channel_adversarial.py::"
        "test_scn12_parentless_provenance_requires_contextual_binding"
    ) in connected
    for node in connected:
        omitted = deepcopy(execution)
        omitted["results"][node] = {
            "setup": "passed",
            "call": "skipped",
            "teardown": "passed",
        }
        report = build_report(manifest, omitted, coverage, behavior, metadata, 0)
        observed = next(row for row in report["scenarios"] if row["id"] == "SCN-12")
        assert observed["stage0_connection"] == "NOT_RUN"
        assert report["deterministic_gate"] == "FAIL"


@pytest.mark.parametrize(
    "fault",
    [
        "test",
        "teardown",
        "missing",
        "skip",
        "coverage_lines",
        "coverage_branches",
        "no_oauth",
        "no_coverage",
        "gate",
        "collection",
    ],
)
def test_failed_or_missing_execution_never_passes_deterministic_gate(fault):
    manifest, execution, coverage, behavior, metadata = input_evidence()
    node = execution["collected"][0]
    gate = 0
    if fault in ("test", "teardown"):
        execution["results"][node]["call" if fault == "test" else "teardown"] = "failed"
    elif fault == "missing":
        del execution["results"][node]
    elif fault == "skip":
        execution["results"][node] = {"setup": "skipped", "teardown": "passed"}
    elif fault == "coverage_lines":
        coverage["totals"]["missing_lines"] = 1
    elif fault == "coverage_branches":
        coverage["totals"]["missing_branches"] = 1
    elif fault == "no_oauth":
        coverage["files"] = {}
    elif fault == "no_coverage":
        coverage = {}
    elif fault == "gate":
        gate = 1
    else:
        execution["exit_code"] = 2
    report = build_report(manifest, execution, coverage, behavior, metadata, gate)
    assert report["deterministic_gate"] == "FAIL" and report["readiness"] == "FAIL"
    assert status({"setup": "passed", "call": "passed"}) == "NOT_RUN"


def test_recorded_controls_report_expected_failures_and_score_distributions():
    report = evaluate()
    assert report == evaluate()
    assert report["summary"]["cases"] == 24
    assert report["summary"]["recorded_outputs"] == 53
    assert report["summary"]["observations"] == 157
    assert report["summary"]["real_model_trials"] == 0
    assert report["summary"]["human_calibration"] == "absent"
    assert report["assurance_summary"]["M"] == {"passed": 21, "failed": 26}
    assert report["assurance_summary"]["H"] == {"passed": 54, "failed": 3}
    assert report["assurance_summary"]["G"] == {"passed": 46, "failed": 7}
    assert report["failure_counts"]["privacy_boundary"] == 10
    assert len({item["artifact_sha256"] for item in report["observations"]}) == 157
    assert all(item["adjudication"] is None for item in report["observations"])


def test_recorded_forbidden_output_is_detected_with_permissive_m_scores(tmp_path):
    from acceptance.behavior import CORPUS

    corpus = json.loads(CORPUS.read_text())
    corpus["cases"] = [corpus["cases"][4]]
    corpus["cases"][0]["recorded_scores"] = [1.0, 1.0]
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(corpus))
    report = evaluate(path)
    assert report["assurance_summary"]["M"] == {"passed": 2, "failed": 0}
    assert report["assurance_summary"]["G"] == {"passed": 1, "failed": 1}


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "no_outcomes",
        "no_coverage",
        "gate_failed",
        "source_changed",
        "head_changed",
    ],
)
def test_runner_uses_only_fresh_execution_and_preserves_failure_status(
    tmp_path, monkeypatch, fault
):
    """Runner I/O fixtures validate stale-artifact rejection; they are not release evidence."""
    from types import SimpleNamespace
    import acceptance.run as runner

    manifest, execution, coverage, _, _ = input_evidence()
    output = tmp_path / "evidence"
    output.mkdir()
    (output / "tests.json").write_text(json.dumps(execution))
    (output / "coverage.json").write_text(json.dumps(coverage))
    monkeypatch.setattr(sys, "argv", ["acceptance.run", "--output", str(output)])
    monkeypatch.setattr(runner, "load_manifest", lambda: manifest)
    monkeypatch.setattr(runner.platform, "platform", lambda: "synthetic-runner-test")
    digests = iter(
        ["fixture-source", "changed" if fault == "source_changed" else "fixture-source"]
    )
    monkeypatch.setattr(runner, "source_digest", lambda: next(digests))
    heads = []

    def git(*args):
        if args == ("status", "--porcelain"):
            return ""
        if args == ("rev-parse", "HEAD"):
            heads.append(True)
            return (
                "changed-head"
                if fault == "head_changed" and len(heads) > 1
                else "fixture-head"
            )
        return "fixture-base"

    monkeypatch.setattr(runner, "git", git)

    def gate(*args, **kwargs):
        assert not (output / "tests.json").exists()
        assert not (output / "coverage.json").exists()
        if fault != "no_outcomes":
            (output / "tests.json").write_text(json.dumps(execution))
        if fault != "no_coverage":
            (output / "coverage.json").write_text(json.dumps(coverage))
        return SimpleNamespace(returncode=1 if fault == "gate_failed" else 0)

    monkeypatch.setattr(runner.subprocess, "run", gate)
    assert runner.main() == (2 if fault == "none" else 1)
    report = json.loads((output / "evidence.json").read_text())
    assert report["readiness"] == ("BLOCKED" if fault == "none" else "FAIL")
    assert (output / "report.md").exists()


def test_ci_checkout_fetches_base_ref_for_actual_revision_metadata(
    tmp_path, monkeypatch
):
    """Reproduce SHA-only Actions checkout, then exercise configured full ref fetch."""
    import os
    import re
    import acceptance.run as runner

    workflow = (runner.ROOT / ".github/workflows/ci.yml").read_text()
    repository = tmp_path / "source"
    repository.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

    def git_at(path, *args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=path,
            env=env,
            capture_output=True,
            text=True,
            check=check,
        )

    git_at(repository, "init", "-b", "main")
    (repository / "fixture.txt").write_text("synthetic base")
    git_at(repository, "add", "fixture.txt")
    git_at(
        repository,
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=synthetic@example.test",
        "commit",
        "-m",
        "base",
    )
    base = git_at(repository, "rev-parse", "HEAD").stdout.strip()
    git_at(repository, "switch", "-c", "task")
    (repository / "fixture.txt").write_text("synthetic task")
    # Supply committer identity locally; never read a production credential/config.
    git_at(
        repository,
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=synthetic@example.test",
        "commit",
        "-am",
        "task",
    )
    head = git_at(repository, "rev-parse", "HEAD").stdout.strip()
    git_at(checkout, "init")
    git_at(checkout, "remote", "add", "origin", repository.as_uri())
    git_at(checkout, "fetch", "--depth=1", "origin", head)
    git_at(checkout, "checkout", "--detach", "FETCH_HEAD")
    monkeypatch.setattr(runner, "ROOT", checkout)
    with pytest.raises(subprocess.CalledProcessError):
        runner.git("rev-parse", "origin/main")
    depth = re.search(r"fetch-depth:\s*(\d+)", workflow)
    assert depth is not None and depth.group(1) == "0", (
        "CI needs complete base refs for evidence metadata"
    )
    git_at(
        checkout,
        "fetch",
        "--unshallow",
        "origin",
        "+refs/heads/*:refs/remotes/origin/*",
    )
    assert runner.git("rev-parse", "origin/main") == base
    assert runner.git("rev-parse", "HEAD") == head
