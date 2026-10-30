"""Offline acceptance evidence evaluation; hashes do not prove external truth."""

from datetime import UTC, datetime, timedelta
import hashlib
import json
import re

CAPABILITIES = {
    "M1": ("private_identity", "human_participation"),
    "M2": ("initiative_cadence_inactivity",),
    "M3": ("hosted_research",),
    "M4": ("public_conversation",),
    "M5": ("intellectual_evolution_restore",),
    "M6": ("owner_privacy_stop",),
}
CLASSES = {
    "synthetic_test_fixture",
    "real_model",
    "human_observation",
    "operator_runtime_observation",
}
KINDS = {"model_trial", "human_assessment", "heuristic_evaluation"} | {
    kind for kinds in CAPABILITIES.values() for kind in kinds
}
BINDING_FIELDS = {
    "head",
    "source_digest",
    "configuration_digest",
    "model_digest",
    "account_digest",
}


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names):
        raise ValueError("invalid evidence fields")


def _hash(value, length=64):
    if (
        type(value) is not str
        or re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value) is None
    ):
        raise ValueError("invalid evidence digest")
    return value


def _time(value):
    result = datetime.fromisoformat(value) if type(value) is str else value
    if (
        not isinstance(result, datetime)
        or result.tzinfo is None
        or result.utcoffset() is None
    ):
        raise ValueError("aware evidence time required")
    return result.astimezone(UTC)


def _list(value, *, maximum=4096):
    if type(value) is not list or len(value) > maximum:
        raise ValueError("bounded evidence list required")
    return value


def _unique_strings(value, *, maximum=4096):
    _list(value, maximum=maximum)
    if any(
        type(item) is not str or not item or len(item) > 512 for item in value
    ) or len(set(value)) != len(value):
        raise ValueError("unique evidence references required")
    return value


def _binding(value):
    _fields(value, BINDING_FIELDS)
    for name, field in value.items():
        _hash(field, 40 if name == "head" else 64)
    return dict(value)


def _rows(manifest, binding, trial_count):
    rows = manifest["scenarios"]
    if type(rows) is not list or [row["id"] for row in rows] != [
        f"SCN-{i:02d}" for i in range(1, 13)
    ]:
        raise ValueError("complete normative scenario inventory required")
    result = []
    for row in rows:
        caps = _unique_strings(row["capabilities"], maximum=6)
        if not caps or not set(caps) <= set(CAPABILITIES):
            raise ValueError("unknown or empty capability scope")
        clauses = _unique_strings(row["clauses"], maximum=32)
        later = _unique_strings(row["later_stage_requirements"], maximum=32)
        if not clauses or not later:
            raise ValueError("normative clauses and later requirements required")
        required = {"model_trial", "human_assessment", "heuristic_evaluation"} | {
            kind for cap in caps for kind in CAPABILITIES[cap]
        }
        result.append(
            {
                "id": row["id"],
                "capabilities": caps,
                "clauses": clauses,
                "later_stage_requirements": later,
                "requirements": sorted(required),
                "trial_ids": [
                    digest([binding, row["id"], number])[:16]
                    for number in range(trial_count or 0)
                ],
            }
        )
    if set(cap for row in result for cap in row["capabilities"]) != set(CAPABILITIES):
        raise ValueError("all MVP capabilities must remain in scope")
    return result


def build_plan(
    manifest,
    binding,
    *,
    at,
    expires_at,
    trial_count=None,
    calibration_digest=None,
    working_tree_clean=True,
):
    binding, start, end = _binding(binding), _time(at), _time(expires_at)
    if not start < end <= start + timedelta(days=30):
        raise ValueError("finite acceptance window required")
    if trial_count is not None and (
        type(trial_count) is not int or not 1 <= trial_count <= 100
    ):
        raise ValueError("operator-calibrated trial count required")
    if calibration_digest is not None:
        _hash(calibration_digest)
    if type(working_tree_clean) is not bool:
        raise ValueError("invalid source cleanliness")
    value = {
        "version": "real-acceptance-plan-v1",
        "binding": binding,
        "manifest_digest": digest(manifest),
        "created_at": start.isoformat(),
        "expires_at": end.isoformat(),
        "trial_count": trial_count,
        "calibration_digest": calibration_digest,
        "working_tree_clean": working_tree_clean,
        "scenarios": _rows(manifest, binding, trial_count),
    }
    return {**value, "plan_hash": digest(value)}


def _plan(value, manifest):
    _fields(
        value,
        {
            "version",
            "binding",
            "manifest_digest",
            "created_at",
            "expires_at",
            "trial_count",
            "calibration_digest",
            "working_tree_clean",
            "scenarios",
            "plan_hash",
        },
    )
    expected = build_plan(
        manifest,
        value["binding"],
        at=value["created_at"],
        expires_at=value["expires_at"],
        trial_count=value["trial_count"],
        calibration_digest=value["calibration_digest"],
        working_tree_clean=value["working_tree_clean"],
    )
    if value != expected:
        raise ValueError("acceptance plan changed")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate evidence field")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("invalid JSON number")


