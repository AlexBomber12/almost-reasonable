"""Fail-closed release decisions from execution records; no model oracle."""

from collections import Counter
from acceptance.manifest import validate_manifest


def test_status(phases):
    if "failed" in phases.values():
        return "FAIL"
    if phases == {"setup": "passed", "call": "passed", "teardown": "passed"}:
        return "PASS"
    return "NOT_RUN"


def build_report(manifest, execution, coverage, behavior, metadata, gate_exit):
    validate_manifest(manifest, set(execution["collected"]))
    outcomes = {
        node: test_status(execution["results"].get(node, {}))
        for node in execution["collected"]
    }
    totals = coverage.get("totals", {})
    full_coverage = (
        bool(totals.get("num_statements"))
        and bool(totals.get("num_branches"))
        and totals.get("missing_lines") == 0
        and totals.get("missing_branches") == 0
        and "app/oauth_server.py" in coverage.get("files", {})
    )
    deterministic = (
        "PASS"
        if (
            gate_exit == 0
            and execution["exit_code"] == 0
            and full_coverage
            and outcomes
            and set(outcomes.values()) == {"PASS"}
        )
        else "FAIL"
    )
    scenarios = []
    for row in manifest["scenarios"]:
        nodes = {node for item in row["evidence"] for node in item["node_ids"]}
        statuses = {outcomes[node] for node in nodes}
        evidence_status = (
            "FAIL"
            if "FAIL" in statuses
            else "NOT_RUN"
            if "NOT_RUN" in statuses
            else "PASS"
        )
        connection = (
            evidence_status if row["stage0_connection"] == "PASS" else "BLOCKED"
        )
        scenarios.append(
            {
                **row,
                "observed_evidence": evidence_status,
                "stage0_connection": connection,
            }
        )
    grouped = {}
    for row in scenarios:
        for item in row["evidence"]:
            for key in [
                "layer:" + item["layer"],
                *("risk:" + risk for risk in item["risks"]),
            ]:
                grouped.setdefault(key, set()).update(item["node_ids"])
    for key, nodes in manifest["release_checks"].items():
        grouped["release:" + key] = set(nodes)
    grouped_results = {
        key: {
            "counts": dict(Counter(outcomes[node] for node in sorted(nodes))),
            "node_ids": sorted(nodes),
        }
        for key, nodes in grouped.items()
    }
    # Recorded assessment fixtures are never human calibration or live trials.
    blockers = list(manifest["release_blockers"])
    readiness = (
        "FAIL"
        if deterministic == "FAIL"
        or any(row["observed_evidence"] == "FAIL" for row in scenarios)
        else "BLOCKED"
    )
    return {
        "format_version": "stage0-release-evidence-v1",
        "metadata": metadata,
        "status_meanings": {
            "PASS": "all required checks in the named scope executed successfully",
            "FAIL": "an executed check or local gate failed",
            "BLOCKED": "required implementation or calibration is absent",
            "NOT_RUN": "required evidence did not execute successfully (including skip or deselection)",
        },
        "deterministic_gate": deterministic,
        "test_counts": dict(Counter(outcomes.values())),
        "test_outcomes": outcomes,
        "grouped_results": grouped_results,
        "coverage": totals,
        "oauth_in_coverage": "app/oauth_server.py" in coverage.get("files", {}),
        "scenarios": scenarios,
        "behavior": behavior,
        "release_blockers": blockers,
        "stage_capabilities": manifest["stage_capabilities"],
        "readiness": readiness,
        "complete_mvp": "BLOCKED",
        "stage1_smoke": "NOT_RUN",
        "owner_concurrency": manifest["owner_concurrency"],
        "cross_store_atomicity": False,
        "production_activation": False,
    }


def readable(report):
    lines = [
        "# Stage 0 execution evidence",
        "",
        f"Readiness: **{report['readiness']}**. Complete MVP: **BLOCKED**.",
        "",
        f"Deterministic gate: {report['deterministic_gate']}; tests: {report['test_counts']}",
        f"Revision: `{report['metadata']['head']}`; base: `{report['metadata']['base']}`.",
        f"Source digest: `{report['metadata']['source_digest']}`.",
        "",
        "| Scenario | Executed evidence | Stage 0 connection | Complete MVP |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {row['id']} | {row['observed_evidence']} | {row['stage0_connection']} | BLOCKED |"
        for row in report["scenarios"]
    ]
    lines += [
        "",
        "Coverage totals: `" + str(report["coverage"]) + "`.",
        "",
        "Recovery/concurrency/adversarial/owner results: `"
        + str(
            {key: value["counts"] for key, value in report["grouped_results"].items()}
        )
        + "`. See JSON for exact group node IDs.",
        "",
        "Recorded behavioral assessment fixtures: `"
        + str(report["behavior"]["summary"])
        + "`.",
        "Thresholds provisional; human calibration absent; real-model trials and hidden cases NOT_RUN.",
        "Negative examples are intentional harness controls, not observed model failures.",
        "",
        "Required work:",
    ]
    lines += ["- " + item for item in report["release_blockers"]]
    lines += [
        "",
        "Owner concurrency: " + report["owner_concurrency"],
        "",
        "All tests use synthetic local stores and fake/recorded transports. No real publication, credentials or enrollment.",
        "No cross-store atomicity or production restore activation is claimed.",
        "Exact node mappings, individual outcomes, capabilities and limitations are in evidence.json.",
        "",
    ]
    return "\n".join(lines)
