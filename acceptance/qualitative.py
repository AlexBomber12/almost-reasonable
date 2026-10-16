"""Offline qualitative pilot import; never participates in the acceptance runner."""

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from acceptance.human_review import (
    digest,
    strict_json_bytes,
    valid_reviewer_id,
    write_json,
)

FORMAT = "qualitative-human-pilot-checkpoint-v1"
IMPORT_VERSION = "qualitative-review-import-v1"
APPROVED_HUMAN_PILOT_SHA256 = (
    "4bb4bac24d6dd6b4563c305e2f502112e09dea9559d8406cca9a9b078eef360f"
)
VERDICTS = frozenset(
    {
        "acceptable",
        "acceptable_with_doubts",
        "specific_error_reported",
        "insufficient_context",
    }
)
CLASSES = frozenset({"human", "synthetic_test_fixture"})
HASH_FIELDS = ("case_input_sha256", "context_sha256", "output_sha256")


def _hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _fields(value, required, optional=()):
    if (
        type(value) is not dict
        or not set(required) <= value.keys()
        or value.keys() - set(required) - set(optional)
    ):
        raise ValueError("invalid qualitative review fields")


def _timestamp(value):
    if type(value) is not str:
        raise ValueError("invalid review timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid review timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("review timestamp requires timezone")


def import_pilot(path, expected_sha256, evidence_class):
    """Validate bindings and preserve the exact source record under its own identity."""
    if type(evidence_class) is not str or evidence_class not in CLASSES:
        raise ValueError("invalid evidence class")
    raw = Path(path).read_bytes()
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if source_sha256 != expected_sha256:
        raise ValueError("source SHA-256 mismatch")
    pilot = strict_json_bytes(raw)
    _fields(
        pilot,
        (
            "format_version",
            "reviewer_id",
            "reviewer_identity_basis",
            "sampling",
            "numeric_scores_assigned",
            "calibration",
            "policy_decisions",
            "plan",
            "responses",
            "pilot_completion",
            "summary",
            "adjudication_pending",
            "adjudication_resolution",
            "remaining_decisions",
        ),
        ("evidence_class",),
    )
    if (
        pilot["format_version"] != FORMAT
        or pilot["numeric_scores_assigned"] is not False
    ):
        raise ValueError("unsupported qualitative pilot")
    if evidence_class == "human":
        if source_sha256 != APPROVED_HUMAN_PILOT_SHA256 or "evidence_class" in pilot:
            raise ValueError("human evidence requires the approved pilot source")
    elif pilot.get("evidence_class") != "synthetic_test_fixture":
        raise ValueError("synthetic evidence class must be bound to the source")
    if (
        not valid_reviewer_id(pilot["reviewer_id"])
        or type(pilot["reviewer_identity_basis"]) is not str
        or not pilot["reviewer_identity_basis"]
    ):
        raise ValueError("missing reviewer provenance")
    plan = pilot["plan"]
    _fields(
        plan,
        ("packet_sha256", "packet_version", "items", "pilot_version", "material_note"),
    )
    if not _hash(plan["packet_sha256"]):
        raise ValueError("invalid packet identity")
    if (
        type(plan["items"]) is not list
        or type(pilot["responses"]) is not list
        or not plan["items"]
        or len(plan["items"]) != len(pilot["responses"])
    ):
        raise ValueError("invalid pilot items")
    items = {}
    for index, item in enumerate(plan["items"]):
        required = (
            "item_id",
            "prompt",
            "prior_conversation",
            "source_excerpts",
            "authority_state",
            "answer",
            "dimensions",
            *HASH_FIELDS,
        )
        if index == 0:
            _fields(item, (*required, "guidance"))
        else:
            _fields(
                item,
                (
                    *required,
                    "format_version",
                    "adapted_from",
                    "assessment_scope",
                    "scenario_sha256",
                ),
            )
            _fields(item["adapted_from"], ("packet_sha256", "item_id"))
            if (
                item["format_version"] != "qualitative-pilot-concrete-scenario-v1"
                or item["adapted_from"]["packet_sha256"] != plan["packet_sha256"]
                or not _hash(item["scenario_sha256"])
            ):
                raise ValueError("invalid adapted scenario provenance")
        if (
            type(item["item_id"]) is not str
            or not item["item_id"]
            or any(not _hash(item[field]) for field in HASH_FIELDS)
            or type(item["answer"]) is not dict
            or type(item["dimensions"]) is not list
        ):
            raise ValueError("invalid pilot item identity")
        if item["item_id"] in items:
            raise ValueError("duplicate pilot item")
        items[item["item_id"]] = item
    rows = []
    for response in pilot["responses"]:
        _fields(
            response,
            (
                "item_id",
                "verdict",
                "user_choice",
                "received_at",
                "note",
                "source",
                "assessment_scope",
                *HASH_FIELDS,
            ),
            ("packet_sha256", "scenario_sha256"),
        )
        if type(response["item_id"]) is not str:
            raise ValueError("invalid response item identity")
        item = items.get(response["item_id"])
        if item is None or any(response[field] != item[field] for field in HASH_FIELDS):
            raise ValueError("mismatched scenario or output identity")
        identity = "packet_sha256" if "packet_sha256" in response else "scenario_sha256"
        if (identity == "packet_sha256") != (item is plan["items"][0]) or response[
            identity
        ] != (plan[identity] if identity == "packet_sha256" else item[identity]):
            raise ValueError("mismatched packet or scenario identity")
        if (
            type(response["verdict"]) is not str
            or response["verdict"] not in VERDICTS
            or type(response["user_choice"]) is not str
            or type(response["note"]) is not str
            or type(response["source"]) is not str
            or not response["source"]
            or any(not _hash(response[field]) for field in HASH_FIELDS)
        ):
            raise ValueError("invalid verdict provenance")
        _timestamp(response["received_at"])
        rows.append({**response, "evidence_class": evidence_class})
    if len({row["item_id"] for row in rows}) != len(items):
        raise ValueError("missing or duplicate verdict")
    decisions = pilot["policy_decisions"]
    _fields(
        decisions,
        (
            "ambiguous_without_identified_error",
            "manual_review_budget",
            "invented_autobiographical_memories",
            "fictional_biography_disclosure",
        ),
    )
    for key in ("invented_autobiographical_memories", "fictional_biography_disclosure"):
        _timestamp(decisions[key]["received_at"])
    if pilot["summary"]["responses_received"] != len(rows) or pilot["summary"][
        "verdict_counts"
    ] != dict(Counter(row["verdict"] for row in rows)):
        raise ValueError("pilot summary mismatch")
    return {
        "import_version": IMPORT_VERSION,
        "source_sha256": source_sha256,
        "evidence_class": evidence_class,
        "source": pilot,
        "verdicts": rows,
        "counts": dict(Counter(row["verdict"] for row in rows)),
        "source_identity_sha256": digest(pilot),
        "calibration": "NOT_APPROVED",
    }


def readable(result):
    source = result["source"]
    lines = [
        "# Qualitative pilot review",
        "",
        f"Import: {result['import_version']}; evidence: {result['evidence_class']}; source SHA-256: {result['source_sha256']}",
        f"Reviewer: {source['reviewer_id']} ({source['reviewer_identity_basis']})",
        f"Verdicts: {len(result['verdicts'])}; counts: {json.dumps(result['counts'], sort_keys=True)}",
        f"Original packet: {source['plan']['packet_sha256']}; first item retains packet binding; later items are adapted scenarios with independent hashes.",
        "",
    ]
    for row in result["verdicts"]:
        lines.append(
            f"- {row['item_id']}: {row['verdict']} at {row['received_at']}; note: {json.dumps(row['note'], ensure_ascii=False)}"
        )
        lines.append(
            f"  scenario/packet: {row.get('scenario_sha256', row.get('packet_sha256'))}; "
            f"case: {row['case_input_sha256']}; context: {row['context_sha256']}; "
            f"output: {row['output_sha256']}; source: {row['source']}"
        )
    lines += [
        "",
        "## Subsequent policy clarification",
        "",
        json.dumps(
            source["policy_decisions"], ensure_ascii=False, sort_keys=True, indent=2
        ),
        "",
        "Qualitative permissibility is separate from provisional quality scores and deterministic G checks. An unexplained error report remains a concern without an invented rationale.",
        "This limited review does not calibrate 21 dimensions, approve thresholds, establish real-model quality, or authorize rollout.",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--evidence-class", choices=sorted(CLASSES), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = import_pilot(args.source, args.sha256, args.evidence_class)
    args.output.mkdir(parents=True, exist_ok=True)
    write_json(args.output / "import.json", result)
    (args.output / "summary.md").write_text(readable(result), encoding="utf-8")
    print(
        f"Imported {len(result['verdicts'])} {result['evidence_class']} verdicts: {result['counts']}"
    )


if __name__ == "__main__":
    main()
