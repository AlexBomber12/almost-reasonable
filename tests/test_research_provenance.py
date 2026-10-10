from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest

from app.research.provenance import (
    EvidenceSupport,
    SourceRecord,
    SourceKind,
    SourceStatus,
    VerificationEvidence,
    parse_hosted_search_response,
    register_independently_verified_source,
)
from app.research.provider import HostedSearchResponse


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def response(*, url="https://example.org/paper", annotation=True, source=True, **ann):
    annotations = []
    if annotation:
        value = {
            "type": "url_citation",
            "start_index": 0,
            "end_index": 8,
            "url": url,
            "title": "Synthetic paper",
        }
        value.update(ann)
        annotations.append(value)
    sources = [{"type": "url", "url": url}] if source else []
    return HostedSearchResponse(
        "resp_synthetic",
        "completed",
        (
            {
                "type": "web_search_call",
                "id": "ws_synthetic",
                "status": "completed",
                "action": {"type": "search", "sources": sources},
            },
            {
                "type": "message",
                "id": "msg_synthetic",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Факт один.",
                        "annotations": annotations,
                    }
                ],
            },
        ),
    )


def test_valid_provider_annotation_records_provenance_without_semantic_claim():
    parsed = parse_hosted_search_response(response(), retrieved_at=NOW)

    assert parsed.text == "Факт один."
    assert parsed.search_calls == 1
    assert len(parsed.sources) == 1
    source = parsed.sources[0]
    assert source.kind is SourceKind.PROVIDER_ANNOTATION
    assert source.status is SourceStatus.RECORDED
    assert source.provider_source_reference == (
        "resp_synthetic:ws_synthetic:msg_synthetic:0"
    )
    assert source.url == "https://example.org/paper"
    assert source.title == "Synthetic paper"
    assert source.publisher is None
    assert source.source_language is None
    assert source.publication_date is None
    assert source.annotation_exists is True
    assert source.metadata_validated is True
    assert source.evidence_support is EvidenceSupport.NOT_EVALUATED
    assert source.retrieved_at == NOW


def test_whitespace_only_provider_text_is_rejected():
    valid = response()
    message = dict(valid.output[1])
    content = dict(message["content"][0])
    content["text"] = "   "
    message["content"] = [content]
    malformed = HostedSearchResponse(
        valid.response_id, valid.status, (valid.output[0], message)
    )

    with pytest.raises(ValueError, match="missing provider output text"):
        parse_hosted_search_response(malformed, retrieved_at=NOW)


def test_trailing_dot_dns_hostname_is_accepted():
    parsed = parse_hosted_search_response(
        response(url="https://example.org./paper"), retrieved_at=NOW
    )

    assert parsed.sources[0].status is SourceStatus.RECORDED


def test_ipv6_literal_source_hostname_is_accepted():
    url = "https://[2001:db8::1]/paper"
    parsed = parse_hosted_search_response(response(url=url), retrieved_at=NOW)
    verified = register_independently_verified_source(
        reference="source-ipv6",
        url=url,
        title="IPv6 source",
        publisher=None,
        source_language="en",
        publication_date=None,
        evidence=VerificationEvidence("manual_check", "audit:ipv6", NOW),
    )

    assert parsed.sources[0].status is SourceStatus.RECORDED
    assert verified.is_sourced is True


