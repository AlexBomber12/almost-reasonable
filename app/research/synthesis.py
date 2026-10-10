"""Typed Russian research synthesis that remains untrusted model data."""

from dataclasses import dataclass
from enum import StrEnum

from .provenance import SourceRecord


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
    if type(sources) is not tuple or any(
        type(source) is not SourceRecord for source in sources
    ):
        raise TypeError("sources must be a tuple of SourceRecord values")
    _require_text(draft.main_finding, "main finding")
    _require_text(draft.uncertainty, "uncertainty")
    if type(draft.claims) is not tuple:
        raise TypeError("claims must be a tuple")
    if (
        type(draft.contradictions) is not tuple
        or type(draft.unverified_claims) is not tuple
    ):
        raise TypeError("synthesis lists must be tuples")
    for value in (*draft.contradictions, *draft.unverified_claims):
        _require_text(value, "synthesis text")

    by_reference: dict[str, SourceRecord] = {}
    for source in sources:
        if source.reference in by_reference:
            raise ValueError("duplicate source reference")
        by_reference[source.reference] = source

    claims: list[ResearchClaim] = []
    unverified = list(draft.unverified_claims)
    for claim in draft.claims:
        if type(claim) is not ResearchClaimDraft:
            raise TypeError("claim must be a ResearchClaimDraft")
        text = _require_text(claim.text, "claim")
        if type(claim.kind) is not ClaimKind:
            raise ValueError("invalid claim kind")
        if type(claim.evidence_references) is not tuple or any(
            not isinstance(reference, str) or not reference
            for reference in claim.evidence_references
        ):
            raise TypeError("evidence references must be non-empty strings")
        linked = tuple(
            by_reference[reference]
            for reference in claim.evidence_references
            if reference in by_reference and by_reference[reference].is_sourced
        )
        fully_linked = bool(claim.evidence_references) and len(linked) == len(
            claim.evidence_references
        )
        status = ClaimStatus.SOURCED if fully_linked else ClaimStatus.UNVERIFIED
        if status is ClaimStatus.UNVERIFIED and text not in unverified:
            unverified.append(text)
        claims.append(
            ResearchClaim(text, claim.kind, status, linked if fully_linked else ())
        )
    return ResearchSynthesis(
        main_finding=draft.main_finding,
        claims=tuple(claims),
        uncertainty=draft.uncertainty,
        contradictions=draft.contradictions,
        unverified_claims=tuple(unverified),
        source_statuses=sources,
    )


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {field}")
    if len(value.encode("utf-8")) > 16 * 1024:
        raise ValueError(f"oversized {field}")
    return value