def decode(raw, *, maximum=65536):
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise ValueError("invalid evidence size")
    return json.loads(
        raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant
    )


def _approval(value, plan, at):
    if value is None:
        return set(), set(), False
    _fields(
        value,
        {
            "version",
            "plan_hash",
            "approved_evidence_sha256",
            "approved_calibration_sha256",
            "reviewer_refs",
            "expires_at",
        },
    )
    if (
        value["version"] != "real-acceptance-approval-v1"
        or value["plan_hash"] != plan["plan_hash"]
    ):
        raise ValueError("approval does not bind this plan")
    for key in ("approved_evidence_sha256", "reviewer_refs"):
        for item in _unique_strings(value[key]):
            _hash(item)
    if value["approved_calibration_sha256"] is not None:
        _hash(value["approved_calibration_sha256"])
    valid = (
        value["approved_calibration_sha256"] is not None
        and value["approved_calibration_sha256"] == plan["calibration_digest"]
        and at < _time(value["expires_at"])
    )
    return set(value["approved_evidence_sha256"]), set(value["reviewer_refs"]), valid


def _observation(raw):
    value = decode(raw)
    _fields(
        value,
        {
            "version",
            "plan_hash",
            "scenario",
            "kind",
            "source_class",
            "outcome",
            "observed_at",
            "receipt_refs",
            "trial_ids",
            "provider_attempt",
            "participants",
            "reviewer",
            "model_digest",
            "account_digest",
        },
    )
    if (
        value["version"] != "real-acceptance-evidence-v1"
        or value["kind"] not in KINDS
        or value["source_class"] not in CLASSES
        or value["outcome"] not in {"PASS", "FAIL", "NOT_RUN"}
    ):
        raise ValueError("invalid observation classification")
    _hash(value["plan_hash"])
    _time(value["observed_at"])
    refs = _unique_strings(value["receipt_refs"], maximum=128)
    if not refs:
        raise ValueError("underlying receipt required")
    for ref in refs:
        _hash(ref)
    _unique_strings(value["trial_ids"], maximum=100)
    for person in _list(value["participants"], maximum=16):
        _fields(person, {"subject", "session", "human_observed"})
        _hash(person["subject"])
        _hash(person["session"])
        if type(person["human_observed"]) is not bool:
            raise ValueError("human observation must be explicit")
    for name in ("reviewer", "model_digest", "account_digest", "provider_attempt"):
        if value[name] is not None:
            _hash(value[name])
    return value


def _ci(raw, binding):
    if raw is None:
        return "NOT_RUN"
    value = decode(raw, maximum=4194304)
    metadata = value["metadata"]
    if (
        metadata["head"] != binding["head"]
        or metadata["source_digest"] != binding["source_digest"]
    ):
        return "NOT_RUN"
    coverage, counts = value["coverage"], value["test_counts"]
    totals = [
        coverage[key]
        for key in (
            "num_statements",
            "num_branches",
            "missing_lines",
            "missing_branches",
        )
    ]
    return (
        "PASS"
        if metadata["working_tree"] == "clean"
        and value["deterministic_gate"] == "PASS"
        and set(counts) == {"PASS"}
        and type(counts["PASS"]) is int
        and counts["PASS"] > 0
        and all(type(total) is int for total in totals)
        and coverage["num_statements"] > 0
        and coverage["num_branches"] > 0
        and coverage["missing_lines"] == coverage["missing_branches"] == 0
        and value["oauth_in_coverage"] is True
        else "FAIL"
    )


def _qualified(value, row, plan, reviewers):
    kind, source = value["kind"], value["source_class"]
    if kind == "model_trial":
        return (
            source == "real_model"
            and value["model_digest"] == plan["binding"]["model_digest"]
            and value["provider_attempt"] is not None
            and len(value["trial_ids"]) == 1
            and value["trial_ids"][0] in row["trial_ids"]
        )
    if kind.startswith("human_"):
        if source != "human_observation" or value["reviewer"] not in reviewers:
            return False
        if kind == "human_assessment":
            return set(value["trial_ids"]) == set(row["trial_ids"])
        people = value["participants"]
        return (
            len(people) >= 2
            and len({person["subject"] for person in people}) == len(people)
            and len({person["session"] for person in people}) == len(people)
            and all(person["human_observed"] for person in people)
        )
    return (
        source == "operator_runtime_observation"
        and (
            kind != "public_conversation"
            or value["account_digest"] == plan["binding"]["account_digest"]
        )
        and (
            kind != "heuristic_evaluation"
            or plan["calibration_digest"] in value["receipt_refs"]
        )
    )


def _status(values):
    return "FAIL" if "FAIL" in values else "NOT_RUN" if "NOT_RUN" in values else "PASS"