@pytest.mark.parametrize(
    ("provider_response", "status"),
    [
        (response(annotation=False), SourceStatus.MISSING_ANNOTATION),
        (response(source=False), SourceStatus.UNSUPPORTED_ANNOTATION),
        (response(url="file:///etc/passwd"), SourceStatus.UNSUPPORTED_URL),
        (response(url="javascript:alert(1)"), SourceStatus.UNSUPPORTED_URL),
        (response(url="https://example.org:99999/paper"), SourceStatus.UNSUPPORTED_URL),
        (response(url="https://example.org:0/paper"), SourceStatus.UNSUPPORTED_URL),
        (
            response(url="https://example.org:notaport/paper"),
            SourceStatus.UNSUPPORTED_URL,
        ),
        (response(url="https://."), SourceStatus.UNSUPPORTED_URL),
        (response(url="https://-"), SourceStatus.UNSUPPORTED_URL),
        (response(url="https://bad\x00host/paper"), SourceStatus.UNSUPPORTED_URL),
        (response(url="https://example.org/%"), SourceStatus.UNSUPPORTED_URL),
        (response(url="https://example.org/%ZZ"), SourceStatus.UNSUPPORTED_URL),
        (response(title=""), SourceStatus.MALFORMED_ANNOTATION),
        (response(title="bad\nmetadata"), SourceStatus.MALFORMED_ANNOTATION),
        (response(start_index=9, end_index=2), SourceStatus.MALFORMED_ANNOTATION),
        (response(publisher=123), SourceStatus.MALFORMED_METADATA),
        (response(publisher="bad\nmetadata"), SourceStatus.MALFORMED_METADATA),
        (response(publication_date="yesterday"), SourceStatus.MALFORMED_METADATA),
        (
            response(source=False, url="https://forged.example/fake"),
            SourceStatus.UNSUPPORTED_ANNOTATION,
        ),
    ],
)
def test_missing_forged_malformed_or_unsupported_annotations_fail_closed(
    provider_response, status
):
    parsed = parse_hosted_search_response(provider_response, retrieved_at=NOW)

    assert parsed.sources[0].status is status
    assert parsed.sources[0].metadata_validated is False
    assert parsed.sources[0].is_sourced is False


def test_provider_claimed_optional_metadata_is_recorded_but_not_independently_verified():
    parsed = parse_hosted_search_response(
        response(
            publisher="Synthetic Journal",
            source_language="en",
            publication_date="2025-01-02",
        ),
        retrieved_at=NOW,
    )
    source = parsed.sources[0]

    assert source.status is SourceStatus.RECORDED
    assert source.publisher == "Synthetic Journal"
    assert source.source_language == "en"
    assert source.publication_date == "2025-01-02"
    assert source.independently_verified is False


def test_malformed_annotation_metadata_is_removed_from_downgraded_record():
    malicious = "bad\nmetadata" + "x" * 2000
    parsed = parse_hosted_search_response(
        response(
            title="bad\nmetadata",
            publisher=malicious,
            source_language="invalid language",
            publication_date="yesterday",
        ),
        retrieved_at=NOW,
    )
    source = parsed.sources[0]

    assert source.status is SourceStatus.MALFORMED_ANNOTATION
    assert source.title is None
    assert source.publisher is None
    assert source.source_language is None
    assert source.publication_date is None
    assert malicious not in repr(source)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: object(),
        lambda value: HostedSearchResponse(value.response_id, "failed", value.output),
        lambda value: HostedSearchResponse(
            value.response_id, value.status, (object(),)
        ),
        lambda value: HostedSearchResponse(value.response_id, value.status, (None,)),
        lambda value: HostedSearchResponse(
            value.response_id,
            value.status,
            (
                {
                    "type": "web_search_call",
                    "id": "bad id",
                    "status": "completed",
                    "action": {},
                },
            ),
        ),
    ],
)
def test_malformed_provider_envelopes_are_rejected(mutator):
    with pytest.raises((TypeError, ValueError)):
        parse_hosted_search_response(mutator(response()), retrieved_at=NOW)


def test_search_call_bound_is_checked_against_actual_response():
    item = response().output[0]
    second = dict(item)
    second["id"] = "ws_second"
    duplicate = HostedSearchResponse(
        "resp_synthetic", "completed", (item, second, response().output[1])
    )
    with pytest.raises(ValueError, match="search call bound"):
        parse_hosted_search_response(duplicate, retrieved_at=NOW, max_search_calls=1)


