from datetime import UTC, datetime

import pytest

from app.research.provenance import (
    EvidenceSupport,
    VerificationEvidence,
    register_independently_verified_source,
)
from app.research.synthesis import (
    ClaimKind,
    ClaimStatus,
    ResearchClaimDraft,
    ResearchSynthesisDraft,
    build_russian_synthesis,
)


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def source(reference="source-1"):
    return register_independently_verified_source(
        reference=reference,
        url=f"https://example.org/{reference}",
        title=f"Источник {reference}",
        publisher=None,
        source_language="ru",
        publication_date="2025-01-01",
        evidence=VerificationEvidence(
            "manual_primary_source_check", f"audit:{reference}", NOW
        ),
    )


def test_typed_russian_synthesis_separates_observation_interpretation_and_conflict():
    draft = ResearchSynthesisDraft(
        main_finding="Данные указывают на несколько возможных объяснений.",
        claims=(
            ResearchClaimDraft(
                "Наблюдаемая величина выросла.", ClaimKind.OBSERVED_FACT, ("source-1",)
            ),
            ResearchClaimDraft(
                "Вероятно, это связано с механизмом X.",
                ClaimKind.INTERPRETATION,
                ("source-1",),
            ),
        ),
        uncertainty="Причинная связь пока не установлена.",
        contradictions=("Источник 2 сообщает противоположный тренд.",),
        unverified_claims=("Механизм X универсален.",),
    )

    result = build_russian_synthesis(draft, (source(), source("source-2")))

    assert result.main_finding == draft.main_finding
    assert result.requires_shared_russian_output_gate is True
    assert result.claims[0].kind is ClaimKind.OBSERVED_FACT
    assert result.claims[0].status is ClaimStatus.SOURCED
    assert result.claims[0].evidence[0].evidence_support is (
        EvidenceSupport.NOT_EVALUATED
    )
    assert result.claims[1].kind is ClaimKind.INTERPRETATION
    assert result.uncertainty
    assert result.contradictions
    assert result.unverified_claims


def test_missing_or_invalid_evidence_link_downgrades_claim_without_fabrication():
    draft = ResearchSynthesisDraft(
        main_finding="Проверенных данных недостаточно.",
        claims=(
            ResearchClaimDraft(
                "Непроверенное утверждение.", ClaimKind.OBSERVED_FACT, ("missing",)
            ),
        ),
        uncertainty="Высокая неопределённость.",
        contradictions=(),
        unverified_claims=(),
    )

    result = build_russian_synthesis(draft, (source(),))

    assert result.claims[0].status is ClaimStatus.UNVERIFIED
    assert result.claims[0].evidence == ()
    assert "Непроверенное утверждение." in result.unverified_claims


def test_semantic_support_is_never_inferred_from_syntax_or_a_url():
    draft = ResearchSynthesisDraft(
        main_finding="Есть формальная ссылка.",
        claims=(ResearchClaimDraft("Факт.", ClaimKind.OBSERVED_FACT, ("source-1",)),),
        uncertainty="Нужна семантическая проверка.",
        contradictions=(),
        unverified_claims=(),
    )

    result = build_russian_synthesis(draft, (source(),))

    assert result.claims[0].evidence[0].evidence_support is (
        EvidenceSupport.NOT_EVALUATED
    )


@pytest.mark.parametrize(
    "draft",
    [
        object(),
        ResearchSynthesisDraft("", (), "Есть неопределённость.", (), ()),
        ResearchSynthesisDraft("Вывод.", (), "", (), ()),
        ResearchSynthesisDraft(
            "Вывод.",
            (ResearchClaimDraft("", ClaimKind.OBSERVED_FACT, ()),),
            "Неопределённость.",
            (),
            (),
        ),
        ResearchSynthesisDraft(
            "Вывод.",
            (ResearchClaimDraft("Факт.", "observed_fact", ()),),
            "Неопределённость.",
            (),
            (),
        ),
    ],
)
def test_synthesis_contract_rejects_malformed_untrusted_data(draft):
    with pytest.raises((TypeError, ValueError)):
        build_russian_synthesis(draft, ())


def test_synthesis_rejects_duplicate_source_references():
    draft = ResearchSynthesisDraft("Вывод.", (), "Неопределённость.", (), ())
    with pytest.raises(ValueError, match="duplicate source"):
        build_russian_synthesis(draft, (source(), source()))


@pytest.mark.parametrize(
    ("draft", "sources"),
    [
        (ResearchSynthesisDraft("Вывод.", (), "Неопределённость.", (), ()), []),
        (ResearchSynthesisDraft("Вывод.", [], "Неопределённость.", (), ()), ()),
        (ResearchSynthesisDraft("Вывод.", (), "Неопределённость.", [], ()), ()),
        (
            ResearchSynthesisDraft("Вывод.", (object(),), "Неопределённость.", (), ()),
            (),
        ),
        (
            ResearchSynthesisDraft(
                "Вывод.",
                (ResearchClaimDraft("Факт.", ClaimKind.OBSERVED_FACT, []),),
                "Неопределённость.",
                (),
                (),
            ),
            (),
        ),
        (
            ResearchSynthesisDraft(
                "x" * (16 * 1024 + 1), (), "Неопределённость.", (), ()
            ),
            (),
        ),
    ],
)
def test_synthesis_rejects_mutable_wrong_and_oversized_fields(draft, sources):
    with pytest.raises((TypeError, ValueError)):
        build_russian_synthesis(draft, sources)