def assess(
    plan,
    manifest,
    *,
    ci=None,
    evidence=(),
    approval=None,
    current,
    at,
    working_tree_clean=True,
):
    _plan(plan, manifest)
    now = _time(at)
    approved, reviewers, calibrated = _approval(approval, plan, now)
    current = _binding(current)
    if type(working_tree_clean) is not bool:
        raise ValueError("invalid source cleanliness")
    active = (
        current == plan["binding"]
        and plan["working_tree_clean"]
        and working_tree_clean
        and _time(plan["created_at"]) <= now < _time(plan["expires_at"])
    )
    if len(evidence) > 4096 or sum(len(raw) for raw in evidence) > 4194304:
        raise ValueError("evidence package exceeds bound")
    rows = {row["id"]: row for row in plan["scenarios"]}
    accepted = {name: [] for name in rows}
    attempts = set()
    failures, nonqualifying = [], []
    failed_trials = {name: set() for name in rows}
    failed_requirements = {name: set() for name in rows}
    for raw in evidence:
        value, reference = _observation(raw), hashlib.sha256(raw).hexdigest()
        if value["scenario"] not in rows:
            raise ValueError("unknown evidence scenario")
        row = rows[value["scenario"]]
        scoped = (
            active
            and value["plan_hash"] == plan["plan_hash"]
            and _time(plan["created_at"]) <= _time(value["observed_at"]) <= now
            and _time(value["observed_at"]) < _time(plan["expires_at"])
        )
        if (
            scoped
            and value["source_class"] != "synthetic_test_fixture"
            and value["outcome"] == "FAIL"
        ):
            failures.append({"scenario": row["id"], "sha256": reference})
            failed_requirements[row["id"]].add(value["kind"])
            if value["kind"] == "model_trial":
                failed_trials[row["id"]].update(
                    set(value["trial_ids"]) & set(row["trial_ids"])
                )
        if (
            not scoped
            or not calibrated
            or reference not in approved
            or value["outcome"] != "PASS"
            or value["kind"] not in row["requirements"]
            or not _qualified(value, row, plan, reviewers)
        ):
            nonqualifying.append(reference)
            continue
        if value["kind"] == "model_trial":
            if value["provider_attempt"] in attempts:
                nonqualifying.append(reference)
                continue
            attempts.add(value["provider_attempt"])
        accepted[row["id"]].append(value)
    loaded = {hashlib.sha256(raw).hexdigest() for raw in evidence}
    if ci is not None:
        loaded.add(hashlib.sha256(ci).hexdigest())
    missing_approved = sorted(approved - loaded)
    ci_status = _ci(ci, current)
    if (
        not active
        or not calibrated
        or ci is None
        or hashlib.sha256(ci).hexdigest() not in approved
        or missing_approved
    ):
        ci_status = "NOT_RUN"
    scenarios = []
    for name, row in rows.items():
        requirements = {}
        for kind in row["requirements"]:
            matches = [value for value in accepted[name] if value["kind"] == kind]
            valid = bool(matches)
            if kind == "model_trial":
                valid = bool(row["trial_ids"]) and {
                    trial for value in matches for trial in value["trial_ids"]
                } == set(row["trial_ids"])
            requirements[kind] = (
                "FAIL"
                if kind in failed_requirements[name]
                else "PASS"
                if valid
                else "NOT_RUN"
            )
        assurance = {
            "G": _status(
                [
                    ci_status,
                    *[
                        status
                        for kind, status in requirements.items()
                        if kind
                        not in {
                            "model_trial",
                            "human_assessment",
                            "heuristic_evaluation",
                        }
                    ],
                ]
            ),
            "H": requirements["heuristic_evaluation"],
            "M": _status(
                [requirements["model_trial"], requirements["human_assessment"]]
            ),
        }
        state = (
            "FAIL"
            if any(failure["scenario"] == name for failure in failures)
            else _status(list(assurance.values()))
        )
        passed_trials = {
            trial
            for value in accepted[name]
            if value["kind"] == "model_trial"
            for trial in value["trial_ids"]
        }
        trials = [
            {
                "id": trial,
                "status": "FAIL"
                if trial in failed_trials[name]
                else "PASS"
                if trial in passed_trials
                else "NOT_RUN",
            }
            for trial in row["trial_ids"]
        ]
        distribution = {
            status: sum(trial["status"] == status for trial in trials)
            for status in ("PASS", "FAIL", "NOT_RUN")
        }
        scenarios.append(
            {
                "id": name,
                "status": state,
                "assurance": assurance,
                "requirements": requirements,
                "trials": trials,
                "trial_distribution": distribution,
            }
        )
    caps = {
        cap: _status(
            [
                result["status"]
                for result in scenarios
                if cap in rows[result["id"]]["capabilities"]
            ]
        )
        for cap in CAPABILITIES
    }
    state = _status([result["status"] for result in scenarios])
    return {
        "version": "real-acceptance-report-v1",
        "plan_hash": plan["plan_hash"],
        "binding": current,
        "complete_mvp": "BLOCKED" if state == "NOT_RUN" else state,
        "rollout_authorized": False,
        "evidence_basis": "owner-approved attestations",
        "independently_verified_external_truth": False,
        "scenarios": scenarios,
        "capabilities": caps,
        "failures": failures,
        "nonqualifying": nonqualifying,
        "missing_approved": missing_approved,
    }