def test_multiple_distinct_search_calls_are_accepted_within_the_bound():
    valid = response()
    second_call = {
        "type": "web_search_call",
        "id": "ws_second",
        "status": "completed",
        "action": {"type": "search", "sources": []},
    }
    parsed = parse_hosted_search_response(
        HostedSearchResponse(
            valid.response_id,
            valid.status,
            (valid.output[0], second_call, valid.output[1]),
        ),
        retrieved_at=NOW,
        max_search_calls=2,
    )
    assert parsed.search_calls == 2


@pytest.mark.parametrize(
    "action",
    [
        {"type": "open_page", "url": "https://example.org/paper"},
        {
            "type": "find_in_page",
            "url": "https://example.org/paper",
            "pattern": "synthetic evidence",
        },
    ],
)
def test_documented_page_action_variants_can_validate_citation_provenance(action):
    valid = response()
    call = dict(valid.output[0])
    call["action"] = action
    parsed = parse_hosted_search_response(
        HostedSearchResponse(valid.response_id, valid.status, (call, valid.output[1])),
        retrieved_at=NOW,
    )

    assert parsed.sources[0].status is SourceStatus.RECORDED
    assert parsed.sources[0].is_sourced is True


def test_open_page_without_a_url_is_valid_but_cannot_establish_provenance():
    valid = response()
    call = dict(valid.output[0])
    call["action"] = {"type": "open_page", "url": None}
    parsed = parse_hosted_search_response(
        HostedSearchResponse(valid.response_id, valid.status, (call, valid.output[1])),
        retrieved_at=NOW,
    )

    assert parsed.sources[0].status is SourceStatus.UNSUPPORTED_ANNOTATION
    assert parsed.sources[0].is_sourced is False


def test_search_action_may_omit_its_optional_source_list():
    valid = response(annotation=False)
    call = dict(valid.output[0])
    call["action"] = {"type": "search", "sources": None}
    parsed = parse_hosted_search_response(
        HostedSearchResponse(valid.response_id, valid.status, (call, valid.output[1])),
        retrieved_at=NOW,
    )
    assert parsed.sources[0].status is SourceStatus.MISSING_ANNOTATION


def test_documented_realtime_feed_sources_are_ignored_for_url_provenance():
    valid = response()
    call = dict(valid.output[0])
    call["action"] = {
        "type": "search",
        "sources": [
            {"type": "oai-sports"},
            {"type": "oai-weather"},
            {"type": "oai-finance"},
            {"type": "url", "url": "https://example.org/paper"},
        ],
    }
    parsed = parse_hosted_search_response(
        HostedSearchResponse(valid.response_id, valid.status, (call, valid.output[1])),
        retrieved_at=NOW,
    )

    assert parsed.sources[0].status is SourceStatus.RECORDED
    assert parsed.sources[0].url == "https://example.org/paper"


def test_duplicate_search_call_ids_and_result_budget_overflow_fail_closed():
    valid = response()
    duplicate_id = HostedSearchResponse(
        valid.response_id,
        valid.status,
        (valid.output[0], valid.output[0], valid.output[1]),
    )
    with pytest.raises(ValueError, match="duplicate web search call"):
        parse_hosted_search_response(duplicate_id, retrieved_at=NOW, max_search_calls=2)

    with pytest.raises(ValueError, match="source result budget"):
        parse_hosted_search_response(valid, retrieved_at=NOW, max_sources=0)

    message = dict(valid.output[1])
    content = dict(message["content"][0])
    content["annotations"] = content["annotations"] * 2
    message["content"] = [content]
    with pytest.raises(ValueError, match="source result budget"):
        parse_hosted_search_response(
            HostedSearchResponse(
                valid.response_id, valid.status, (valid.output[0], message)
            ),
            retrieved_at=NOW,
            max_sources=1,
        )


