"""Synthetic review fixtures exercise offline validation, never human calibration."""

from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from acceptance.behavior import evaluate
from acceptance.behavior import CORPUS
from app.behavior.evaluation import default_rubric
from acceptance.human_review import (
    CONTEXT,
    PACKET_VERSION,
    blank_review,
    digest,
    packet,
    readable_summary,
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
    assert len(pkt["items"]) > 31
    serialized = json.dumps(pkt, ensure_ascii=False)
    assert "recorded_scores" not in serialized
    assert "positive" not in serialized and "negative" not in serialized
    assert "acceptable_decisions" not in serialized
    assert len({row["item_id"] for row in pkt["items"]}) == len(pkt["items"])
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
    assert pkt["uncovered_dimensions"] == []
    assert {d for item in pkt["items"] for d in item["dimensions"]} == {
        rule.dimension.value for rule in default_rubric() if rule.assurance.value != "G"
    }
    positions = {row["item_id"]: index for index, row in enumerate(pkt["items"])}
    order = [
        positions[digest([PACKET_VERSION, pkt["corpus_version"], case, 1])[:20]]
        < positions[digest([PACKET_VERSION, pkt["corpus_version"], case, 2])[:20]]
        for case in ("continuity", "citations", "mission")
    ]
    assert len(order) == 3
    assert [item["item_id"] for item in pkt["items"]] == sorted(positions)
    assert all(
        row["guidance"] and set(row["guidance"]) == set(row["dimensions"])
        for row in pkt["items"]
    )
    observations = evaluate()["observations"]
    assert any(
        row["case_input_sha256"] == observations[0]["case_input_sha256"]
        for row in pkt["items"]
    )


def test_explicit_mapping_and_required_context():
    pkt = packet()
    corpus = strict_json(CORPUS)
    context = strict_json(CONTEXT)["cases"]
    for case in corpus["cases"]:
        items = [
            item
            for item in pkt["items"]
            if item["item_id"]
            in {
                digest([PACKET_VERSION, pkt["corpus_version"], case["id"], trial])[:20]
                for trial in range(1, len(case["outputs"]) + 1)
            }
        ]
        assert len(items) == len(case["outputs"])
        assert all(
            item["dimensions"] == sorted(case["review_dimensions"]) for item in items
        )
        assert all(
            item["prior_conversation"] == context[case["id"]]["prior_conversation"]
            for item in items
        )
        assert all(
            item["source_excerpts"] == context[case["id"]]["source_excerpts"]
            for item in items
        )
    assert next(case for case in corpus["cases"] if case["id"] == "russian")[
        "review_dimensions"
    ] == ["natural_russian"]


def test_new_dimensions_have_contrasts_and_multiple_reasonable_outcomes():
    corpus = strict_json(CORPUS)["cases"]
    newly_covered = {
        "independent_intellectual_development",
        "evidence_versus_interpretation",
        "useful_privacy_minimized_research",
        "single_source_skepticism",
        "owner_non_sycophancy",
        "mission_continuity",
        "evidence_driven_revision",
        "semantic_platform_policy_judgment",
        "non_impersonation",
        "no_invented_human_biography",
        "no_over_optimization_for_publication_frequency",
    }
    for dimension in newly_covered:
        cases = [case for case in corpus if dimension in case["review_dimensions"]]
        assert cases
        assert any(
            1.0 in case["recorded_scores"] and 0.25 in case["recorded_scores"]
            for case in cases
        )
        assert all(
            len({out["text"] for out in case["outputs"]}) == len(case["outputs"])
            for case in cases
        )
    assert {"owner_evidence", "peer_evidence"} <= {case["id"] for case in corpus}
    assert {"mission_continuity", "mission_abandonment"} <= {
        case["id"] for case in corpus
    }
    assert {"belief_revision", "belief_retention"} <= {case["id"] for case in corpus}


def test_old_packet_submission_rejected():
    pkt = packet()
    old = fixture(pkt)
    old["packet_sha256"] = "0" * 64
    old["corpus_version"] = "stage0-recorded-anchors-v1"
    old["context_version"] = "stage0-review-context-v1"
    with pytest.raises(ValueError, match="stale or mismatched"):
        validate(pkt, old)


def test_packet_binds_context_dimensions_and_guidance(tmp_path, monkeypatch):
    import acceptance.human_review as review

    original = packet()
    corpus = strict_json(CORPUS)
    context = strict_json(CONTEXT)
    corpus["cases"][0]["review_dimensions"].append("mission_continuity")
    altered_corpus = tmp_path / "cases.json"
    altered_corpus.write_text(json.dumps(corpus), encoding="utf-8")
    monkeypatch.setattr(review, "CORPUS", altered_corpus)
    # The new mapping is digest-bound even though the answer is unchanged.
    mapped = packet()
    assert mapped["packet_sha256"] != original["packet_sha256"]
    monkeypatch.setattr(review, "CORPUS", CORPUS)
    context["cases"]["development"]["prior_conversation"].append(
        "Сеанс 3: новый вопрос."
    )
    altered_context = tmp_path / "context.json"
    altered_context.write_text(json.dumps(context), encoding="utf-8")
    monkeypatch.setattr(review, "CONTEXT", altered_context)
    assert packet()["packet_sha256"] != original["packet_sha256"]
    monkeypatch.setitem(
        review.GUIDANCE, "mission_continuity", "Новая проверочная инструкция."
    )
    assert packet()["packet_sha256"] != original["packet_sha256"]


def test_invalid_dimension_mapping_fails_closed(tmp_path, monkeypatch):
    import acceptance.human_review as review

    corpus = strict_json(CORPUS)
    corpus["cases"][0]["review_dimensions"] = ["safe_privacy_boundaries"]
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(corpus), encoding="utf-8")
    monkeypatch.setattr(review, "CORPUS", path)
    with pytest.raises(ValueError, match="invalid review dimensions"):
        packet()


