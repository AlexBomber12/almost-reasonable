"""Unicode privacy screening must retain its semantics without hot character loops."""

import sys
import unicodedata

import pytest

from app.cognition import sharing


def legacy_normalize(value):
    normalized = unicodedata.normalize("NFKC", value).translate(
        sharing._IDNA_DOT_TRANSLATION
    )
    return "".join(
        " " if character.isspace() else character
        for character in normalized
        if character.isspace()
        or (
            unicodedata.category(character) not in {"Cc", "Cf"}
            and ord(character) not in sharing._DEFAULT_IGNORABLE_CODEPOINTS
        )
    )


def legacy_canonical(value):
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", legacy_normalize(value))
        if unicodedata.category(character) not in {"Mn", "Mc", "Me"}
    )


def test_text_screening_has_no_per_character_python_category_work(monkeypatch):
    original = unicodedata.category
    calls = []

    def counted(character):
        calls.append(True)
        return original(character)

    monkeypatch.setattr(unicodedata, "category", counted)
    text = "e\u0301\u00a0Hello\u200b " * 200
    assert sharing._normalize_privacy_text(text) == "é Hello " * 200
    assert sharing._canonical_identifier_text(text) == "e Hello " * 200
    assert len(calls) == 0, (
        "repeated graph checks classify every source character again"
    )


def test_normalization_matches_previous_screen_for_every_unicode_codepoint():
    for start in range(0, sys.maxunicode + 1, 4096):
        value = "".join(
            chr(code) for code in range(start, min(start + 4096, sys.maxunicode + 1))
        )
        assert sharing._normalize_privacy_text(value) == legacy_normalize(value)
        assert sharing._canonical_identifier_text(value) == legacy_canonical(value)


@pytest.mark.parametrize(
    "text",
    [
        "user\u200b@ｅxample\u3002com",
        "é\u0301\u034f@exámple.\u200bcom",
        "\u00a0\t\nИдентичность\u2060\u202e\r\u2029",
        "\u115f\u1160\u17b4\u3164\uffa0\U000e0001",
        "한글\u0301 क्\u200dष العربية\u061c",
        "👩\u200d💻\ufe0f\U000e0100\U0010ffff",
        "\ud800x\udfff",
        "",
    ],
)
def test_unicode_sequences_preserve_screening_and_marker_boundaries(text):
    assert sharing._normalize_privacy_text(text) == legacy_normalize(text)
    assert sharing._canonical_identifier_text(text) == legacy_canonical(text)
    assert sharing._normalized_words(text) == tuple(
        sharing._WORD.findall(legacy_canonical(text).casefold())
    )


def test_new_source_text_is_screened_on_each_call():
    from types import SimpleNamespace

    candidate = SimpleNamespace(
        value="Independent evidence supports alternative explanations"
    )
    source = SimpleNamespace(content="A synthetic unrelated private conversation")
    assert (
        sharing.screen_abstract_candidate(candidate, (source,)).decision.value
        == "allow"
    )
    source.content = candidate.value
    assert (
        sharing.screen_abstract_candidate(candidate, (source,)).decision.value
        == "reject"
    )