def test_reasoning_items_are_ignored_but_message_cardinality_is_closed():
    valid = response()
    with_reasoning = HostedSearchResponse(
        valid.response_id,
        valid.status,
        ({"type": "reasoning"}, *valid.output),
    )
    assert parse_hosted_search_response(with_reasoning, retrieved_at=NOW).text

    no_message = HostedSearchResponse(
        valid.response_id, valid.status, (valid.output[0],)
    )
    with pytest.raises(ValueError, match="exactly one message"):
        parse_hosted_search_response(no_message, retrieved_at=NOW)


@pytest.mark.parametrize(
    ("provider_response", "kwargs"),
    [
        (HostedSearchResponse("bad id", "completed", ()), {}),
        (HostedSearchResponse("resp", "completed", []), {}),
        (response(), {"max_search_calls": 0}),
    ],
)
def test_top_level_response_identifiers_types_and_bounds_are_validated(
    provider_response, kwargs
):
    with pytest.raises(ValueError):
        parse_hosted_search_response(provider_response, retrieved_at=NOW, **kwargs)


def test_provider_output_item_count_is_bounded():
    import app.research.provenance as provenance_module

    valid = response()
    oversized = HostedSearchResponse(
        valid.response_id,
        valid.status,
        tuple({"type": "reasoning"} for _ in range(17)),
    )
    assert len(oversized.output) > provenance_module.MAX_PROVIDER_OUTPUT_ITEMS
    with pytest.raises(ValueError, match="output item bound"):
        parse_hosted_search_response(oversized, retrieved_at=NOW)


def test_total_provider_response_bytes_are_bounded(monkeypatch):
    import app.research.provenance as provenance_module

    monkeypatch.setattr(provenance_module, "MAX_PROVIDER_RESPONSE_UTF8_BYTES", 1)
    with pytest.raises(ValueError, match="response byte bound"):
        parse_hosted_search_response(response(), retrieved_at=NOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "bad id"},
        {"status": "failed"},
        {"action": {"type": "unsupported_action"}},
        {"action": None},
        {"action": {"type": "search", "sources": "not-a-list"}},
        {"action": {"type": "search", "sources": [None]}},
        {"action": {"type": "search", "sources": [{"type": "unknown"}]}},
        {"action": {"type": "search", "sources": [{"type": "url", "url": 3}]}},
        {
            "action": {
                "type": "search",
                "sources": [
                    {"type": "url", "url": f"https://example.org/{index}"}
                    for index in range(101)
                ],
            }
        },
        {"action": {"type": "open_page", "url": 3}},
        {
            "action": {
                "type": "find_in_page",
                "url": "https://example.org/paper",
                "pattern": "",
            }
        },
    ],
)
def test_malformed_search_call_fields_are_rejected(changes):
    valid = response()
    call = dict(valid.output[0])
    call.update(changes)
    malformed = HostedSearchResponse(
        valid.response_id, valid.status, (call, valid.output[1])
    )
    with pytest.raises(ValueError):
        parse_hosted_search_response(malformed, retrieved_at=NOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "bad id"},
        {"status": "failed"},
        {"role": "user"},
        {"content": None},
        {"content": [None]},
        {"content": [{"type": "refusal"}]},
        {"content": [{"type": "output_text", "text": "", "annotations": []}]},
        {"content": [{"type": "output_text", "text": "ok", "annotations": None}]},
    ],
)
def test_malformed_message_fields_are_rejected(changes):
    valid = response()
    message = dict(valid.output[1])
    message.update(changes)
    malformed = HostedSearchResponse(
        valid.response_id, valid.status, (valid.output[0], message)
    )
    with pytest.raises(ValueError):
        parse_hosted_search_response(malformed, retrieved_at=NOW)


