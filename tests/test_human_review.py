"""Synthetic review fixtures exercise offline validation, never human calibration."""

from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from acceptance.behavior import evaluate
from acceptance.human_review import (
    CONTEXT,
    PACKET_VERSION,
    blank_review,
    digest,
    packet,
    strict_json,
    summarize,
    validate,
)


def fixture(pkt, reviewer="synthetic-reviewer-a"):
    submission = blank_review(pkt)
    submission["reviewer_id"] = reviewer
    submission["evidence_class"] = "synthetic_test_fixture"
    submission["metadata"]["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    for row in submission["assessments"]:
        row["score"] = 0.5
    return submission


def test_export_stable_blind_and_contextual():
    pkt = packet()
    assert pkt == packet()
    assert len(pkt["items"]) == 31
    serialized = json.dumps(pkt, ensure_ascii=False)
    assert "recorded_scores" not in serialized
    assert "positive" not in serialized and "negative" not in serialized
    assert "acceptable_decisions" not in serialized
    assert len({row["item_id"] for row in pkt["items"]}) == 31
    assert all(
        row["authority_state"] and row["context_sha256"] and row["dimensions"]
        for row in pkt["items"]
    )
    assert any(
        row["prior_conversation"] and "астрономию" in str(row["prior_conversation"])
        for row in pkt["items"]
    )
    assert any("Полярные" in str(row["source_excerpts"]) for row in pkt["items"])
    assert any("ожидает" in row["authority_state"] for row in pkt["items"])
    assert any(row["answer"]["text"].startswith("This is") for row in pkt["items"])
    assert "non_impersonation" in pkt["uncovered_dimensions"]
    positions = {row["item_id"]: index for index, row in enumerate(pkt["items"])}
    order = [
        positions[digest([PACKET_VERSION, pkt["corpus_version"], case, 1])[:20]]
        < positions[digest([PACKET_VERSION, pkt["corpus_version"], case, 2])[:20]]
        for case in ("continuity", "citations", "mission")
    ]
    assert len(set(order)) == 2
    assert all(
        row["guidance"] and set(row["guidance"]) == set(row["dimensions"])
        for row in pkt["items"]
    )
    observations = evaluate()["observations"]
    assert any(
        row["case_input_sha256"] == observations[0]["case_input_sha256"]
        for row in pkt["items"]
    )


def test_complete_synthetic_evidence_never_calibrates_and_disagreement_visible():
    pkt = packet()
    first = fixture(pkt)
    second = fixture(pkt, "synthetic-reviewer-b")
    second["assessments"][0]["score"] = 1
    first["assessments"][0]["concerns"] = ["ambiguous"]
    result = summarize(pkt, [first, second])
    assert result["status"] == "VALID_REVIEW_EVIDENCE_POLICY_UNRESOLVED"
    assert result["calibration"] == "NOT_APPROVED"
    assert result["reviewers_supplied"] == 2
    assert result["human_claimed_reviewers"] == 0
    assert result["disagreements"] and result["adjudication_items"]
    assert result["concerns"] == {"ambiguous": 1}
    assert "G" in result["limitations"][-1]


def test_incomplete_is_distinct_from_invalid():
    pkt = packet()
    sub = fixture(pkt)
    sub["assessments"].pop()
    sub["assessments"][0]["score"] = None
    result = summarize(pkt, [sub])
    assert result["status"] == "INCOMPLETE"
    assert len(result["missing_assessments"]) == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda x: x.update(packet_sha256="0" * 64),
        lambda x: x.update(rubric_version="old"),
        lambda x: x["assessments"][0].update(context_sha256="0" * 64),
        lambda x: x["assessments"][0].update(output_sha256="0" * 64),
        lambda x: x["assessments"][0].update(case_input_sha256="0" * 64),
        lambda x: x["assessments"][0].update(item_id="unknown"),
        lambda x: x["assessments"].append(deepcopy(x["assessments"][0])),
        lambda x: x["assessments"][0].update(score=True),
        lambda x: x["assessments"][0].update(score=1.1),
        lambda x: x["assessments"][0].update(score=float("nan")),
        lambda x: x["assessments"][0].update(concerns=["invented"]),
        lambda x: x["assessments"][0].update(extra="bad"),
        lambda x: x["metadata"].update(reviewed_at="2026-01-01"),
        lambda x: x.update(evidence_class="unknown"),
    ],
)
def test_invalid_submissions_fail_closed(mutate):
    pkt = packet()
    sub = fixture(pkt)
    mutate(sub)
    with pytest.raises(ValueError):
        validate(pkt, sub)


def test_duplicate_json_keys_and_nonfinite_rejected(tmp_path):
    path = tmp_path / "bad.json"
    for content in ('{"a":1,"a":2}', '{"score":NaN}'):
        path.write_text(content)
        with pytest.raises(ValueError):
            strict_json(path)


def test_duplicate_reviewer_rejected():
    pkt = packet()
    with pytest.raises(ValueError, match="duplicate reviewer"):
        summarize(pkt, [fixture(pkt), fixture(pkt)])


def test_missing_required_context_blocks_export(tmp_path, monkeypatch):
    import acceptance.human_review as review

    context = json.loads(CONTEXT.read_text())
    context["cases"]["continuity"]["prior_conversation"] = []
    path = tmp_path / "context.json"
    path.write_text(json.dumps(context))
    monkeypatch.setattr(review, "CONTEXT", path)
    with pytest.raises(ValueError, match="insufficient review context"):
        packet()
