"""Deterministic privacy-policy boundaries."""

from .models import AbstractionCandidate, AbstractionKind, BoundaryDecision


MAX_ABSTRACTION_OVERLAP = 0.35


def validate_ordinary_derivation(
    source_audiences: tuple[str, ...], target_audience: str
) -> BoundaryDecision:
    if not source_audiences or any(
        audience != target_audience for audience in source_audiences
    ):
        return BoundaryDecision(False, "ordinary_derivation_must_inherit_audience")
    return BoundaryDecision(True, "audience_inherited")


def validate_abstract_to_shared(candidate: AbstractionCandidate) -> BoundaryDecision:
    """Return eligibility for later integration, never publication authorization.

    Semantic judgments are explicit trusted inputs.  A false or uncertain screen
    fails closed; this deterministic layer does not pretend to prove anonymity.
    """

    checks = (
        (
            not isinstance(candidate.kind, AbstractionKind),
            "unsupported_abstraction_kind",
        ),
        (
            not isinstance(candidate.text, str) or not candidate.text.strip(),
            "empty_abstraction",
        ),
        (
            not isinstance(candidate.source_audience, str)
            or not candidate.source_audience.startswith("private:"),
            "invalid_source_audience",
        ),
        (
            not isinstance(candidate.max_source_overlap, (int, float))
            or isinstance(candidate.max_source_overlap, bool)
            or not 0.0 <= candidate.max_source_overlap <= 1.0,
            "invalid_source_overlap",
        ),
        (bool(candidate.direct_identifiers), "direct_identifier"),
        (bool(candidate.private_canaries), "private_canary"),
        (candidate.contains_personal_information, "personal_information"),
        (candidate.contains_quotation, "exact_or_attributable_quotation"),
        (candidate.recognizable_paraphrase, "recognizable_paraphrase"),
        (candidate.contains_relationship_detail, "relationship_detail"),
        (candidate.contains_attribution, "attribution"),
        (not candidate.semantic_screen_passed, "semantic_screen_failed_or_uncertain"),
        (not candidate.protected_source_references, "missing_source_provenance"),
        (
            candidate.max_source_overlap > MAX_ABSTRACTION_OVERLAP,
            "excessive_source_overlap",
        ),
    )
    for rejected, reason in checks:
        if rejected:
            return BoundaryDecision(False, reason)
    return BoundaryDecision(
        True,
        "eligible_for_fail_closed_integration",
        activation_allowed=False,
        protected_source_references=candidate.protected_source_references,
    )