def test_oversized_provider_text_is_rejected(monkeypatch):
    import app.research.provenance as provenance_module

    valid = response()
    message = dict(valid.output[1])
    content = dict(message["content"][0])
    content["text"] = "oversized"
    message["content"] = [content]
    monkeypatch.setattr(provenance_module, "MAX_RESPONSE_TEXT_UTF8_BYTES", 1)
    with pytest.raises(ValueError, match="byte bound"):
        parse_hosted_search_response(
            HostedSearchResponse(
                valid.response_id, valid.status, (valid.output[0], message)
            ),
            retrieved_at=NOW,
        )


def test_malformed_and_unsupported_annotation_objects_are_downgraded():
    for annotation, status in (
        (None, SourceStatus.MALFORMED_ANNOTATION),
        (
            {
                "type": "file_citation",
                "url": "https://example.org/paper",
                "title": "Paper",
                "start_index": 0,
                "end_index": 2,
            },
            SourceStatus.UNSUPPORTED_ANNOTATION,
        ),
    ):
        valid = response()
        message = dict(valid.output[1])
        content = dict(message["content"][0])
        content["annotations"] = [annotation]
        message["content"] = [content]
        parsed = parse_hosted_search_response(
            HostedSearchResponse(
                valid.response_id, valid.status, (valid.output[0], message)
            ),
            retrieved_at=NOW,
        )
        assert parsed.sources[0].status is status


def test_naive_timestamps_invalid_calendar_dates_and_long_urls_are_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_hosted_search_response(response(), retrieved_at=datetime(2026, 1, 1))

    evidence = VerificationEvidence("manual_primary_source_check", "audit:x", NOW)
    with pytest.raises(ValueError, match="publication date"):
        register_independently_verified_source(
            reference="source-1",
            url="https://example.org",
            title="Title",
            publisher=None,
            source_language=None,
            publication_date="2025-99-99",
            evidence=evidence,
        )
    with pytest.raises(ValueError, match="URL"):
        register_independently_verified_source(
            reference="source-1",
            url="https://" + "x" * 5000,
            title="Title",
            publisher=None,
            source_language=None,
            publication_date=None,
            evidence=evidence,
        )
    with pytest.raises(ValueError, match="URL"):
        register_independently_verified_source(
            reference="source-1",
            url="https://example.org:99999/paper",
            title="Title",
            publisher=None,
            source_language=None,
            publication_date=None,
            evidence=evidence,
        )
    with pytest.raises(ValueError, match="URL"):
        register_independently_verified_source(
            reference="source-1",
            url="https://example.org:0/paper",
            title="Title",
            publisher=None,
            source_language=None,
            publication_date=None,
            evidence=evidence,
        )
    with pytest.raises(ValueError, match="URL"):
        register_independently_verified_source(
            reference="source-1",
            url="https://example.org/%ZZ",
            title="Title",
            publisher=None,
            source_language=None,
            publication_date=None,
            evidence=evidence,
        )
    with pytest.raises(ValueError, match="URL"):
        register_independently_verified_source(
            reference="source-1",
            url="https://-",
            title="Title",
            publisher=None,
            source_language=None,
            publication_date=None,
            evidence=evidence,
        )
    with pytest.raises(ValueError, match="URL"):
        register_independently_verified_source(
            reference="source-1",
            url="https://\ud800/paper",
            title="Title",
            publisher=None,
            source_language=None,
            publication_date=None,
            evidence=evidence,
        )


def test_independent_verification_requires_a_representable_utc_offset():
    class MissingOffset(tzinfo):
        def utcoffset(self, _value):
            return None

    class FailingOffset(tzinfo):
        def utcoffset(self, _value):
            raise ValueError("synthetic offset failure")

    timestamps = (
        datetime(2026, 1, 1, tzinfo=MissingOffset()),
        datetime(2026, 1, 1, tzinfo=FailingOffset()),
        datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
    )
    for timestamp in timestamps:
        evidence = VerificationEvidence("manual_check", "audit:x", timestamp)
        with pytest.raises(ValueError, match="timezone-aware"):
            register_independently_verified_source(
                reference="source-1",
                url="https://example.org",
                title="Title",
                publisher=None,
                source_language=None,
                publication_date=None,
                evidence=evidence,
            )


