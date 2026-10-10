import pytest

from app.behavior.russian_gate import (
    RUSSIAN_GATE_CORPUS_VERSION,
    RUSSIAN_GATE_VERSION,
    GateReason,
    RussianOutputGate,
    RussianOutputPolicy,
    corpus_cases,
)


def test_versioned_corpus_records_expected_tradeoffs() -> None:
    gate = RussianOutputGate()
    cases = corpus_cases()
    observed = {case.case_id: gate.assess(case.text).accepted for case in cases}

    assert RUSSIAN_GATE_VERSION == "russian-h-gate-v1"
    assert RUSSIAN_GATE_CORPUS_VERSION == "russian-h-corpus-v1"
    assert observed == {case.case_id: case.expected_accept for case in cases}
    assert {case.category for case in cases} == {
        "fluent_russian",
        "technical_english",
        "italian_names",
        "ukrainian_proximity",
        "belarusian_proximity",
        "english_only",
        "mixed_script",
        "unicode_controls",
        "foreign_quote",
        "code_and_url",
    }


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", GateReason.EMPTY),
        ("Hello, this response is entirely in English.", GateReason.NON_RUSSIAN_PROSE),
        ("Привет.\u200b Скрытый контроль.", GateReason.DISALLOWED_CONTROL),
        ("Це український текст, який містить інші слова.", GateReason.NEARBY_LANGUAGE),
        ("Гэта беларускі тэкст, які мае іншыя словы.", GateReason.NEARBY_LANGUAGE),
    ],
)
def test_gate_rejection_reasons(text: str, reason: GateReason) -> None:
    assessment = RussianOutputGate().assess(text)
    assert not assessment.accepted
    assert assessment.reason is reason


def test_gate_accepts_russian_technical_context_quote_code_and_url() -> None:
    text = (
        "В Python используйте async/await и API OpenAI. Итальянский город Reggio Emilia "
        "здесь лишь имя. Цитата: «The map is not the territory». Она показывает разницу "
        "между моделью и миром.\n```python\nprint('hello')\n```\n"
        "Источник: https://example.com/docs?q=test"
    )
    assessment = RussianOutputGate().assess(text)
    assert assessment.accepted
    assert assessment.reason is GateReason.ACCEPTED
    assert assessment.russian_word_count > assessment.foreign_prose_word_count


def test_long_foreign_quote_needs_proportionate_russian_framing() -> None:
    bypass = "«This response contains only English conversational prose.» Да."
    assessment = RussianOutputGate().assess(bypass)
    assert not assessment.accepted
    assert assessment.reason is GateReason.NON_RUSSIAN_PROSE


def test_policy_regenerates_with_strict_limit_then_falls_back() -> None:
    drafts = iter(("Still English only.", "Теперь ответ полностью по-русски."))
    calls: list[str] = []

    def regenerate(rejected: str) -> str:
        calls.append(rejected)
        return next(drafts)

    policy = RussianOutputPolicy(max_regenerations=2)
    delivered = policy.deliver("This is English prose.", regenerate)
    assert delivered.text == "Теперь ответ полностью по-русски."
    assert delivered.regeneration_attempts == 2
    assert not delivered.used_fallback
    assert len(calls) == 2

    fallback = RussianOutputPolicy(max_regenerations=1).deliver(
        "English response.", lambda _: "Still not Russian."
    )
    assert fallback.used_fallback
    assert fallback.text == RussianOutputPolicy.SAFE_FALLBACK
    assert fallback.regeneration_attempts == 1

    unavailable = RussianOutputPolicy(max_regenerations=2).deliver(
        "English response.", None
    )
    assert unavailable.used_fallback
    assert unavailable.regeneration_attempts == 0


def test_policy_does_not_regenerate_valid_text_and_validates_limit() -> None:
    result = RussianOutputPolicy(max_regenerations=0).deliver("Это нормальный ответ.")
    assert result.text == "Это нормальный ответ."
    assert not result.used_fallback
    assert result.assessments[0].accepted
    immediate = RussianOutputPolicy(max_regenerations=0).deliver("English only")
    assert immediate.used_fallback
    with pytest.raises(ValueError, match="max_regenerations"):
        RussianOutputPolicy(max_regenerations=3)
