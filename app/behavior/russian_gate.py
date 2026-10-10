"""Versioned deterministic H-class gate for conversational Russian output."""

import ast
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import json
import math
import re
import unicodedata


RUSSIAN_GATE_VERSION = "russian-h-gate-v1"
RUSSIAN_GATE_CORPUS_VERSION = "russian-h-corpus-v1"


class GateReason(StrEnum):
    ACCEPTED = "accepted"
    EMPTY = "empty"
    DISALLOWED_CONTROL = "disallowed_control"
    NON_RUSSIAN_PROSE = "non_russian_prose"
    NEARBY_LANGUAGE = "nearby_language"


@dataclass(frozen=True, slots=True)
class GateAssessment:
    version: str
    accepted: bool
    reason: GateReason
    russian_word_count: int
    foreign_prose_word_count: int


@dataclass(frozen=True, slots=True)
class RussianCorpusCase:
    case_id: str
    category: str
    text: str
    expected_accept: bool
    limitation_note: str


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    text: str
    used_fallback: bool
    regeneration_attempts: int
    assessments: tuple[GateAssessment, ...]


_URL = re.compile(
    r"(?<![\w@])(?:(?:(?:https?|ftp)://|www\.)[^\s<>()]+|"
    r"(?:[a-z0-9-]+\.)+(?:com|org|net|edu|gov|io|ai|ru|it|de|fr|uk|"
    r"dev|app|info|biz|me|co|photography|xn--[a-z0-9-]{2,59})"
    r"(?:/[^\s<>()]*)?)",
    re.IGNORECASE,
)
_FENCED_CODE = re.compile(
    r"(?P<fence>`{3,}|~{3,})(?:(?P<language>[A-Za-z0-9_+#.-]+)[ \t]*\r?\n)?"
    r"(?P<content>.*?)(?P=fence)(?![`~])",
    re.DOTALL,
)
_INLINE_CODE = re.compile(
    r"(?<!`)(?P<fence>`+)(?!`)(?P<content>[\s\S]*?)(?<!`)(?P=fence)(?!`)"
)
_SUPPORTED_FENCED_CODE_LANGUAGES = frozenset(
    (
        "json",
        "python",
        "sql",
    )
)
_SQL_CODE_SIGNAL = re.compile(
    r"^\s*select\s+(?:\*|[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)"
    r"\s+from\s+[A-Za-z_]\w*"
    r"(?:\s+(?:(?:where|join|limit)\s+|(?:group|order)\s+by\s+)[^;\r\n]+)*"
    r"\s*;?\s*$",
    re.IGNORECASE,
)
_HTML_CODE_SIGNAL = re.compile(r"^\s*</?[A-Za-z][^>]*>\s*$")
_CYRILLIC_WORD = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґЎў]+")
_RUSSIAN_CYRILLIC_LETTER = re.compile(r"[А-Яа-яЁё]")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
_UKRAINIAN_WORDS = frozenset(
    (
        "це",
        "який",
        "яка",
        "яке",
        "містить",
        "інші",
        "український",
        "та",
        "привіт",
        "сьогодні",
        "чудова",
        "читати",
        "книжки",
    )
)
_BELARUSIAN_WORDS = frozenset(
    ("гэта", "які", "якая", "мае", "іншыя", "беларускі", "тэкст")
)
_BULGARIAN_WORDS = frozenset(
    ("това", "изцяло", "български", "който", "написан", "защото", "също")
)
_NEARBY_SHORT_REPLIES = frozenset(
    (
        "добър вечер",
        "добър ден",
        "добро утро",
        "лека нощ",
        "много благодаря",
        "добры вечар",
        "добрай раніцы",
        "да пабачэння",
        "вялікі дзякуй",
        "доброго ранку",
        "добрий ранок",
        "добрий вечір",
        "щиро дякую",
    )
)
_LANGUAGE_REFERENCE_TEXT = {
    "ru": (
        "сегодня хорошая погода я люблю читать книги и разговаривать с друзьями "
        "этот ответ написан по русски и подробно объясняет основную мысль "
        "проверяемые доказательства помогают сделать осторожный вывод "
        "содержательный диалог требует ясных аргументов"
    ),
    "uk": (
        "сьогодні гарна погода я люблю читати книжки та розмовляти з друзями "
        "ця відповідь написана українською і докладно пояснює головну думку "
        "перевірені докази допомагають зробити обережний висновок "
        "змістовний діалог потребує ясних аргументів"
    ),
    "be": (
        "сёння добрае надвор е я люблю чытаць кнігі і размаўляць з сябрамі "
        "гэты адказ напісаны па беларуску і падрабязна тлумачыць асноўную думку "
        "правераныя доказы дапамагаюць зрабіць асцярожную выснову "
        "змястоўны дыялог патрабуе ясных аргументаў"
    ),
    "bg": (
        "днес времето е хубаво обичам да чета книги и да разговарям с приятели "
        "този отговор е написан на български и подробно обяснява основната мисъл "
        "проверените доказателства помагат да направим предпазлив извод "
        "съдържателният диалог изисква ясни аргументи"
    ),
}
_COMMON_LATIN_TERMS = frozenset(
    (
        "api",
        "async",
        "await",
        "openai",
        "python",
        "json",
        "http",
        "https",
        "sql",
        "sqlite",
        "threads",
        "linux",
    )
)