def test_complete_synthetic_evidence_never_calibrates_and_disagreement_visible():
    pkt = packet()
    first = fixture(pkt)
    second = fixture(pkt, "synthetic-reviewer-b")
    second["assessments"][0]["score"] = 1
    first["assessments"][0]["concerns"] = ["ambiguous"]
    result = summarize(pkt, [first, second])
    assert result["status"] == "NO_HUMAN_ASSESSMENTS"
    assert result["calibration"] == "NOT_APPROVED"
    assert result["evidence_classes"]["synthetic_test_fixture"]["reviewer_count"] == 2
    assert result["evidence_classes"]["human"]["reviewer_count"] == 0
    assert result["evidence_classes"]["synthetic_test_fixture"]["disagreements"]
    assert result["evidence_classes"]["synthetic_test_fixture"]["adjudication_items"]
    assert result["evidence_classes"]["synthetic_test_fixture"]["concerns"] == {
        "ambiguous": 1
    }
    assert "G" in result["limitations"][-1]


def test_incomplete_is_distinct_from_invalid():
    pkt = packet()
    sub = fixture(pkt)
    sub["assessments"].pop()
    sub["assessments"][0]["score"] = None
    result = summarize(pkt, [sub])
    assert result["status"] == "NO_HUMAN_ASSESSMENTS"
    assert (
        result["evidence_classes"]["synthetic_test_fixture"]["status"] == "INCOMPLETE"
    )
    assert (
        len(result["evidence_classes"]["synthetic_test_fixture"]["missing_assessments"])
        == 2
    )


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


def constructed_human_record(pkt, reviewer, score=0.2):
    """Synthetic test construction exercising the human evidence-class branch."""
    sub = fixture(pkt, reviewer)
    sub["evidence_class"] = "human"
    sub["metadata"]["note"] = "Synthetic test construction, not a real reviewer"
    for row in sub["assessments"]:
        row["score"] = score
    return sub


def test_fixture_cannot_change_human_summary():
    pkt = packet()
    human = constructed_human_record(pkt, "synthetic-human-branch")
    human["assessments"][0]["concerns"] = ["unsupported_claim"]
    human["assessments"][0]["note"] = "Synthetic test note"
    baseline = summarize(pkt, [human])
    synthetic = fixture(pkt, "synthetic-fixture")
    for row in synthetic["assessments"]:
        row["score"] = 1.0
    synthetic["assessments"][0]["score"] = None
    combined = summarize(pkt, [human, synthetic])
    assert (
        combined["evidence_classes"]["human"] == baseline["evidence_classes"]["human"]
    )
    assert combined["status"] == baseline["status"]
    assert combined["evidence_classes"]["human"]["disagreements"] == []
    assert combined["evidence_classes"]["synthetic_test_fixture"]["missing_assessments"]


def test_fixture_only_has_no_human_calibration_observations():
    result = summarize(packet(), [fixture(packet())])
    assert result["status"] == "NO_HUMAN_ASSESSMENTS"
    assert result["calibration"] == "NOT_APPROVED"
    assert result["evidence_classes"]["human"]["reviewer_count"] == 0
    assert result["evidence_classes"]["human"]["assessments_supplied"] == 0
    assert result["evidence_classes"]["human"]["score_distributions"] == {}


def test_human_disagreement_and_concern_remain_attributed():
    pkt = packet()
    first = constructed_human_record(pkt, "synthetic-human-a")
    second = constructed_human_record(pkt, "synthetic-human-b")
    second["assessments"][0]["score"] = 0.9
    first["assessments"][0]["concerns"] = ["unsupported_claim"]
    first["assessments"][0]["note"] = "Synthetic source excerpt does not support claim"
    result = summarize(pkt, [first, second])
    human = result["evidence_classes"]["human"]
    disagreement = human["disagreements"][0]
    assert disagreement["item_id"] == first["assessments"][0]["item_id"]
    assert disagreement["dimension"] == first["assessments"][0]["dimension"]
    assert {x["reviewer_id"] for x in disagreement["scores"]} == {
        "synthetic-human-a",
        "synthetic-human-b",
    }
    concern = human["concern_items"][0]
    assert concern["reviewer_id"] == "synthetic-human-a"
    assert concern["item_id"] == first["assessments"][0]["item_id"]
    assert concern["dimension"] == first["assessments"][0]["dimension"]
    assert concern["note"] == "Synthetic source excerpt does not support claim"


