"""Typed Russian research synthesis that remains untrusted model data."""

from dataclasses import dataclass
from enum import StrEnum

from .provenance import MAX_SOURCE_RESULTS, SourceRecord


MAX_SYNTHESIS_CLAIMS = 32
MAX_SYNTHESIS_CONTRADICTIONS = 16
MAX_SYNTHESIS_UNVERIFIED_CLAIMS = 32
MAX_EVIDENCE_REFERENCES_PER_CLAIM = MAX_SOURCE_RESULTS
MAX_SYNTHESIS_FIELD_UTF8_BYTES = 16 * 1024
MAX_SYNTHESIS_UTF8_BYTES = 64 * 1024


class ClaimKind(StrEnum):
    OBSERVED_FACT = "observed_fact"
    INTERPRETATION = "interpretation"


class ClaimStatus(StrEnum):
    SOURCED = "sourced"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class ResearchClaimDraft:
    text: str
    kind: ClaimKind
    evidence_references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResearchSynthesisDraft:
    main_finding: str
    claims: tuple[ResearchClaimDraft, ...]
    uncertainty: str
    contradictions: tuple[str, ...]
    unverified_claims: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResearchClaim:
    text: str
    kind: ClaimKind
    status: ClaimStatus
    evidence: tuple[SourceRecord, ...]


@dataclass(frozen=True, slots=True)
class ResearchSynthesis:
    main_finding: str
    claims: tuple[ResearchClaim, ...]
    uncertainty: str
    contradictions: tuple[str, ...]
    unverified_claims: tuple[str, ...]
    source_statuses: tuple[SourceRecord, ...]

    @property
    def requires_shared_russian_output_gate(self) -> bool:
        return True


def build_russian_synthesis(
    draft: ResearchSynthesisDraft, sources: tuple[SourceRecord, ...]
) -> ResearchSynthesis:
    """Link claims only to sourced records; semantic support remains unevaluated."""

    if type(draft) is not ResearchSynthesisDraft:
        raise TypeError("draft must be a ResearchSynthesisDraft")
    _validate_draft(draft)
    if type(sources) is not tuple:
        raise TypeError("sources must be a tuple of SourceRecord values")
    if any(type(source) is not SourceRecord for source in sources):
        raise TypeError("sources must be a tuple of SourceRecord values")

    by_reference: dict[str, SourceRecord] = {}
    for source in sources:
        if source.reference in by_reference:
            raise ValueError("duplicate source reference")
        by_reference[source.reference] = source

    claims: list[ResearchClaim] = []
    unverified: list[str] = []
    unverified_seen: set[str] = set()
    for text in draft.unverified_claims:
        if text not in unverified_seen:
            unverified_seen.add(text)
            unverified.append(text)
    for claim in draft.claims:
        linked = tuple(
            by_reference[reference]
            for reference in claim.evidence_references
            if reference in by_reference and by_reference[reference].is_sourced
        )
        fully_linked = bool(claim.evidence_references) and len(linked) == len(
            claim.evidence_references
        )
        status = ClaimStatus.SOURCED if fully_linked else ClaimStatus.UNVERIFIED
        if status is ClaimStatus.UNVERIFIED and claim.text not in unverified_seen:
            unverified_seen.add(claim.text)
            unverified.append(claim.text)
        claims.append(
            ResearchClaim(
                claim.text, claim.kind, status, linked if fully_linked else ()
            )
        )
    return ResearchSynthesis(
        main_finding=draft.main_finding,
        claims=tuple(claims),
        uncertainty=draft.uncertainty,
        contradictions=draft.contradictions,
        unverified_claims=tuple(unverified),
        source_statuses=sources,
    )


def _validate_draft(draft: ResearchSynthesisDraft) -> None:
    if type(draft.claims) is not tuple:
        raise TypeError("claims must be a tuple")
    if type(draft.contradictions) is not tuple:
        raise TypeError("contradictions must be a tuple")
    if type(draft.unverified_claims) is not tuple:
        raise TypeError("unverified claims must be a tuple")
    if len(draft.claims) > MAX_SYNTHESIS_CLAIMS:
        raise ValueError("synthesis exceeded claim count bound")
    if len(draft.contradictions) > MAX_SYNTHESIS_CONTRADICTIONS:
        raise ValueError("synthesis exceeded contradiction count bound")
    if len(draft.unverified_claims) > MAX_SYNTHESIS_UNVERIFIED_CLAIMS:
        raise ValueError("synthesis exceeded unverified claim count bound")

    total_bytes = _add_text_bytes(0, draft.main_finding, "main finding")
    total_bytes = _add_text_bytes(total_bytes, draft.uncertainty, "uncertainty")
    for value in draft.contradictions:
        total_bytes = _add_text_bytes(total_bytes, value, "contradiction")
    for value in draft.unverified_claims:
        total_bytes = _add_text_bytes(total_bytes, value, "unverified claim")
    for claim in draft.claims:
        if type(claim) is not ResearchClaimDraft:
            raise TypeError("claim must be a ResearchClaimDraft")
        total_bytes = _add_text_bytes(total_bytes, claim.text, "claim")
        if type(claim.kind) is not ClaimKind:
            raise ValueError("invalid claim kind")
        if type(claim.evidence_references) is not tuple:
            raise TypeError("evidence references must be a tuple")
        if len(claim.evidence_references) > MAX_EVIDENCE_REFERENCES_PER_CLAIM:
            raise ValueError("synthesis exceeded evidence reference count bound")
        for reference in claim.evidence_references:
            if type(reference) is not str or not reference:
                raise TypeError("evidence references must be non-empty strings")
            total_bytes = _add_text_bytes(
                total_bytes,
                reference,
                "evidence reference",
                enforce_field_bound=False,
            )


def _add_text_bytes(
    total: int,
    value: object,
    field: str,
    *,
    enforce_field_bound: bool = True,
) -> int:
    if type(value) is not str or not value.strip():
        raise ValueError(f"invalid {field}")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError(f"invalid {field}") from error
    if enforce_field_bound and size > MAX_SYNTHESIS_FIELD_UTF8_BYTES:
        raise ValueError(f"oversized {field}")
    updated = total + size
    if updated > MAX_SYNTHESIS_UTF8_BYTES:
        raise ValueError("synthesis draft exceeds aggregate byte bound")
    return updated
