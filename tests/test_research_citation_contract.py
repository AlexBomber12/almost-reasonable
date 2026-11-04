"""Offline citation regressions, not measurements of Luna's real accuracy."""

from copy import deepcopy
import json
import os

import pytest

from app.research.provenance import SourceStatus, parse_hosted_search_response
from app.research.provider import HostedSearchResponse
from test_b05c_claim_evidence import assessed_raw
from test_int20_public_research import NOW, boundary, grant


def repeated_raw():
    raw = assessed_raw()
    content = raw["output"][1]["content"][0]
    # The same cited page appears in several claims/explanations. The API
    # annotates citation occurrences; that does not create more source URLs.
    content["annotations"] = [
        dict(annotation, start_index=index, end_index=index + 1)
        for index, annotation in enumerate(content["annotations"] * 3)
    ]
    return raw


def parse(raw, **kwargs):
    return parse_hosted_search_response(
        HostedSearchResponse(raw["id"], raw["status"], tuple(raw["output"])),
        retrieved_at=NOW,
        max_sources=3,
        **kwargs,
    )


def test_repeated_citations_share_source_records_and_survive_public_wire(tmp_path):
    raw = repeated_raw()
    parsed = parse(raw)
    assert len(parsed.sources) == 2
    assert all(source.status is SourceStatus.RECORDED for source in parsed.sources)
    worker, sdk, store = boundary(tmp_path, [raw], enabled=True)
    request = grant(worker)
    result = worker.execute(request, peer_uid=os.geteuid())
    assert result["outcome"] == "completed"
    report = result["report"]
    assert len(report["sources"]) == 2
    assert report["semantic_support"] == "not_evaluated"
    references = {source["reference"] for source in report["sources"]}
    assert all(
        item["reference"] in references
        for claim in report["assessment"]["claims"]
        for item in claim["evidence"]
    )
    assert store.usage_attempt(request.operation_id).total_tokens == 150
    assert (
        worker.execute(request, peer_uid=os.geteuid())["outcome"]
        == "duplicate_operation"
    )
    assert len(sdk.calls) == 1


@pytest.mark.parametrize("case", ["range", "type", "title", "url"])
def test_invalid_later_citation_is_not_hidden_by_a_valid_duplicate(case):
    raw = repeated_raw()
    content = raw["output"][1]["content"][0]
    annotation = deepcopy(content["annotations"][-1])
    annotation.update(
        {
            "range": {"end_index": len(content["text"]) + 1},
            "type": {"type": "file_citation"},
            "title": {"title": ""},
            "url": {"url": "http://unsafe.example/paper"},
        }[case]
    )
    content["annotations"] = [content["annotations"][1], annotation]
    evidence = parse(raw)
    assert any(
        source.status is not SourceStatus.RECORDED for source in evidence.sources
    )


def test_conflicting_duplicate_metadata_is_rejected():
    raw = repeated_raw()
    content = raw["output"][1]["content"][0]
    annotation = deepcopy(content["annotations"][0])
    annotation["title"] = "PRIVATE-CANARY"
    content["annotations"] = [content["annotations"][0], annotation]
    with pytest.raises(ValueError, match="conflicting duplicate citation"):
        parse(raw)


@pytest.mark.parametrize("count", [32, 33])
def test_citation_occurrences_keep_an_independent_hard_bound(count):
    raw = repeated_raw()
    content = raw["output"][1]["content"][0]
    content["annotations"] = [content["annotations"][0]] * count
    if count == 32:
        assert len(parse(raw).sources) == 1
    else:
        with pytest.raises(ValueError, match="annotation bound"):
            parse(raw)


@pytest.mark.parametrize(
    "case,expected",
    [
        ("sources", "research_citation_budget"),
        ("annotations", "research_annotation_budget"),
        ("conflict", "research_citation_conflict"),
        ("json", "research_assessment_json"),
        ("fields", "research_assessment_fields"),
        ("binding", "research_assessment_source"),
    ],
)
def test_safe_specific_failure_codes_are_durable_without_raw_answers(
    tmp_path, case, expected
):
    raw = assessed_raw()
    content = raw["output"][1]["content"][0]
    if case == "sources":
        for index in range(2):
            url = f"https://example.org/additional-{index}"
            raw["output"][0]["action"]["sources"].append({"type": "url", "url": url})
            content["annotations"].append(dict(content["annotations"][0], url=url))
    elif case == "annotations":
        content["annotations"] = [content["annotations"][0]] * 33
    elif case == "conflict":
        content["annotations"].append(
            dict(content["annotations"][0], title="PRIVATE-CANARY")
        )
    elif case == "json":
        content["text"] = '{"main_finding":"PRIVATE-CANARY"'
    else:
        draft = json.loads(content["text"])
        if case == "fields":
            draft["PRIVATE-CANARY"] = True
        else:
            draft["claims"][0]["evidence"][0]["url"] = (
                "https://uncited.example/PRIVATE-CANARY"
            )
        content["text"] = json.dumps(draft, ensure_ascii=False)
    worker, sdk, store = boundary(tmp_path, [raw], enabled=True)
    request = grant(worker)
    result = worker.execute(request, peer_uid=os.geteuid())
    assert result["outcome"] == "invalid_response"
    assert result["report"] is None
    record = store.usage_attempt(request.operation_id)
    assert record.provider_error_code == expected
    assert record.total_tokens == 150
    assert "PRIVATE-CANARY" not in repr(record) + json.dumps(result)
    assert len(sdk.calls) == 1


def test_unrecognized_parser_exception_stays_generic_and_private(tmp_path, monkeypatch):
    def reject(*args, **kwargs):
        raise ValueError("PRIVATE-CANARY")

    monkeypatch.setattr(
        "app.trusted.public_research.parse_hosted_search_response", reject
    )
    worker, sdk, store = boundary(tmp_path, [assessed_raw()], enabled=True)
    request = grant(worker)
    result = worker.execute(request, peer_uid=os.geteuid())
    record = store.usage_attempt(request.operation_id)
    assert record.provider_error_code == "research_provenance_invalid"
    assert "PRIVATE-CANARY" not in repr(record) + json.dumps(result)
    assert len(sdk.calls) == 1