def test_independently_verified_prior_source_requires_concrete_evidence():
    evidence = VerificationEvidence(
        method="manual_primary_source_check",
        artifact_reference="audit:synthetic-001",
        verified_at=NOW,
    )
    source = register_independently_verified_source(
        reference="prior-source-1",
        url="https://example.org/verified",
        title="Verified synthetic source",
        publisher="Example Institute",
        source_language="en",
        publication_date="2024-02-03",
        evidence=evidence,
    )

    assert source.kind is SourceKind.INDEPENDENTLY_VERIFIED
    assert source.status is SourceStatus.VERIFIED
    assert source.independently_verified is True
    assert source.verification_evidence == evidence
    assert source.is_sourced is True


def test_directly_forged_source_record_cannot_become_sourced():
    forged = SourceRecord(
        reference="forged",
        kind=SourceKind.INDEPENDENTLY_VERIFIED,
        status=SourceStatus.VERIFIED,
        provider_source_reference=None,
        url="https://example.org/forged",
        title="Forged",
        publisher=None,
        source_language="en",
        retrieval_time=NOW,
        publication_date=None,
        annotation_exists=False,
        metadata_validated=True,
        evidence_support=EvidenceSupport.SUPPORTED,
        independently_verified=True,
        verification_evidence=VerificationEvidence("claimed", "claimed", NOW),
        _authority=object(),
    )
    assert forged.is_sourced is False


def test_replacing_a_sourced_record_revokes_its_provenance_authority():
    verified = register_independently_verified_source(
        reference="prior-source-1",
        url="https://example.org/verified",
        title="Verified synthetic source",
        publisher=None,
        source_language="en",
        publication_date=None,
        evidence=VerificationEvidence(
            "manual_primary_source_check", "audit:synthetic-001", NOW
        ),
    )
    provider = parse_hosted_search_response(response(), retrieved_at=NOW).sources[0]

    assert replace(verified, url="file:///invented").is_sourced is False
    assert replace(verified, retrieval_time=object()).is_sourced is False
    assert replace(provider, title="Invented title").is_sourced is False


def test_internal_source_factory_rejects_unknown_construction_authority():
    import app.research.provenance as provenance_module

    with pytest.raises(ValueError, match="construction authority"):
        provenance_module._source_record(
            reference="forged",
            kind=SourceKind.INDEPENDENTLY_VERIFIED,
            status=SourceStatus.VERIFIED,
            provider_source_reference=None,
            url="https://example.org/forged",
            title="Forged",
            publisher=None,
            source_language="en",
            retrieval_time=NOW,
            publication_date=None,
            annotation_exists=False,
            metadata_validated=True,
            evidence_support=EvidenceSupport.NOT_EVALUATED,
            independently_verified=True,
            verification_evidence=VerificationEvidence("claimed", "claimed", NOW),
            _authority=object(),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"reference": "bad reference"},
        {"url": "http://example.org"},
        {"title": ""},
        {"publisher": ""},
        {"source_language": "bad tag"},
        {"publication_date": "03/02/2024"},
        {"evidence": None},
        {
            "evidence": VerificationEvidence(
                method="", artifact_reference="audit:x", verified_at=NOW
            )
        },
    ],
)
def test_prior_source_cannot_claim_verification_without_valid_evidence(changes):
    kwargs = {
        "reference": "prior-source-1",
        "url": "https://example.org/verified",
        "title": "Verified synthetic source",
        "publisher": None,
        "source_language": "en",
        "publication_date": "2024-02-03",
        "evidence": VerificationEvidence(
            method="manual_primary_source_check",
            artifact_reference="audit:synthetic-001",
            verified_at=NOW,
        ),
    }
    kwargs.update(changes)
    with pytest.raises((TypeError, ValueError)):
        register_independently_verified_source(**kwargs)
