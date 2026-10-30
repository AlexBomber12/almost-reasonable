"""Schema fixtures test the evaluator; none are real acceptance evidence."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

from acceptance.manifest import load_manifest
from app.core.real_acceptance import build_plan, assess, decode

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)
BINDING = {
    "head": "a" * 40,
    "source_digest": "b" * 64,
    "configuration_digest": "c" * 64,
    "model_digest": "d" * 64,
    "account_digest": "e" * 64,
}
REVIEWER = "9" * 64


def raw(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def plan():
    return build_plan(
        load_manifest(),
        BINDING,
        at=NOW,
        expires_at=NOW + timedelta(days=1),
        trial_count=2,
        calibration_digest="f" * 64,
    )


def ci():
    return raw(
        {
            "metadata": {
                "head": BINDING["head"],
                "source_digest": BINDING["source_digest"],
                "working_tree": "clean",
            },
            "deterministic_gate": "PASS",
            "test_counts": {"PASS": 100},
            "coverage": {
                "num_statements": 1000,
                "num_branches": 400,
                "missing_lines": 0,
                "missing_branches": 0,
            },
            "oauth_in_coverage": True,
        }
    )


def observation(
    packet, row, kind, *, source_class="synthetic_test_fixture", trial_ids=()
):
    return {
        "version": "real-acceptance-evidence-v1",
        "plan_hash": packet["plan_hash"],
        "scenario": row["id"],
        "kind": kind,
        "source_class": source_class,
        "outcome": "PASS",
        "observed_at": (NOW + timedelta(minutes=1)).isoformat(),
        "receipt_refs": ["8" * 64],
        "trial_ids": list(trial_ids),
        "provider_attempt": sha(raw([row["id"], list(trial_ids)]))
        if kind == "model_trial"
        else None,
        "participants": [
            {"subject": "1" * 64, "session": "2" * 64, "human_observed": True},
            {"subject": "3" * 64, "session": "4" * 64, "human_observed": True},
        ]
        if kind == "human_participation"
        else [],
        "reviewer": REVIEWER if kind.startswith("human_") else None,
        "model_digest": BINDING["model_digest"] if kind == "model_trial" else None,
        "account_digest": BINDING["account_digest"]
        if kind == "public_conversation"
        else None,
    }


def bundle(packet, *, declared_attested=False):
    """Synthetic shapes can exercise attested-data parsing, not external truth."""
    values = []
    for row in packet["scenarios"]:
        for kind in row["requirements"]:
            source_class = "synthetic_test_fixture"
            if declared_attested:
                source_class = (
                    "real_model"
                    if kind == "model_trial"
                    else "human_observation"
                    if kind.startswith("human_")
                    else "operator_runtime_observation"
                )
            if kind == "model_trial":
                values.extend(
                    raw(
                        observation(
                            packet,
                            row,
                            kind,
                            source_class=source_class,
                            trial_ids=[trial],
                        )
                    )
                    for trial in row["trial_ids"]
                )
            else:
                item = observation(
                    packet,
                    row,
                    kind,
                    source_class=source_class,
                    trial_ids=row["trial_ids"] if kind == "human_assessment" else [],
                )
                if kind == "heuristic_evaluation":
                    item["receipt_refs"].append(packet["calibration_digest"])
                values.append(raw(item))
    return values


def approval(packet, evidence):
    return {
        "version": "real-acceptance-approval-v1",
        "plan_hash": packet["plan_hash"],
        "approved_evidence_sha256": [sha(value) for value in [ci(), *evidence]],
        "approved_calibration_sha256": "f" * 64,
        "reviewer_refs": [REVIEWER],
        "expires_at": (NOW + timedelta(days=1)).isoformat(),
    }


def evaluate(packet, evidence=(), policy=None, **kwargs):
    return assess(
        packet,
        load_manifest(),
        ci=ci(),
        evidence=evidence,
        approval=policy,
        current=BINDING,
        at=NOW + timedelta(hours=1),
        **kwargs,
    )


def test_empty_real_acceptance_is_not_run_and_never_authorizes_rollout():
    packet = plan()
    assert [row["id"] for row in packet["scenarios"]] == [
        f"SCN-{number:02d}" for number in range(1, 13)
    ]
    report = evaluate(packet)
    assert report["complete_mvp"] == "BLOCKED" and report["rollout_authorized"] is False
    assert all(row["status"] == "NOT_RUN" for row in report["scenarios"])
    assert set(report["capabilities"]) == {f"M{number}" for number in range(1, 7)}


def test_complete_fixture_bundle_cannot_become_real_acceptance_even_if_approved():
    packet = plan()
    evidence = bundle(packet)
    report = evaluate(packet, evidence, approval(packet, evidence))
    assert report["complete_mvp"] == "BLOCKED"
    assert all(row["status"] == "NOT_RUN" for row in report["scenarios"])
    assert len(report["nonqualifying"]) == len(evidence)


def test_owner_attested_shape_is_explicitly_not_independent_external_verification():
    packet = plan()
    evidence = bundle(packet, declared_attested=True)
    report = evaluate(packet, evidence, approval(packet, evidence))
    assert report["complete_mvp"] == "PASS"
    assert report["evidence_basis"] == "owner-approved attestations"
    assert report["independently_verified_external_truth"] is False
    assert report["rollout_authorized"] is False


@pytest.mark.parametrize(
    "change",
    ["head", "source_digest", "configuration_digest", "model_digest", "account_digest"],
)
def test_stale_code_or_runtime_binding_cannot_reuse_evidence(change):
    packet = plan()
    evidence = bundle(packet, declared_attested=True)
    current = dict(BINDING)
    current[change] = "0" * len(current[change])
    report = assess(
        packet,
        load_manifest(),
        ci=ci(),
        evidence=evidence,
        approval=approval(packet, evidence),
        current=current,
        at=NOW + timedelta(hours=1),
    )
    assert report["complete_mvp"] == "BLOCKED"


def test_edited_evidence_cannot_use_an_earlier_approved_hash():
    packet = plan()
    evidence = bundle(packet, declared_attested=True)
    policy = approval(packet, evidence)
    changed = json.loads(evidence[0])
    changed["receipt_refs"] = ["7" * 64]
    evidence[0] = raw(changed)
    report = evaluate(packet, evidence, policy)
    assert report["complete_mvp"] == "BLOCKED"


@pytest.mark.parametrize("field", ["subject", "session", "human_observed"])
def test_two_accounts_or_sessions_alone_do_not_prove_independent_humans(field):
    packet = plan()
    evidence = bundle(packet, declared_attested=True)
    for index, value in enumerate(evidence):
        row = json.loads(value)
        if row["kind"] == "human_participation":
            row["participants"][1][field] = (
                False if field == "human_observed" else row["participants"][0][field]
            )
            evidence[index] = raw(row)
    report = evaluate(packet, evidence, approval(packet, evidence))
    assert (
        report["complete_mvp"] == "BLOCKED" and report["capabilities"]["M1"] != "PASS"
    )


def test_a_failed_observation_cannot_be_hidden_by_omitting_approval():
    packet = plan()
    evidence = bundle(packet, declared_attested=True)
    policy = approval(packet, evidence)
    failed = next(
        json.loads(item)
        for item in evidence
        if json.loads(item)["kind"] == "model_trial"
    )
    failed["outcome"] = "FAIL"
    evidence.append(raw(failed))
    report = evaluate(packet, evidence, policy)
    assert report["complete_mvp"] == "FAIL" and report["failures"]
    assert report["scenarios"][0]["trial_distribution"]["FAIL"] == 1
    assert report["scenarios"][0]["assurance"]["M"] == "FAIL"


def test_approved_import_inventory_cannot_silently_drop_a_known_receipt():
    packet = plan()
    evidence = bundle(packet, declared_attested=True)
    failed = json.loads(evidence[0])
    failed["outcome"] = "FAIL"
    policy = approval(packet, [*evidence, raw(failed)])
    result = evaluate(packet, evidence, policy)
    assert result["complete_mvp"] == "BLOCKED" and result["missing_approved"] == [
        sha(raw(failed))
    ]


def test_nonmodel_runtime_failure_is_retained_without_inventing_model_outcomes():
    packet = plan()
    failed = observation(
        packet,
        packet["scenarios"][0],
        "owner_privacy_stop",
        source_class="operator_runtime_observation",
    )
    failed["outcome"] = "FAIL"
    report = evaluate(packet, [raw(failed)])
    assert report["complete_mvp"] == "FAIL" and report["scenarios"][0][
        "trial_distribution"
    ] == {"PASS": 0, "FAIL": 0, "NOT_RUN": 2}
    assert report["scenarios"][0]["assurance"]["G"] == "FAIL"


def test_manifest_cannot_be_replaced_by_a_smaller_scenario_inventory():
    manifest = deepcopy(load_manifest())
    manifest["scenarios"].pop()
    with pytest.raises(ValueError):
        build_plan(manifest, BINDING, at=NOW, expires_at=NOW + timedelta(hours=1))


def test_one_provider_attempt_cannot_be_reused_as_independent_trials():
    packet = plan()
    evidence = bundle(packet, declared_attested=True)
    for index, value in enumerate(evidence):
        item = json.loads(value)
        if item["kind"] == "model_trial":
            item["provider_attempt"] = "6" * 64
            evidence[index] = raw(item)
    assert (
        evaluate(packet, evidence, approval(packet, evidence))["complete_mvp"]
        == "BLOCKED"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"expires_at": NOW},
        {"expires_at": NOW + timedelta(days=31)},
        {"at": datetime(2026, 10, 30)},
        {"at": 1},
        {"trial_count": True},
        {"trial_count": 0},
        {"trial_count": 101},
        {"calibration_digest": "not-a-digest"},
        {"working_tree_clean": 1},
        {"binding": {**BINDING, "head": "a" * 64}},
    ],
)
def test_preregistration_rejects_unbounded_or_ambiguous_inputs(changes):
    args = {
        "binding": BINDING,
        "at": NOW,
        "expires_at": NOW + timedelta(days=1),
        **changes,
    }
    with pytest.raises(ValueError):
        build_plan(load_manifest(), **args)


@pytest.mark.parametrize(
    "mutation",
    [
        "capabilities",
        "clauses",
        "unknown_capability",
        "later_stage_requirements",
        "missing_M6",
    ],
)
def test_missing_normative_scope_is_rejected(mutation):
    manifest = deepcopy(load_manifest())
    if mutation == "missing_M6":
        for row in manifest["scenarios"]:
            row["capabilities"] = [cap for cap in row["capabilities"] if cap != "M6"]
    elif mutation == "unknown_capability":
        manifest["scenarios"][0]["capabilities"] = ["M9"]
    else:
        manifest["scenarios"][0][mutation] = []
    with pytest.raises(ValueError):
        build_plan(manifest, BINDING, at=NOW, expires_at=NOW + timedelta(hours=1))


@pytest.mark.parametrize(
    "value", [b"", b'{"x":1,"x":2}', b'{"x":NaN}', b"[]" * 40000, b"\xff", "{}"]
)
def test_strict_bounded_json_rejects_ambiguous_or_unreadable_evidence(value):
    with pytest.raises((ValueError, UnicodeError)):
        decode(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "unknown"),
        ("receipt_refs", []),
        ("receipt_refs", ["bad"]),
        ("trial_ids", ["repeat", "repeat"]),
        ("trial_ids", [1]),
        ("trial_ids", ["x" * 513]),
        ("trial_ids", "wrong"),
        (
            "participants",
            [{"subject": "1" * 64, "session": "2" * 64, "human_observed": 1}],
        ),
        ("participants", [{}]),
        ("participants", [{}] * 17),
        ("scenario", "SCN-13"),
    ],
)
def test_invalid_receipt_metadata_fails_closed(field, value):
    packet = plan()
    item = observation(packet, packet["scenarios"][0], "model_trial")
    item[field] = value
    with pytest.raises(ValueError):
        evaluate(packet, [raw(item)])


def test_plan_tampering_and_wrong_approval_fail_closed():
    packet = plan()
    packet["scenarios"][0]["requirements"].remove("model_trial")
    with pytest.raises(ValueError):
        evaluate(packet)
    with pytest.raises(ValueError):
        evaluate(plan(), policy={})
    policy = approval(plan(), [])
    policy["plan_hash"] = "0" * 64
    with pytest.raises(ValueError):
        evaluate(plan(), policy=policy)


@pytest.mark.parametrize(
    "reason",
    [
        "expired",
        "future",
        "policy_expired",
        "no_calibration",
        "wrong_calibration",
        "dirty",
        "dirty_plan",
        "no_trials",
        "no_reviewer",
    ],
)
def test_missing_calibration_freshness_clean_source_or_reviewer_blocks_acceptance(
    reason,
):
    packet = plan()
    if reason in {"no_calibration", "dirty_plan", "no_trials"}:
        packet = build_plan(
            load_manifest(),
            BINDING,
            at=NOW,
            expires_at=NOW + timedelta(days=1),
            trial_count=None if reason == "no_trials" else 2,
            calibration_digest=None if reason == "no_calibration" else "f" * 64,
            working_tree_clean=reason != "dirty_plan",
        )
    evidence = bundle(packet, declared_attested=True)
    # No-calibration fixtures still carry a bounded receipt, not a JSON null.
    evidence = [
        raw({**json.loads(item), "receipt_refs": ["8" * 64, "f" * 64]})
        for item in evidence
    ]
    policy = approval(packet, evidence)
    at = (
        NOW + timedelta(days=2)
        if reason == "expired"
        else NOW - timedelta(seconds=1)
        if reason == "future"
        else NOW + timedelta(hours=1)
    )
    if reason == "policy_expired":
        policy["expires_at"] = NOW.isoformat()
    if reason == "wrong_calibration":
        policy["approved_calibration_sha256"] = "0" * 64
    if reason == "no_calibration":
        policy["approved_calibration_sha256"] = None
    if reason == "no_reviewer":
        policy["reviewer_refs"] = []
    result = assess(
        packet,
        load_manifest(),
        ci=ci(),
        evidence=evidence,
        approval=policy,
        current=BINDING,
        at=at,
        working_tree_clean=reason != "dirty",
    )
    assert result["complete_mvp"] == "BLOCKED"


def test_oversize_package_and_ambiguous_cleanliness_are_rejected():
    with pytest.raises(ValueError):
        evaluate(plan(), [b"{}"] * 4097)
    with pytest.raises(ValueError):
        evaluate(plan(), [b"x" * 4194305])
    with pytest.raises(ValueError):
        evaluate(plan(), working_tree_clean=1)


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "wrong_head",
        "wrong_source",
        "dirty",
        "gate_failed",
        "skip",
        "bool_count",
        "zero_count",
        "bool_coverage",
        "zero_lines",
        "zero_branches",
        "uncovered_line",
        "uncovered_branch",
        "oauth",
    ],
)
def test_ci_cannot_promote_missing_stale_incomplete_or_skipped_execution(change):
    packet = plan()
    evidence = bundle(packet, declared_attested=True)
    value = json.loads(ci())
    if change.startswith("wrong_"):
        value["metadata"]["head" if change == "wrong_head" else "source_digest"] = (
            "0" * (40 if change == "wrong_head" else 64)
        )
    elif change == "dirty":
        value["metadata"]["working_tree"] = "modified"
    elif change == "gate_failed":
        value["deterministic_gate"] = "FAIL"
    elif change == "skip":
        value["test_counts"]["SKIP"] = 1
    elif change in {"bool_count", "zero_count"}:
        value["test_counts"]["PASS"] = True if change == "bool_count" else 0
    elif change == "bool_coverage":
        value["coverage"]["num_statements"] = True
    elif change in {"zero_lines", "zero_branches"}:
        value["coverage"][
            "num_statements" if change == "zero_lines" else "num_branches"
        ] = 0
    elif change in {"uncovered_line", "uncovered_branch"}:
        value["coverage"][
            "missing_lines" if change == "uncovered_line" else "missing_branches"
        ] = 1
    elif change == "oauth":
        value["oauth_in_coverage"] = False
    execution = raw(value)
    policy = approval(packet, evidence)
    policy["approved_evidence_sha256"].append(
        sha(execution)
    ) if execution != ci() else None
    result = assess(
        packet,
        load_manifest(),
        ci=None if change == "missing" else execution,
        evidence=evidence,
        approval=policy,
        current=BINDING,
        at=NOW + timedelta(hours=1),
    )
    assert result["complete_mvp"] != "PASS"


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_digest", "0" * 64),
        ("provider_attempt", None),
        ("trial_ids", []),
        ("trial_ids", ["unregistered"]),
    ],
)
def test_unregistered_model_attempt_is_not_accepted(field, value):
    packet = plan()
    evidence = bundle(packet, declared_attested=True)
    for index, data in enumerate(evidence):
        item = json.loads(data)
        if item["kind"] == "model_trial":
            item[field] = value
            evidence[index] = raw(item)
    assert (
        evaluate(packet, evidence, approval(packet, evidence))["complete_mvp"]
        == "BLOCKED"
    )


def test_wrong_public_account_unbound_corpus_and_fixture_failures_do_not_qualify():
    packet = plan()
    evidence = bundle(packet, declared_attested=True)
    for index, data in enumerate(evidence):
        item = json.loads(data)
        if item["kind"] == "public_conversation":
            item["account_digest"] = "0" * 64
        if item["kind"] == "heuristic_evaluation":
            item["receipt_refs"] = ["8" * 64]
        evidence[index] = raw(item)
    failed = observation(packet, packet["scenarios"][0], "model_trial")
    failed["outcome"] = "FAIL"
    evidence.append(raw(failed))
    result = evaluate(packet, evidence, approval(packet, evidence))
    assert result["complete_mvp"] == "BLOCKED" and not result["failures"]
