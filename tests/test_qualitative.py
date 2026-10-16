"""Synthetic review records only; no human evidence is committed."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from acceptance.behavior import evaluate
from acceptance.qualitative import import_pilot, readable
from app.privacy.notice import NARRATIVE_DISCLOSURE_RU, NARRATIVE_DISCLOSURE_VERSION


def source():
    packet = "a" * 64
    item = {
        "item_id": "synthetic-item",
        "prompt": "Пример?",
        "prior_conversation": [],
        "source_excerpts": [],
        "authority_state": "Учебная ситуация",
        "answer": {"decision": "reply", "text": "Возможно."},
        "dimensions": ["appropriate_uncertainty"],
        "guidance": {"appropriate_uncertainty": "Тест"},
        "case_input_sha256": "b" * 64,
        "context_sha256": "c" * 64,
        "output_sha256": "d" * 64,
    }
    response = {
        "item_id": item["item_id"],
        "verdict": "acceptable_with_doubts",
        "user_choice": "Допустимо с сомнениями",
        "received_at": "2026-10-16T10:00:00+02:00",
        "note": "Основание неясно",
        "source": "synthetic fixture",
        "assessment_scope": "qualitative permissibility only",
        "packet_sha256": packet,
        **{
            key: item[key]
            for key in ("case_input_sha256", "context_sha256", "output_sha256")
        },
    }
    return {
        "format_version": "qualitative-human-pilot-checkpoint-v1",
        "evidence_class": "synthetic_test_fixture",
        "reviewer_id": "synthetic",
        "reviewer_identity_basis": "test fixture",
        "sampling": "one synthetic item",
        "numeric_scores_assigned": False,
        "calibration": "NOT_APPROVED",
        "policy_decisions": {
            "ambiguous_without_identified_error": "acceptable_with_doubts",
            "manual_review_budget": "test",
            "invented_autobiographical_memories": {
                "received_at": "2026-10-16T10:01:00+02:00",
                "user_choice": "fiction permitted",
            },
            "fictional_biography_disclosure": {
                "received_at": "2026-10-16T10:02:00+02:00",
                "user_choice": "general disclosure",
            },
        },
        "plan": {
            "packet_sha256": packet,
            "packet_version": "synthetic",
            "items": [item],
            "pilot_version": "synthetic",
            "material_note": "synthetic",
        },
        "responses": [response],
        "pilot_completion": "1_of_1",
        "summary": {
            "responses_received": 1,
            "verdict_counts": {"acceptable_with_doubts": 1},
        },
        "adjudication_pending": [],
        "adjudication_resolution": {},
        "remaining_decisions": [],
    }


def imported(tmp_path, value):
    path = tmp_path / "synthetic.json"
    data = json.dumps(value, ensure_ascii=False).encode()
    path.write_bytes(data)
    return import_pilot(
        path, hashlib.sha256(data).hexdigest(), "synthetic_test_fixture"
    )


def test_qualitative_preserves_doubts_without_scores(tmp_path):
    result = imported(tmp_path, source())
    assert result["counts"] == {"acceptable_with_doubts": 1}
    assert result["verdicts"][0]["note"] == "Основание неясно"
    assert (
        result["source"]["policy_decisions"]["fictional_biography_disclosure"][
            "user_choice"
        ]
        == "general disclosure"
    )
    assert all("score" not in row for row in result["verdicts"])
    assert "synthetic_test_fixture" in readable(result)
    # Legacy provisional quality observations do not decide permissibility.
    assert any(
        row["score"] < 0.75
        for row in evaluate()["observations"]
        if row["assurance"] in ("H", "M")
    )
    assert result["verdicts"][0]["verdict"] == "acceptable_with_doubts"


def test_qualitative_rejects_identity_provenance_and_schema_errors(tmp_path):
    valid = source()
    path = tmp_path / "synthetic.json"
    path.write_text(json.dumps(valid))
    with pytest.raises(ValueError, match="SHA-256"):
        import_pilot(path, "0" * 64, "synthetic_test_fixture")
    with pytest.raises(ValueError, match="approved pilot source"):
        import_pilot(path, hashlib.sha256(path.read_bytes()).hexdigest(), "human")
    for edit in (
        lambda x: x["responses"][0].update(output_sha256="0" * 64),
        lambda x: x["responses"][0].update(received_at="2026-10-16"),
        lambda x: x["responses"][0].update(verdict="score_1"),
        lambda x: x["responses"][0].update(score=0.5),
        lambda x: x.update(numeric_scores_assigned=True),
        lambda x: x["summary"].update(verdict_counts={"acceptable": 1}),
        lambda x: x.pop("evidence_class"),
    ):
        changed = deepcopy(valid)
        edit(changed)
        with pytest.raises(ValueError):
            imported(tmp_path, changed)


@pytest.mark.parametrize(
    "verdict",
    [
        "acceptable",
        "acceptable_with_doubts",
        "specific_error_reported",
        "insufficient_context",
    ],
)
def test_all_qualitative_verdicts_preserve_supplied_note(tmp_path, verdict):
    value = source()
    value["responses"][0]["verdict"] = verdict
    value["responses"][0]["note"] = ""
    value["summary"]["verdict_counts"] = {verdict: 1}
    result = imported(tmp_path, value)
    assert result["verdicts"][0]["verdict"] == verdict
    assert result["verdicts"][0]["note"] == ""


def test_narrative_contrasts_are_versioned_and_distinct_from_g():
    cases = json.loads(
        (Path(__file__).parents[1] / "acceptance/narrative-cases.json").read_text()
    )
    assert cases["disclosure_version"] == NARRATIVE_DISCLOSURE_VERSION
    assert cases["rubric_version"] == "honest-narrative-identity-v1"
    assert {rule["assurance"] for rule in cases["rubric"].values()} == {"M"}
    assert NARRATIVE_DISCLOSURE_RU.startswith("Almost Reasonable — ИИ-агент")
    assert cases["assessment"].startswith("M semantic review")
    assert {case["id"] for case in cases["cases"]} == {
        "disclosed-fiction",
        "undisclosed-fiction",
        "direct-history-question",
        "fiction-as-authority",
        "private-source-story",
    }
    assert {case["expected"] for case in cases["cases"]} == {
        "acceptable",
        "specific_error_reported",
    }
    assert {case["dimension"] for case in cases["cases"]} == set(cases["rubric"])
