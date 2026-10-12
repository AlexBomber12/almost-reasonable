"""Validate frozen scenario traceability against actual pytest collection."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "acceptance/scenarios.json"
STATUSES = {"PASS", "FAIL", "BLOCKED", "NOT_RUN"}
LAYERS = {"component", "connected integration", "E2E", "recovery"}


def load_manifest(path=MANIFEST):
    return json.loads(path.read_text())


def validate_manifest(manifest, collected):
    scenarios = manifest["scenarios"]
    if [row["id"] for row in scenarios] != [f"SCN-{i:02d}" for i in range(1, 13)]:
        raise ValueError("manifest requires exactly SCN-01 through SCN-12")
    for nodes in manifest["release_checks"].values():
        if not nodes or not set(nodes) <= collected:
            raise ValueError("stale release-check pytest mapping")
    for row in scenarios:
        if not row["clauses"] or not row["capabilities"] or not row["synthetic_scope"]:
            raise ValueError("scenario scope and contracts required")
        if not set(row["capabilities"]) <= {f"M{i}" for i in range(1, 7)}:
            raise ValueError("unknown capability")
        if set(row["assurance"]) != {"G", "H", "M"}:
            raise ValueError("separate G/H/M classification required")
        for key in ("stage0_connection", "complete_mvp"):
            if row[key] not in STATUSES:
                raise ValueError("unknown status")
        if row["missing_connections"] and row["stage0_connection"] != "BLOCKED":
            raise ValueError("missing implementation must be BLOCKED")
        if not row["later_stage_requirements"] or row["complete_mvp"] != "BLOCKED":
            raise ValueError("offline evidence cannot accept complete MVP")
        if not row["evidence"]:
            raise ValueError("executable evidence required")
        for item in row["evidence"]:
            if item["layer"] not in LAYERS:
                raise ValueError("unknown evidence layer")
            if not all(
                item[key] for key in ("entry_point", "components", "sink", "scope")
            ):
                raise ValueError("observable path required")
            if not item["node_ids"] or not set(item["node_ids"]) <= collected:
                raise ValueError(f"stale pytest mapping for {row['id']}")
            if not set(item["risks"]) <= {
                "restart",
                "crash",
                "concurrency",
                "adversarial",
            }:
                raise ValueError("unknown risk")
    return manifest
