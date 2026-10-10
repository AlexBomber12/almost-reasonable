from dataclasses import replace
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
    MAX_EVIDENCE_REFERENCES_PER_CLAIM,
    MAX_SYNTHESIS_CLAIMS,
    MAX_SYNTHESIS_CONTRADICTIONS,
    MAX_SYNTHESIS_UNVERIFIED_CLAIMS,
    MAX_SYNTHESIS_UTF8_BYTES,
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
    with pytest.raises(TypeError):
        replace(result, requires_shared_russian_output_gate=False)


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
        ResearchSynthesisDraft(object(), (), "Есть неопределённость.", (), ()),
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
        (
            ResearchSynthesisDraft("Вывод.", (), "Неопределённость.", (), ()),
            (object(),),
        ),
        (ResearchSynthesisDraft("Вывод.", [], "Неопределённость.", (), ()), ()),
        (ResearchSynthesisDraft("Вывод.", (), "Неопределённость.", [], ()), ()),
        (ResearchSynthesisDraft("Вывод.", (), "Неопределённость.", (), []), ()),
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
        (
            ResearchSynthesisDraft("invalid \ud800", (), "Неопределённость.", (), ()),
            (),
        ),
    ],
)
def test_synthesis_rejects_mutable_wrong_and_oversized_fields(draft, sources):
    with pytest.raises((TypeError, ValueError)):
        build_russian_synthesis(draft, sources)


def test_maximum_synthesis_collection_sizes_are_accepted():
    references = tuple(
        f"missing-{index}" for index in range(MAX_EVIDENCE_REFERENCES_PER_CLAIM)
    )
    draft = ResearchSynthesisDraft(
        "Вывод.",
        tuple(
            ResearchClaimDraft(
                f"Утверждение {index}.", ClaimKind.OBSERVED_FACT, references
            )
            for index in range(MAX_SYNTHESIS_CLAIMS)
        ),
        "Неопределённость.",
        tuple(
            f"Противоречие {index}." for index in range(MAX_SYNTHESIS_CONTRADICTIONS)
        ),
        tuple(
            f"Непроверенное {index}."
            for index in range(MAX_SYNTHESIS_UNVERIFIED_CLAIMS)
        ),
    )

    result = build_russian_synthesis(draft, ())

    assert len(result.claims) == MAX_SYNTHESIS_CLAIMS
    assert len(result.contradictions) == MAX_SYNTHESIS_CONTRADICTIONS
    assert len(result.unverified_claims) == (
        MAX_SYNTHESIS_UNVERIFIED_CLAIMS + MAX_SYNTHESIS_CLAIMS
    )


@pytest.mark.parametrize(
    ("draft", "message"),
    [
        (
            ResearchSynthesisDraft(
                "Вывод.",
                tuple(object() for _ in range(MAX_SYNTHESIS_CLAIMS + 1)),
                "Неопределённость.",
                (),
                (),
            ),
            "claim count bound",
        ),
        (
            ResearchSynthesisDraft(
                "Вывод.",
                (),
                "Неопределённость.",
                tuple(object() for _ in range(MAX_SYNTHESIS_CONTRADICTIONS + 1)),
                (),
            ),
            "contradiction count bound",
        ),
        (
            ResearchSynthesisDraft(
                "Вывод.",
                (),
                "Неопределённость.",
                (),
                tuple(object() for _ in range(MAX_SYNTHESIS_UNVERIFIED_CLAIMS + 1)),
            ),
            "unverified claim count bound",
        ),
        (
            ResearchSynthesisDraft(
                "Вывод.",
                (
                    ResearchClaimDraft(
                        "Факт.",
                        ClaimKind.OBSERVED_FACT,
                        tuple(
                            object()
                            for _ in range(MAX_EVIDENCE_REFERENCES_PER_CLAIM + 1)
                        ),
                    ),
                ),
                "Неопределённость.",
                (),
                (),
            ),
            "evidence reference count bound",
        ),
    ],
)
def test_oversized_synthesis_collections_fail_before_item_processing(draft, message):
    with pytest.raises(ValueError, match=message):
        build_russian_synthesis(draft, ())


def test_aggregate_synthesis_payload_is_bounded():
    per_claim = "я" * 2000
    draft = ResearchSynthesisDraft(
        "Вывод.",
        tuple(
            ResearchClaimDraft(f"{per_claim}{index}", ClaimKind.INTERPRETATION, ())
            for index in range(MAX_SYNTHESIS_CLAIMS)
        ),
        "Неопределённость.",
        (),
        (),
    )
    assert sum(len(claim.text.encode("utf-8")) for claim in draft.claims) > (
        MAX_SYNTHESIS_UTF8_BYTES
    )

    with pytest.raises(ValueError, match="aggregate byte bound"):
        build_russian_synthesis(draft, ())


def test_unverified_claim_deduplication_preserves_first_occurrence_order():
    draft = ResearchSynthesisDraft(
        "Вывод.",
        (
            ResearchClaimDraft("Второе.", ClaimKind.OBSERVED_FACT, ()),
            ResearchClaimDraft("Третье.", ClaimKind.INTERPRETATION, ("missing",)),
            ResearchClaimDraft("Второе.", ClaimKind.INTERPRETATION, ()),
        ),
        "Неопределённость.",
        (),
        ("Первое.", "Первое.", "Второе."),
    )

    result = build_russian_synthesis(draft, ())

    assert result.unverified_claims == ("Первое.", "Второе.", "Третье.")


@pytest.mark.parametrize(
    "references",
    [
        ("",),
        (3,),
        ["source-1"],
    ],
)
def test_invalid_evidence_reference_collections_are_rejected(references):
    draft = ResearchSynthesisDraft(
        "Вывод.",
        (ResearchClaimDraft("Факт.", ClaimKind.OBSERVED_FACT, references),),
        "Неопределённость.",
        (),
        (),
    )

    with pytest.raises(TypeError, match="evidence references"):
        build_russian_synthesis(draft, ())
