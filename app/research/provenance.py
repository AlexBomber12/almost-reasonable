"""Fail-closed hosted-search source annotation validation."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import hmac
from ipaddress import ip_address
import json
import re
import secrets
import unicodedata
from urllib.parse import urlsplit

from .provider import HostedSearchResponse, MAX_SEARCH_CALLS


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*\Z")
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
MAX_RESPONSE_TEXT_UTF8_BYTES = 64 * 1024
MAX_PROVIDER_RESPONSE_UTF8_BYTES = 512 * 1024
MAX_SOURCE_RESULTS = 10
MAX_PROVIDER_OUTPUT_ITEMS = 16
MAX_CONSULTED_SOURCES = 100
_KNOWN_NON_URL_SOURCE_TYPES = frozenset(("oai-sports", "oai-weather", "oai-finance"))
_PROVIDER_SOURCE_AUTHORITY = object()
_VERIFIED_SOURCE_AUTHORITY = object()
_PROVIDER_SOURCE_INTEGRITY_KEY = secrets.token_bytes(32)
_VERIFIED_SOURCE_INTEGRITY_KEY = secrets.token_bytes(32)


class SourceKind(StrEnum):
    PROVIDER_ANNOTATION = "provider_annotation"
    INDEPENDENTLY_VERIFIED = "independently_verified"


class SourceStatus(StrEnum):
    RECORDED = "recorded"
    VERIFIED = "verified"
    MISSING_ANNOTATION = "missing_annotation"
    MALFORMED_ANNOTATION = "malformed_annotation"
    UNSUPPORTED_ANNOTATION = "unsupported_annotation"
    UNSUPPORTED_URL = "unsupported_url"
    MALFORMED_METADATA = "malformed_metadata"


class EvidenceSupport(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    method: str
    artifact_reference: str
    verified_at: datetime


@dataclass(frozen=True, slots=True)
class SourceRecord:
    reference: str
    kind: SourceKind
    status: SourceStatus
    provider_source_reference: str | None
    url: str | None
    title: str | None
    publisher: str | None
    source_language: str | None
    retrieval_time: datetime
    publication_date: str | None
    annotation_exists: bool
    metadata_validated: bool
    evidence_support: EvidenceSupport
    independently_verified: bool
    verification_evidence: VerificationEvidence | None
    _authority: object = field(repr=False, compare=False)
    _integrity: bytes = field(default=b"", repr=False, compare=False)

    @property
    def retrieved_at(self) -> datetime:
        return self.retrieval_time

    @property
    def is_sourced(self) -> bool:
        integrity_valid = _source_integrity_valid(self)
        provider_recorded = (
            integrity_valid
            and self._authority is _PROVIDER_SOURCE_AUTHORITY
            and self.kind is SourceKind.PROVIDER_ANNOTATION
            and self.status is SourceStatus.RECORDED
            and self.provider_source_reference is not None
            and self.metadata_validated
        )
        independently_verified = (
            integrity_valid
            and self._authority is _VERIFIED_SOURCE_AUTHORITY
            and self.kind is SourceKind.INDEPENDENTLY_VERIFIED
            and self.status is SourceStatus.VERIFIED
            and self.independently_verified
            and self.verification_evidence is not None
            and self.metadata_validated
        )
        return provider_recorded or independently_verified


def _source_record(**values: object) -> SourceRecord:
    authority = values.get("_authority")
    record = SourceRecord(**values)
    key = _source_integrity_key(authority)
    if key is None:
        raise ValueError("invalid source construction authority")
    object.__setattr__(record, "_integrity", _source_integrity(record, key))
    return record


def _source_integrity_key(authority: object) -> bytes | None:
    if authority is _PROVIDER_SOURCE_AUTHORITY:
        return _PROVIDER_SOURCE_INTEGRITY_KEY
    if authority is _VERIFIED_SOURCE_AUTHORITY:
        return _VERIFIED_SOURCE_INTEGRITY_KEY
    return None


def _source_integrity_valid(record: SourceRecord) -> bool:
    key = _source_integrity_key(record._authority)
    if key is None or not isinstance(record._integrity, bytes):
        return False
    try:
        expected = _source_integrity(record, key)
    except (AttributeError, TypeError, ValueError):
        return False
    return hmac.compare_digest(record._integrity, expected)


def _source_integrity(record: SourceRecord, key: bytes) -> bytes:
    evidence = record.verification_evidence
    evidence_payload = (
        None
        if evidence is None
        else {
            "method": evidence.method,
            "artifact_reference": evidence.artifact_reference,
            "verified_at": evidence.verified_at.isoformat(),
        }
    )
    serialized = json.dumps(
        {
            "reference": record.reference,
            "kind": record.kind.value,
            "status": record.status.value,
            "provider_source_reference": record.provider_source_reference,
            "url": record.url,
            "title": record.title,
            "publisher": record.publisher,
            "source_language": record.source_language,
            "retrieval_time": record.retrieval_time.isoformat(),
            "publication_date": record.publication_date,
            "annotation_exists": record.annotation_exists,
            "metadata_validated": record.metadata_validated,
            "evidence_support": record.evidence_support.value,
            "independently_verified": record.independently_verified,
            "verification_evidence": evidence_payload,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hmac.digest(key, serialized.encode("utf-8"), "sha256")


@dataclass(frozen=True, slots=True)
class ResearchEvidence:
    response_id: str
    text: str
    sources: tuple[SourceRecord, ...]
    search_calls: int


def parse_hosted_search_response(
    response: HostedSearchResponse,
    *,
    retrieved_at: datetime,
    max_search_calls: int = MAX_SEARCH_CALLS,
    max_sources: int = MAX_SOURCE_RESULTS,
) -> ResearchEvidence:
    if type(response) is not HostedSearchResponse:
        raise TypeError("response must be a HostedSearchResponse")
    if not _valid_identifier(response.response_id):
        raise ValueError("invalid provider response id")
    if response.status != "completed":
        raise ValueError("provider response is not completed")
    if type(response.output) is not tuple:
        raise ValueError("provider output must be a tuple")
    try:
        serialized_output = json.dumps(
            response.output,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("provider output is not serializable") from error
    if len(serialized_output.encode("utf-8")) > MAX_PROVIDER_RESPONSE_UTF8_BYTES:
        raise ValueError("provider response exceeds response byte bound")
    if len(response.output) > MAX_PROVIDER_OUTPUT_ITEMS:
        raise ValueError("provider response exceeded output item bound")
    _require_timestamp(retrieved_at)
    if (
        type(max_search_calls) is not int
        or not 1 <= max_search_calls <= MAX_SEARCH_CALLS
    ):
        raise ValueError("invalid search call bound")
    if type(max_sources) is not int or not 1 <= max_sources <= MAX_SOURCE_RESULTS:
        raise ValueError("invalid source result budget")

    call_ids: list[str] = []
    consulted_urls: dict[str, str] = {}
    messages: list[dict[str, object]] = []
    for item in response.output:
        if type(item) is not dict:
            raise ValueError("malformed provider output item")
        item_type = item.get("type")
        if item_type == "web_search_call":
            call_id, urls = _parse_search_call(item)
            if call_id in call_ids:
                raise ValueError("duplicate web search call id")
            call_ids.append(call_id)
            for url in urls:
                consulted_urls.setdefault(url, call_id)
        elif item_type == "message":
            messages.append(item)
        elif item_type == "reasoning":
            continue
        else:
            raise ValueError("unsupported provider output item")
    if len(call_ids) > max_search_calls:
        raise ValueError("provider response exceeded search call bound")
    if not call_ids:
        raise ValueError("provider response requires a search call")
    if len(messages) != 1:
        raise ValueError("provider response requires exactly one message")
    text, annotation_records = _parse_message(
        messages[0],
        response_id=response.response_id,
        consulted_urls=consulted_urls,
        retrieved_at=retrieved_at,
        max_sources=max_sources,
    )
    if not annotation_records:
        annotation_records = (
            _source_record(
                reference=f"{response.response_id}:{call_ids[0]}:missing",
                kind=SourceKind.PROVIDER_ANNOTATION,
                status=SourceStatus.MISSING_ANNOTATION,
                provider_source_reference=None,
                url=None,
                title=None,
                publisher=None,
                source_language=None,
                retrieval_time=retrieved_at,
                publication_date=None,
                annotation_exists=False,
                metadata_validated=False,
                evidence_support=EvidenceSupport.NOT_EVALUATED,
                independently_verified=False,
                verification_evidence=None,
                _authority=_PROVIDER_SOURCE_AUTHORITY,
            ),
        )
    return ResearchEvidence(
        response.response_id, text, annotation_records, len(call_ids)
    )


def register_independently_verified_source(
    *,
    reference: str,
    url: str,
    title: str,
    publisher: str | None,
    source_language: str | None,
    publication_date: str | None,
    evidence: VerificationEvidence,
) -> SourceRecord:
    if not _valid_identifier(reference):
        raise ValueError("invalid source reference")
    if not _valid_https_url(url):
        raise ValueError("invalid independently verified URL")
    title = _bounded_metadata(title, "title", required=True)
    publisher = _bounded_metadata(publisher, "publisher", required=False)
    if source_language is not None and (
        not isinstance(source_language, str)
        or _LANGUAGE_TAG.fullmatch(source_language) is None
    ):
        raise ValueError("invalid source language")
    if publication_date is not None and not _valid_date(publication_date):
        raise ValueError("invalid publication date")
    if type(evidence) is not VerificationEvidence:
        raise TypeError("verification evidence is required")
    method = _bounded_metadata(evidence.method, "verification method", required=True)
    artifact = _bounded_metadata(
        evidence.artifact_reference, "verification artifact", required=True
    )
    _require_timestamp(evidence.verified_at)
    verified_evidence = VerificationEvidence(method, artifact, evidence.verified_at)
    return _source_record(
        reference=reference,
        kind=SourceKind.INDEPENDENTLY_VERIFIED,
        status=SourceStatus.VERIFIED,
        provider_source_reference=None,
        url=url,
        title=title,
        publisher=publisher,
        source_language=source_language,
        retrieval_time=evidence.verified_at,
        publication_date=publication_date,
        annotation_exists=False,
        metadata_validated=True,
        evidence_support=EvidenceSupport.NOT_EVALUATED,
        independently_verified=True,
        verification_evidence=verified_evidence,
        _authority=_VERIFIED_SOURCE_AUTHORITY,
    )


def _parse_search_call(item: dict[str, object]) -> tuple[str, tuple[str, ...]]:
    call_id = item.get("id")
    if not _valid_identifier(call_id):
        raise ValueError("invalid web search call id")
    if item.get("status") != "completed":
        raise ValueError("incomplete web search call")
    action = item.get("action")
    if type(action) is not dict:
        raise ValueError("unsupported web search action")
    action_type = action.get("type")
    if action_type == "open_page":
        url = action.get("url")
        if url is None:
            return call_id, ()
        if not isinstance(url, str):
            raise ValueError("malformed open-page URL")
        return call_id, (url,)
    if action_type == "find_in_page":
        url = action.get("url")
        pattern = action.get("pattern")
        if not isinstance(url, str) or not _valid_optional_metadata(pattern):
            raise ValueError("malformed find-in-page action")
        return call_id, (url,)
    if action_type != "search":
        raise ValueError("unsupported web search action")
    raw_sources = action.get("sources", [])
    if raw_sources is None:
        raw_sources = []
    if type(raw_sources) is not list:
        raise ValueError("malformed web search sources")
    if len(raw_sources) > MAX_CONSULTED_SOURCES:
        raise ValueError("web search source list exceeds bound")
    urls: list[str] = []
    for source in raw_sources:
        if type(source) is not dict:
            raise ValueError("malformed web search source")
        source_type = source.get("type")
        if source_type in _KNOWN_NON_URL_SOURCE_TYPES:
            continue
        if source_type != "url":
            raise ValueError("malformed web search source")
        url = source.get("url")
        if not isinstance(url, str):
            raise ValueError("malformed web search source URL")
        urls.append(url)
    return call_id, tuple(urls)


def _parse_message(
    item: dict[str, object],
    *,
    response_id: str,
    consulted_urls: dict[str, str],
    retrieved_at: datetime,
    max_sources: int,
) -> tuple[str, tuple[SourceRecord, ...]]:
    message_id = item.get("id")
    if not _valid_identifier(message_id):
        raise ValueError("invalid provider message id")
    if item.get("status") != "completed" or item.get("role") != "assistant":
        raise ValueError("invalid provider message")
    content = item.get("content")
    if type(content) is not list or len(content) != 1:
        raise ValueError("invalid provider message content")
    output = content[0]
    if type(output) is not dict or output.get("type") != "output_text":
        raise ValueError("unsupported provider message content")
    text = output.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("missing provider output text")
    if len(text.encode("utf-8")) > MAX_RESPONSE_TEXT_UTF8_BYTES:
        raise ValueError("provider output text exceeds byte bound")
    annotations = output.get("annotations")
    if type(annotations) is not list:
        raise ValueError("malformed provider annotations")
    if len(annotations) > max_sources:
        raise ValueError("provider response exceeded source result budget")
    records = tuple(
        _parse_annotation(
            annotation,
            index=index,
            text=text,
            response_id=response_id,
            message_id=message_id,
            consulted_urls=consulted_urls,
            retrieved_at=retrieved_at,
        )
        for index, annotation in enumerate(annotations)
    )
    return text, records


def _parse_annotation(
    annotation: object,
    *,
    index: int,
    text: str,
    response_id: str,
    message_id: str,
    consulted_urls: dict[str, str],
    retrieved_at: datetime,
) -> SourceRecord:
    reference = f"{response_id}:{message_id}:{index}"
    if type(annotation) is not dict:
        return _invalid_provider_source(
            reference, SourceStatus.MALFORMED_ANNOTATION, retrieved_at
        )
    url = annotation.get("url")
    call_id = consulted_urls.get(url) if isinstance(url, str) else None
    if call_id is not None:
        reference = f"{response_id}:{call_id}:{message_id}:{index}"
    title = annotation.get("title")
    publisher = annotation.get("publisher")
    language = annotation.get("source_language")
    publication_date = annotation.get("publication_date")
    safe_url = url if _valid_https_url(url) else None
    safe_title = title if _valid_optional_metadata(title) else None
    safe_publisher = publisher if _valid_optional_metadata(publisher) else None
    safe_language = language if _valid_language(language) else None
    safe_publication_date = publication_date if _valid_date(publication_date) else None
    common = {
        "reference": reference,
        "kind": SourceKind.PROVIDER_ANNOTATION,
        "provider_source_reference": reference,
        "url": safe_url,
        "title": safe_title,
        "publisher": safe_publisher,
        "source_language": safe_language,
        "retrieval_time": retrieved_at,
        "publication_date": safe_publication_date,
        "annotation_exists": True,
        "evidence_support": EvidenceSupport.NOT_EVALUATED,
        "independently_verified": False,
        "verification_evidence": None,
        "_authority": _PROVIDER_SOURCE_AUTHORITY,
    }
    if annotation.get("type") != "url_citation":
        return _source_record(
            status=SourceStatus.UNSUPPORTED_ANNOTATION,
            metadata_validated=False,
            **common,
        )
    if not isinstance(url, str) or not _valid_https_url(url):
        return _source_record(
            status=SourceStatus.UNSUPPORTED_URL,
            metadata_validated=False,
            **common,
        )
    start = annotation.get("start_index")
    end = annotation.get("end_index")
    if (
        not isinstance(title, str)
        or not title.strip()
        or len(title.encode("utf-8")) > 1024
        or _has_control_characters(title)
        or type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(text)
    ):
        return _source_record(
            status=SourceStatus.MALFORMED_ANNOTATION,
            metadata_validated=False,
            **common,
        )
    if (
        (publisher is not None and not _valid_optional_metadata(publisher))
        or (language is not None and not _valid_language(language))
        or (publication_date is not None and not _valid_date(publication_date))
    ):
        return _source_record(
            status=SourceStatus.MALFORMED_METADATA,
            metadata_validated=False,
            **common,
        )
    if url not in consulted_urls:
        return _source_record(
            status=SourceStatus.UNSUPPORTED_ANNOTATION,
            metadata_validated=False,
            **common,
        )
    return _source_record(
        status=SourceStatus.RECORDED,
        metadata_validated=True,
        **common,
    )


def _invalid_provider_source(
    reference: str, status: SourceStatus, retrieved_at: datetime
) -> SourceRecord:
    return _source_record(
        reference=reference,
        kind=SourceKind.PROVIDER_ANNOTATION,
        status=status,
        provider_source_reference=None,
        url=None,
        title=None,
        publisher=None,
        source_language=None,
        retrieval_time=retrieved_at,
        publication_date=None,
        annotation_exists=True,
        metadata_validated=False,
        evidence_support=EvidenceSupport.NOT_EVALUATED,
        independently_verified=False,
        verification_evidence=None,
        _authority=_PROVIDER_SOURCE_AUTHORITY,
    )


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _valid_https_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 4096:
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return False
    return (
        _valid_hostname(hostname)
        and parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and not any(character.isspace() for character in value)
        and not _has_control_characters(value)
    )


def _valid_hostname(value: str | None) -> bool:
    if value is None:
        return False
    try:
        ip_address(value)
        return True
    except ValueError:
        pass
    try:
        ascii_hostname = value.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if ascii_hostname.endswith("."):
        ascii_hostname = ascii_hostname[:-1]
    return (
        bool(ascii_hostname)
        and len(ascii_hostname) <= 253
        and all(
            _HOST_LABEL.fullmatch(label) is not None
            for label in ascii_hostname.split(".")
        )
    )


def _valid_optional_metadata(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value.encode("utf-8")) <= 1024
        and not _has_control_characters(value)
    )


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _valid_language(value: object) -> bool:
    return isinstance(value, str) and _LANGUAGE_TAG.fullmatch(value) is not None


def _valid_date(value: object) -> bool:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _bounded_metadata(value: object, field: str, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not _valid_optional_metadata(value):
        raise ValueError(f"invalid {field}")
    assert isinstance(value, str)
    return value.strip()


def _require_timestamp(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    try:
        if value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        value.astimezone(UTC)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(
            "timestamp must be timezone-aware and representable"
        ) from error
    return value