def test_summary_retains_packet_reviewer_and_every_assessment():
    pkt = packet()
    sub = constructed_human_record(pkt, "synthetic-human-only")
    sub["assessments"][0]["concerns"] = ["unsupported_claim"]
    sub["assessments"][0]["note"] = "Synthetic test explanation"
    result = summarize(pkt, [sub])
    assert result["summary_format_version"]
    assert result["packet_sha256"] == pkt["packet_sha256"]
    for key in (
        "corpus_version",
        "context_version",
        "russian_corpus_version",
        "rubric_version",
    ):
        assert result[key] == pkt[key]
    human = result["evidence_classes"]["human"]
    assert human["reviewer_count"] == 1
    assert human["reviewers"][0]["reviewer_id"] == sub["reviewer_id"]
    assert human["reviewers"][0]["metadata"] == sub["metadata"]
    assert human["reviewers"][0]["submission_sha256"] == digest(sub)
    assert len(human["observations"]) == len(sub["assessments"])
    assert human["observations"][0] == {
        "reviewer_id": sub["reviewer_id"],
        "evidence_class": "human",
        **sub["assessments"][0],
    }
    assert human["disagreements"] == []
    assert human["concern_items"][0]["note"] == "Synthetic test explanation"


def test_incomplete_classes_and_readable_attribution():
    pkt = packet()
    human = constructed_human_record(pkt, "synthetic-human-partial")
    synthetic = fixture(pkt, "synthetic-fixture-partial")
    human["assessments"][0]["score"] = None
    synthetic["assessments"].pop()
    human["assessments"][1]["concerns"] = ["unsupported_claim"]
    human["assessments"][1]["note"] = (
        "Synthetic test note: source does not support answer"
    )
    result = summarize(pkt, [human, synthetic])
    assert result["status"] == "INCOMPLETE"
    assert len(result["evidence_classes"]["human"]["missing_assessments"]) == 1
    assert (
        len(result["evidence_classes"]["synthetic_test_fixture"]["missing_assessments"])
        == 1
    )
    readable = readable_summary(result)
    assert pkt["packet_sha256"] in readable
    assert "synthetic-human-partial" in readable
    assert human["assessments"][1]["item_id"] in readable
    assert human["assessments"][1]["dimension"] in readable
    assert "unsupported_claim" in readable
    assert "Synthetic test note: source does not support answer" in readable
    assert "synthetic-fixture-partial" in readable


def test_language_only_case_has_no_honesty_assessment():
    pkt = packet()
    language_items = [
        row for row in pkt["items"] if row["prompt"] == "Ответь по-русски."
    ]
    assert len(language_items) == 2
    assert all(row["dimensions"] == ["natural_russian"] for row in language_items)
    assert not any(
        row["item_id"] in {item["item_id"] for item in language_items}
        and row["dimension"] == "intellectual_honesty"
        for row in blank_review(pkt)["assessments"]
    )


@pytest.mark.parametrize(
    "reviewer_id",
    [
        "reviewer-a\n\n## Итог: APPROVED",
        "reviewer-a **APPROVED**",
        "reviewer/a",
        "reviewer\tA",
    ],
)
def test_reviewer_id_cannot_inject_readable_report(reviewer_id):
    sub = fixture(packet())
    sub["reviewer_id"] = reviewer_id
    with pytest.raises(ValueError, match="reviewer_id"):
        summarize(packet(), [sub])


def test_reviewer_notes_cannot_inject_readable_report():
    pkt = packet()
    sub = constructed_human_record(pkt, "synthetic-markup-test")
    sub["metadata"]["note"] = "<h2>Итог: APPROVED</h2> **APPROVED**"
    sub["assessments"][0]["concerns"] = ["unsupported_claim"]
    sub["assessments"][0]["note"] = (
        "<script>bad</script> [APPROVED](https://example.test)"
    )
    rendered = readable_summary(summarize(pkt, [sub]))
    assert "<h2>" not in rendered
    assert "<script>" not in rendered
    assert "**APPROVED**" not in rendered
    assert "[APPROVED](https://example.test)" not in rendered
    assert "unsupported_claim" in rendered
    assert (
        sub["metadata"]["note"]
        == summarize(pkt, [sub])["evidence_classes"]["human"]["reviewers"][0][
            "metadata"
        ]["note"]
    )
