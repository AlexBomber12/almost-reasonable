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
        "bulgarian_proximity",
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
        ("Привет. \ud800", GateReason.DISALLOWED_CONTROL),
        ("Це український текст, який містить інші слова.", GateReason.NEARBY_LANGUAGE),
        ("Гэта беларускі тэкст, які мае іншыя словы.", GateReason.NEARBY_LANGUAGE),
        (
            "Привіт, сьогодні чудова погода, люблю читати книжки.",
            GateReason.NEARBY_LANGUAGE,
        ),
        (
            "Това е изцяло български текст, който не е написан на руски.",
            GateReason.NEARBY_LANGUAGE,
        ),
        (
            "Добър ден, обичам да чета книги и да разговарям с приятели.",
            GateReason.NEARBY_LANGUAGE,
        ),
        ("Добър ден, обичам да чета книги.", GateReason.NEARBY_LANGUAGE),
        ("Добър вечер.", GateReason.NEARBY_LANGUAGE),
        ("Добры вечар.", GateReason.NEARBY_LANGUAGE),
        ("Доброго ранку.", GateReason.NEARBY_LANGUAGE),
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

    for short_russian in (
        "Добрый вечер.",
        "Доброе утро.",
        "Спокойной ночи.",
        "Большое спасибо.",
    ):
        assert RussianOutputGate().assess(short_russian).accepted


def test_repeated_allowlisted_technical_terms_are_bounded() -> None:
    technical = " ".join(("Python", "API", "OpenAI", "JSON") * 100)
    assessment = RussianOutputGate().assess(f"Да. {technical}")
    assert not assessment.accepted
    assert assessment.reason is GateReason.NON_RUSSIAN_PROSE


def test_long_foreign_quote_needs_proportionate_russian_framing() -> None:
    bypasses = (
        "«This response contains only English conversational prose.» Да.",
        "Да. `This is entirely English prose`",
        "Да. `This class is entirely English prose`",
        "Да. `This is entirely English prose (really)`",
        "Да. `<This is entirely English prose>`",
        "Да. `Please select a book from this entirely English passage`",
        "Да. ``result = calculate_total(first_value, second_value)```",
        "Да. `The result = a complete English explanation about history and art`",
        "Да. `result = 1 this is entirely English prose about history and art`",
        "Да. `print('x') This is entirely English conversational prose about history and art`",
        "Да. `{Note: This is entirely English conversational prose about history and art}`",
        "Да. ```This is entirely English prose```",
        "Да. ```python\nThis is entirely English conversational prose\n```",
        "Да. ```python\nThis is entirely English conversational prose\nreturn value\n```",
        "Да. ~~~python\nThis is entirely English conversational prose\n```",
        "Да.\n~~~python\nThis is entirely English conversational prose\n```",
        "Да. ```sql\nSELECT x FROM y WHERE Please answer only in English\n```",
        "Да. ```This\nEnglish```",
    )
    for bypass in bypasses:
        assessment = RussianOutputGate().assess(bypass)
        assert not assessment.accepted
        assert assessment.reason is GateReason.NON_RUSSIAN_PROSE