class RussianOutputGate:
    """Conservative corpus-calibrated heuristic; it is not authorization."""

    def assess(self, text: object) -> GateAssessment:
        if not isinstance(text, str) or not text.strip():
            return _assessment(False, GateReason.EMPTY, 0, 0)
        normalized = unicodedata.normalize("NFKC", text)
        if any(_is_disallowed_control(character) for character in normalized):
            return _assessment(False, GateReason.DISALLOWED_CONTROL, 0, 0)

        prose = _strip_non_prose_regions(normalized)
        cyrillic = [word.casefold() for word in _CYRILLIC_WORD.findall(prose)]
        latin = [word.casefold() for word in _LATIN_WORD.findall(prose)]
        nearby_score = max(
            sum(word in markers for word in cyrillic)
            for markers in (
                _UKRAINIAN_WORDS,
                _BELARUSIAN_WORDS,
                _BULGARIAN_WORDS,
            )
        )
        nearby_letters = sum(character in "іїєґўІЇЄҐЎ" for character in prose)
        ordinary_latin = sum(word not in _COMMON_LATIN_TERMS for word in latin)
        technical_latin = len(latin) - ordinary_latin
        technical_allowance = min(8, max(2, len(cyrillic) // 2))
        foreign = ordinary_latin + max(0, technical_latin - technical_allowance)
        foreign += _foreign_script_units(prose)
        if (
            nearby_letters >= 2
            or nearby_score >= 2
            or " ".join(cyrillic) in _NEARBY_SHORT_REPLIES
            or _matches_nearby_cyrillic_profile(cyrillic)
        ):
            return _assessment(
                False, GateReason.NEARBY_LANGUAGE, len(cyrillic), foreign
            )
        if not cyrillic or foreign > len(cyrillic):
            return _assessment(
                False, GateReason.NON_RUSSIAN_PROSE, len(cyrillic), foreign
            )
        return _assessment(True, GateReason.ACCEPTED, len(cyrillic), foreign)


class RussianOutputPolicy:
    """Bounded regenerate-or-application-fallback delivery policy."""

    SAFE_FALLBACK = (
        "Не удалось подготовить корректный ответ по-русски. Попробуем позже."
    )

    def __init__(self, max_regenerations: int = 1):
        if type(max_regenerations) is not int or not 0 <= max_regenerations <= 2:
            raise ValueError("max_regenerations must be an integer from 0 to 2")
        self._max_regenerations = max_regenerations
        self._gate = RussianOutputGate()

    def deliver(
        self,
        draft: str,
        regenerate: Callable[[str], str] | None = None,
    ) -> DeliveryResult:
        current = draft
        assessments: list[GateAssessment] = []
        attempt = 0
        while True:
            assessment = self._gate.assess(current)
            assessments.append(assessment)
            if assessment.accepted:
                return DeliveryResult(current, False, attempt, tuple(assessments))
            if regenerate is None or attempt == self._max_regenerations:
                break
            attempt += 1
            try:
                current = regenerate(current)
            except (OSError, ValueError):
                break
        return DeliveryResult(
            self.SAFE_FALLBACK,
            True,
            attempt,
            tuple(assessments),
        )


def corpus_cases() -> tuple[RussianCorpusCase, ...]:
    """Frozen examples publish known decisions, not a claim of perfect detection."""

    return (
        RussianCorpusCase(
            "ru-fluent",
            "fluent_russian",
            "Мне кажется, этот вывод требует более сильных доказательств.",
            True,
            "Короткая естественная русская проза.",
        ),
        RussianCorpusCase(
            "ru-tech",
            "technical_english",
            "В Python удобно использовать async/await для API OpenAI.",
            True,
            "Технические токены исключаются из латинской прозы.",
        ),
        RussianCorpusCase(
            "ru-italy",
            "italian_names",
            "Маршрут проходит через Reggio Emilia и Piazza Navona в Италии.",
            True,
            "Несколько имён допустимы при достаточном русском контексте.",
        ),
        RussianCorpusCase(
            "uk-near",
            "ukrainian_proximity",
            "Це український текст, який містить інші слова.",
            False,
            "Близкие языки дают неизбежные пограничные ошибки.",
        ),
        RussianCorpusCase(
            "be-near",
            "belarusian_proximity",
            "Гэта беларускі тэкст, які мае іншыя словы.",
            False,
            "Эвристика использует ограниченный список маркеров.",
        ),
        RussianCorpusCase(
            "bg-near",
            "bulgarian_proximity",
            "Това е изцяло български текст, който не е написан на руски.",
            False,
            "Общие кириллические буквы требуют лексических маркеров.",
        ),
        RussianCorpusCase(
            "en-only",
            "english_only",
            "This answer contains only English conversational prose.",
            False,
            "Латинская проза без кириллицы отклоняется.",
        ),
        RussianCorpusCase(
            "mixed-prose",
            "mixed_script",
            "Привет. This is a long conversational answer in English.",
            False,
            "Короткий русский маркер не маскирует длинную иностранную прозу.",
        ),
        RussianCorpusCase(
            "unicode-control",
            "unicode_controls",
            "Нормальный\u200b ответ со скрытым символом.",
            False,
            "Невидимые форматирующие символы отклоняются консервативно.",
        ),
        RussianCorpusCase(
            "foreign-quote",
            "foreign_quote",
            "Автор пишет: «The map is not the territory». Это уместная цитата с пояснением.",
            True,
            "Атрибутированность и необходимость остаются M-вопросом.",
        ),
        RussianCorpusCase(
            "code-url",
            "code_and_url",
            "Пример кода: `print('ok')`. Документация: https://example.com/docs",
            True,
            "Код и URL не считаются разговорной прозой.",
        ),
    )


def _strip_non_prose_regions(text: str) -> str:
    without_code = _FENCED_CODE.sub(_replace_fenced_code_span, text)
    without_code = _INLINE_CODE.sub(_replace_code_span, without_code)
    return _URL.sub(" ", without_code)


def _replace_fenced_code_span(match: re.Match[str]) -> str:
    language = match.group("language")
    content = match.group("content")
    if (
        language is not None
        and language.casefold() not in _SUPPORTED_FENCED_CODE_LANGUAGES
    ):
        content = f"{language}\n{content}"
    return " " if _has_complete_code_signal(content, language) else content


def _replace_code_span(match: re.Match[str]) -> str:
    content = match.group("content")
    return " " if _has_complete_code_signal(content) else content


def _has_complete_code_signal(content: str, language: str | None = None) -> bool:
    normalized_language = language.casefold() if language is not None else None
    if normalized_language == "json":
        return _is_json_data(content)
    if normalized_language == "sql":
        return _SQL_CODE_SIGNAL.fullmatch(content) is not None
    if normalized_language is None and (
        _is_json_data(content)
        or _SQL_CODE_SIGNAL.fullmatch(content)
        or _HTML_CODE_SIGNAL.fullmatch(content)
    ):
        return True
    try:
        parsed = ast.parse(content, mode="exec")
    except SyntaxError:
        return False
    code_nodes = (
        ast.Assign,
        ast.AnnAssign,
        ast.Assert,
        ast.AsyncFor,
        ast.AsyncFunctionDef,
        ast.AsyncWith,
        ast.AugAssign,
        ast.Break,
        ast.ClassDef,
        ast.Continue,
        ast.Delete,
        ast.FunctionDef,
        ast.For,
        ast.Global,
        ast.If,
        ast.Import,
        ast.ImportFrom,
        ast.Match,
        ast.Nonlocal,
        ast.Pass,
        ast.Raise,
        ast.Return,
        ast.Try,
        ast.TryStar,
        ast.While,
        ast.With,
    )
    expression_nodes: tuple[type[ast.expr], ...] = (ast.Call,)
    if normalized_language == "python":
        expression_nodes = (ast.Call, ast.Dict, ast.List, ast.Set, ast.Tuple)
    return bool(parsed.body) and all(
        isinstance(node, code_nodes)
        or isinstance(node, ast.Expr)
        and isinstance(node.value, expression_nodes)
        for node in parsed.body
    )


def _is_json_data(content: str) -> bool:
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return False
    return type(value) in (dict, list)


def _foreign_script_units(text: str) -> int:
    letters = sum(
        unicodedata.category(character).startswith("L")
        and not ("A" <= character <= "Z" or "a" <= character <= "z")
        and _RUSSIAN_CYRILLIC_LETTER.fullmatch(character) is None
        for character in text
    )
    return math.ceil(letters / 4)


def _matches_nearby_cyrillic_profile(words: list[str]) -> bool:
    if len(words) < 3:
        return False
    observed = _trigram_profile(" ".join(words))
    scores = {
        language: _cosine_similarity(observed, _trigram_profile(reference))
        for language, reference in _LANGUAGE_REFERENCE_TEXT.items()
    }
    winner = max(scores, key=scores.__getitem__)
    return (
        winner != "ru"
        and scores[winner] >= 0.18
        and scores[winner] - scores["ru"] >= 0.08
    )


def _trigram_profile(text: str) -> Counter[str]:
    normalized = " ".join(_CYRILLIC_WORD.findall(text.casefold()))
    padded = f" {normalized} "
    return Counter(padded[index : index + 3] for index in range(len(padded) - 2))


def _cosine_similarity(left: Counter[str], right: Counter[str]) -> float:
    numerator = sum(count * right[trigram] for trigram, count in left.items())
    left_norm = math.sqrt(sum(count * count for count in left.values()))
    right_norm = math.sqrt(sum(count * count for count in right.values()))
    return numerator / (left_norm * right_norm)


def _is_disallowed_control(character: str) -> bool:
    return character not in "\n\r\t" and unicodedata.category(character) in (
        "Cc",
        "Cf",
        "Cs",
    )


def _assessment(
    accepted: bool, reason: GateReason, russian: int, foreign: int
) -> GateAssessment:
    return GateAssessment(RUSSIAN_GATE_VERSION, accepted, reason, russian, foreign)