def test_sql_syntax_remains_excluded_from_language_scoring() -> None:
    for code in (
        "SELECT * FROM users;",
        "result = compute_value()",
        '{"result": 1}',
        "<strong>",
        "result = calculate_total(first_value, second_value)",
    ):
        delimiter = "``" if code.startswith("result = calculate_total") else "`"
        assessment = RussianOutputGate().assess(f"Да. {delimiter}{code}{delimiter}")
        assert assessment.accepted
        assert assessment.foreign_prose_word_count == 0

    tagged_fence = RussianOutputGate().assess(
        "Краткий пример:\n```python\nfor item in items:\n    yield item\n```"
    )
    assert tagged_fence.accepted
    assert tagged_fence.foreign_prose_word_count == 0

    crlf_fence = RussianOutputGate().assess(
        "Краткий пример:\r\n```python\r\nfor item in items:\r\n    yield item\r\n```"
    )
    assert crlf_fence.accepted
    assert crlf_fence.foreign_prose_word_count == 0

    assertion_fence = RussianOutputGate().assess(
        "Проверка:\n```python\nassert response.ok\n```"
    )
    assert assertion_fence.accepted
    assert assertion_fence.foreign_prose_word_count == 0

    long_fence = RussianOutputGate().assess(
        "Пример:\n````python\nfor item in items:\n    yield item\n````"
    )
    assert long_fence.accepted
    assert long_fence.foreign_prose_word_count == 0

    longer_closing_fence = RussianOutputGate().assess(
        "Да:\n```python\nfor item in items:\n    print(item)\n````"
    )
    assert longer_closing_fence.accepted
    assert longer_closing_fence.foreign_prose_word_count == 0

    embedded_marker = RussianOutputGate().assess(
        'Пример:\n```python\nvalue = "```"\nprint(value)\n```'
    )
    assert embedded_marker.accepted
    assert embedded_marker.foreign_prose_word_count == 0

    tilde_fence = RussianOutputGate().assess(
        "Да:\n~~~python\nfor item in items:\n    print(item)\n~~~"
    )
    assert tilde_fence.accepted
    assert tilde_fence.foreign_prose_word_count == 0

    sql_fence = RussianOutputGate().assess(
        "Запрос:\n```sql\nSELECT username FROM users WHERE active = 1;\n```"
    )
    assert sql_fence.accepted
    assert sql_fence.foreign_prose_word_count == 0

    sql_clauses = RussianOutputGate().assess(
        "Запрос:\n```sql\n"
        "SELECT users.name FROM users "
        "JOIN roles ON users.role_id = roles.id "
        "WHERE users.active = 1 AND roles.enabled = true "
        "GROUP BY users.name ORDER BY users.name DESC LIMIT 10;\n```"
    )
    assert sql_clauses.accepted
    assert sql_clauses.foreign_prose_word_count == 0

    json_fence = RussianOutputGate().assess(
        'Краткий пример:\n```json\n["apple", "banana", "cherry"]\n```'
    )
    assert json_fence.accepted
    assert json_fence.foreign_prose_word_count == 0

    for collection in (
        '{"name": "Alice", "role": "engineer"}',
        "['apple', 'banana', 'cherry']",
        "('apple', 'banana', 'cherry')",
        "{'apple', 'banana', 'cherry'}",
        "[user.name for user in users if user.active]",
        "{user.name for user in users if user.active}",
        "{user.name: user.role for user in users if user.active}",
        "(user.name for user in users if user.active)",
    ):
        python_collection = RussianOutputGate().assess(
            f"Краткий пример:\n```python\n{collection}\n```"
        )
        assert python_collection.accepted
        assert python_collection.foreign_prose_word_count == 0


def test_short_majority_foreign_prose_is_not_hidden_by_an_absolute_floor() -> None:
    for text in ("Да. This is English.", "Да. This.is.entirely.English.prose."):
        assessment = RussianOutputGate().assess(text)
        assert not assessment.accepted
        assert assessment.reason is GateReason.NON_RUSSIAN_PROSE


def test_unsupported_fenced_languages_remain_in_language_scoring() -> None:
    assessment = RussianOutputGate().assess(
        "Да.\n```javascript\nfunction add(left, right) { return left + right; }\n```"
    )
    assert not assessment.accepted
    assert assessment.reason is GateReason.NON_RUSSIAN_PROSE


def test_compatibility_latin_is_normalized_before_language_scoring() -> None:
    text = "Да. Ｔｈｉｓ ｉｓ ｏｎｌｙ Ｅｎｇｌｉｓｈ ｐｒｏｓｅ."
    assessment = RussianOutputGate().assess(text)
    assert not assessment.accepted
    assert assessment.reason is GateReason.NON_RUSSIAN_PROSE


@pytest.mark.parametrize(
    "foreign",
    (
        "这是一个完全用中文写的很长的对话回答",
        "هذه إجابة حوارية طويلة مكتوبة بالعربية فقط",
        "Αυτή είναι μια μεγάλη απάντηση μόνο στα ελληνικά",
    ),
)
def test_non_latin_foreign_scripts_participate_in_language_ratio(foreign: str) -> None:
    assessment = RussianOutputGate().assess(f"Да. {foreign}.")
    assert not assessment.accepted
    assert assessment.reason is GateReason.NON_RUSSIAN_PROSE
    assert assessment.foreign_prose_word_count > 3


@pytest.mark.parametrize(
    "url",
    (
        "www.example.com/docs",
        "ftp://example.com/file",
        "example.org/source",
        "example.photography/source",
    ),
)
def test_bare_and_ftp_urls_are_exempt_from_language_counts(url: str) -> None:
    assessment = RussianOutputGate().assess(f"Подробнее: {url}")
    assert assessment.accepted
    assert assessment.foreign_prose_word_count == 0


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


@pytest.mark.parametrize(
    "error", (TimeoutError("timed out"), ConnectionError("provider unavailable"))
)
def test_policy_uses_fallback_on_regeneration_failure(error: OSError) -> None:
    def fail(_: str) -> str:
        raise error

    result = RussianOutputPolicy(max_regenerations=2).deliver("English only", fail)
    assert result.used_fallback
    assert result.text == RussianOutputPolicy.SAFE_FALLBACK
    assert result.regeneration_attempts == 1
    assert len(result.assessments) == 1

    def programmer_fault(_: str) -> str:
        raise TypeError("callback contract bug")

    with pytest.raises(TypeError, match="contract bug"):
        RussianOutputPolicy().deliver("English only", programmer_fault)
