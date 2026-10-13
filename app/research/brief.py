"""Closed research-brief records and bounded privacy screening."""

from dataclasses import dataclass, field
from enum import StrEnum
import base64
import binascii
import hmac
import json
import re
import secrets
import unicodedata
from urllib.parse import unquote


MAX_BRIEF_UTF8_BYTES = 8 * 1024
MAX_FIELD_UTF8_BYTES = 2 * 1024
MAX_RESULT_BUDGET = 10
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*\Z")
_PUBLIC_REFERENCE = re.compile(
    r"(?i)(?:doi|arxiv|pmid|isbn):[A-Za-z0-9][A-Za-z0-9./_()-]{0,255}\Z"
)
_EMAIL = re.compile(
    r"(?i)(?<![\w.!#$%&'*+/=?^_`{|}~\"-])"
    r"(?:\"(?:[^\"\\\r\n]|\\.)*\"|[\w.!#$%&'*+/=?^_`{|}~-]+)@"
    r"(?:(?:[\w-]+\.)*[\w-]+|\[[^\]\s]+\])"
)
_EMAIL_COMMENT = re.compile(r"\([^()\r\n]*\)")
_PHONE = re.compile(r"(?<![^\W_])(?:\+?\d[\d\s()./–—-]{5,}\d)(?![^\W_])")
_YEAR_RANGE = re.compile(
    r"(?<!\d)(?:1[5-9]\d{2}|20\d{2}|21\d{2})\s*[-/–—]\s*"
    r"(?:1[5-9]\d{2}|20\d{2}|21\d{2})(?!\d)"
)
_CALENDAR_DATE = re.compile(
    r"(?<!\d)(?:"
    r"\d{4}[-./](?:0?[1-9]|1[0-2])[-./](?:0?[1-9]|[12]\d|3[01])"
    r"|(?:0?[1-9]|[12]\d|3[01])[-./](?:0?[1-9]|1[0-2])[-./]\d{4}"
    r"|(?:0?[1-9]|1[0-2])[-./](?:0?[1-9]|[12]\d|3[01])[-./]\d{4}"
    r")(?!\d)"
)
_PUBLIC_GROUPED_QUANTITY = re.compile(
    r"(?i)(?:populations?|quantit(?:y|ies)|amounts?|totals?)\s+"
    r"(?:(?:over|under|above|below|of)\s+)?"
    r"[1-9]\d{0,2}(?:[ .\u00a0\u202f]\d{3})+"
    r"(?!\d)(?![ .\u00a0\u202f]\d)"
)
_BASE64 = re.compile(
    r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{2,}={0,2}(?![A-Za-z0-9+/_=-])"
)
_PATH_BASE64 = re.compile(r"(?=/([A-Za-z0-9+/_-]{2,}={0,2}(?![A-Za-z0-9+/_=-])))")
_FOLDED_BASE64 = re.compile(
    r"(?<![A-Za-z0-9+/_-])"
    r"(?:[A-Za-z0-9+/_-]+ +)+[A-Za-z0-9+/_-]+={0,2}"
    r"(?![A-Za-z0-9+/_=-])"
)
_PATH_FOLDED_BASE64 = re.compile(
    r"(?=/((?:[A-Za-z0-9+/_-]+ +)+[A-Za-z0-9+/_-]+={0,2}"
    r"(?![A-Za-z0-9+/_=-])))"
)
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_CONFUSABLES = str.maketrans(
    {
        "А": "A",
        "В": "B",
        "С": "C",
        "Е": "E",
        "Н": "H",
        "К": "K",
        "М": "M",
        "О": "O",
        "Р": "P",
        "Т": "T",
        "Х": "X",
        "а": "a",
        "с": "c",
        "е": "e",
        "о": "o",
        "р": "p",
        "х": "x",
    }
)
_INJECTION_PATTERNS = (
    "ignore previous instructions",
    "reveal the system prompt",
    "developer message",
    "execute shell",
    "run shell",
    "игнорируй предыдущие инструкции",
    "раскрой системный промпт",
    "ignora le istruzioni precedenti",
)
_BRIEF_AUTHORITY = object()
_BRIEF_INTEGRITY_KEY = secrets.token_bytes(32)
_MAX_ENCODED_SCREEN_DEPTH = 4
_MAX_ENCODED_REPAIR_WORK = 16 * 1024 * 1024
_NON_SEGMENTED_SCRIPT_PREFIXES = (
    "CJK ",
    "HIRAGANA ",
    "KATAKANA ",
    "HANGUL ",
    "THAI ",
    "LAO ",
    "KHMER ",
    "MYANMAR ",
)


class BriefDisposition(StrEnum):
    READY = "ready"
    DECLINED = "declined"
    DEFERRED = "deferred"


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {field}")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        raise ValueError(f"invalid {field}")
    return value.strip()


@dataclass(frozen=True, slots=True)
class ResearchQuestion:
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _require_text(self.text, "research question"))


@dataclass(frozen=True, slots=True)
class BroadTopic:
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _require_text(self.text, "broad topic"))


@dataclass(frozen=True, slots=True)
class ResearchPurpose:
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _require_text(self.text, "research purpose"))


@dataclass(frozen=True, slots=True)
class TimeRequirement:
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _require_text(self.text, "time requirement"))


@dataclass(frozen=True, slots=True)
class PublicReference:
    identifier: str

    def __post_init__(self) -> None:
        value = _require_text(self.identifier, "public reference identifier")
        if _PUBLIC_REFERENCE.fullmatch(value) is None:
            raise ValueError("invalid public reference identifier")
        object.__setattr__(self, "identifier", value)


@dataclass(frozen=True, slots=True)
class LanguagePreferences:
    languages: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.languages) is not tuple or not self.languages:
            raise TypeError("language preferences must be a non-empty tuple")
        if len(self.languages) > 8 or any(
            not isinstance(value, str) or _LANGUAGE_TAG.fullmatch(value) is None
            for value in self.languages
        ):
            raise ValueError("invalid language preferences")


@dataclass(frozen=True, slots=True, repr=False)
class PrivatePayloadRecord:
    """An explicit negative type used to prove private records fail closed."""

    payload: str


@dataclass(frozen=True, slots=True)
class ResearchRecordSet:
    records: tuple[object, ...]

    def __post_init__(self) -> None:
        if type(self.records) is not tuple:
            raise TypeError("research records must be a tuple")


@dataclass(frozen=True, slots=True, repr=False)
class ResearchScreeningContext:
    """Trusted comparison material that is never serialized into a brief."""

    private_canaries: tuple[str, ...] = ()
    participant_identifiers: tuple[str, ...] = ()
    private_quotations: tuple[str, ...] = ()
    sensitive_details: tuple[str, ...] = ()
    private_texts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value in (
            self.private_canaries,
            self.participant_identifiers,
            self.private_quotations,
            self.sensitive_details,
            self.private_texts,
        ):
            if type(value) is not tuple:
                raise TypeError("screening collections must be tuples")
            if any(
                not isinstance(item, str) or not item or not _normalized(item)
                for item in value
            ):
                raise ValueError("screening entries must be non-empty strings")


@dataclass(frozen=True, slots=True)
class ResearchBrief:
    question: str
    broad_topic: str | None
    purpose: str | None
    language_preferences: tuple[str, ...]
    time_requirement: str | None
    public_reference_identifiers: tuple[str, ...]
    result_budget: int
    trusted_config_version: str
    _authority: object = field(repr=False, compare=False)
    _integrity: bytes = field(default=b"", repr=False, compare=False)

    @property
    def built_by_screened_pipeline(self) -> bool:
        """Confirm construction integrity, not external-transmission authority."""
        if self._authority is not _BRIEF_AUTHORITY or not isinstance(
            self._integrity, bytes
        ):
            return False
        try:
            expected = _brief_integrity(self.to_json())
        except (TypeError, ValueError):
            return False
        return hmac.compare_digest(self._integrity, expected)

    def to_dict(self) -> dict[str, object]:
        return {
            "question": self.question,
            "broad_topic": self.broad_topic,
            "purpose": self.purpose,
            "language_preferences": list(self.language_preferences),
            "time_requirement": self.time_requirement,
            "public_reference_identifiers": list(self.public_reference_identifiers),
            "result_budget": self.result_budget,
            "trusted_config_version": self.trusted_config_version,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )


@dataclass(frozen=True, slots=True)
class BriefBuildResult:
    disposition: BriefDisposition
    reason: str | None
    brief: ResearchBrief | None


_ALLOWED_RECORD_TYPES = (
    ResearchQuestion,
    BroadTopic,
    ResearchPurpose,
    LanguagePreferences,
    TimeRequirement,
    PublicReference,
)


def build_safe_research_brief(
    record_set: ResearchRecordSet,
    *,
    result_budget: int,
    trusted_config_version: str,
    screening: ResearchScreeningContext,
) -> BriefBuildResult:
    """Select approved records, screen the result, and return no provenance."""

    if type(record_set) is not ResearchRecordSet:
        raise TypeError("record_set must be a ResearchRecordSet")
    if type(record_set.records) is not tuple:
        raise TypeError("research records must be a tuple")
    if type(screening) is not ResearchScreeningContext:
        raise TypeError("screening must be a ResearchScreeningContext")
    if type(result_budget) is not int or not 1 <= result_budget <= MAX_RESULT_BUDGET:
        raise ValueError("invalid result budget")
    if (
        not isinstance(trusted_config_version, str)
        or _IDENTIFIER.fullmatch(trusted_config_version) is None
    ):
        raise ValueError("invalid trusted configuration version")

    selected: dict[type[object], object] = {}
    references: list[str] = []
    for record in record_set.records:
        if type(record) is PrivatePayloadRecord:
            return _decline("private_record_type")
        if type(record) not in _ALLOWED_RECORD_TYPES:
            return _decline("unsupported_record_type")
        if type(record) is PublicReference:
            references.append(record.identifier)
            continue
        if type(record) in selected:
            return _decline("duplicate_field")
        selected[type(record)] = record

    question_record = selected.get(ResearchQuestion)
    if question_record is None:
        return _decline("missing_question")
    assert type(question_record) is ResearchQuestion
    topic = selected.get(BroadTopic)
    purpose = selected.get(ResearchPurpose)
    languages = selected.get(LanguagePreferences)
    time_requirement = selected.get(TimeRequirement)
    free_text_values = [
        question_record.text,
        *(record.text for record in (topic, purpose, time_requirement) if record),
    ]
    language_values = list(languages.languages) if languages else []
    values = [
        *free_text_values,
        *language_values,
        *references,
    ]
    if any(len(value.encode("utf-8")) > MAX_FIELD_UTF8_BYTES for value in values):
        return _decline("field_too_large")
    if sum(len(value.encode("utf-8")) for value in values) > MAX_BRIEF_UTF8_BYTES:
        return _decline("brief_too_large")

    reason = _screen(values, screening, identifier_values=free_text_values)
    if reason is not None:
        return _decline(reason)

    brief = ResearchBrief(
        question=question_record.text,
        broad_topic=topic.text if type(topic) is BroadTopic else None,
        purpose=purpose.text if type(purpose) is ResearchPurpose else None,
        language_preferences=(
            languages.languages if type(languages) is LanguagePreferences else ()
        ),
        time_requirement=(
            time_requirement.text if type(time_requirement) is TimeRequirement else None
        ),
        public_reference_identifiers=tuple(references),
        result_budget=result_budget,
        trusted_config_version=trusted_config_version,
        _authority=_BRIEF_AUTHORITY,
    )
    object.__setattr__(brief, "_integrity", _brief_integrity(brief.to_json()))
    if len(brief.to_json().encode("utf-8")) > MAX_BRIEF_UTF8_BYTES:
        return _decline("brief_too_large")
    return BriefBuildResult(BriefDisposition.READY, None, brief)


def _decline(reason: str) -> BriefBuildResult:
    return BriefBuildResult(BriefDisposition.DECLINED, reason, None)


def _normalized(value: str) -> str:
    value = "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if unicodedata.category(character) not in {"Cf", "Mn"}
    )
    return " ".join(value.translate(_CONFUSABLES).casefold().split())


def _contains(values: list[str], needles: tuple[str, ...]) -> bool:
    normalized_values = tuple(_normalized(value) for value in values)
    return any(
        _normalized(needle) in value
        for needle in needles
        for value in normalized_values
    )


def _contains_at_boundaries(values: list[str], needles: tuple[str, ...]) -> bool:
    normalized_values = tuple(_normalized(value) for value in values)
    return any(
        _participant_identifier_matches(value, _normalized(needle))
        for needle in needles
        for value in normalized_values
    )


def _participant_identifier_matches(value: str, needle: str) -> bool:
    nonsegmented = any(
        unicodedata.name(character, "").startswith(_NON_SEGMENTED_SCRIPT_PREFIXES)
        for character in needle
    )
    left_boundary = not nonsegmented and bool(re.match(r"[^\W_]", needle[0]))
    right_boundary = not nonsegmented and bool(re.match(r"[^\W_]", needle[-1]))
    exact_left = r"(?<![^\W_])" if left_boundary else ""
    exact_right = r"(?![^\W_])" if right_boundary else ""
    if re.search(rf"{exact_left}{re.escape(needle)}{exact_right}", value):
        return True
    compact = _compact_protected(needle) or needle
    fragments = re.sub(r"%[0-9a-f]{2}", " ", value)
    fragments = "".join(
        character if character in compact or _word_char(character) else " "
        for character in fragments
    )
    states = {0}
    for token in fragments.split():
        words = sum(_word_char(char) for char in token)
        next_states = {0}
        for prior in states:
            # Vowel-led two-letter continuations of "An" are ambiguous
            # public words; inserted fragments such as "An Xn" still match.
            if (
                prior
                and compact == "ann"
                and len(compact) - prior == 1
                and words == 2
                and token[0] in "aeiou"
                and token[0] != compact[prior]
            ):
                continue
            cursor = prior
            progressed = False
            matched_words = 0
            for index, character in enumerate(token):
                if character != compact[cursor]:
                    continue
                if (
                    cursor == 0
                    and left_boundary
                    and index
                    and _word_char(token[index - 1])
                    and not (index == 1 and " " in needle)
                ):
                    continue
                if (
                    cursor == len(compact) - 1
                    and right_boundary
                    and index + 1 < len(token)
                    and _word_char(token[index + 1])
                ):
                    continue
                cursor += 1
                progressed = True
                matched_words += _word_char(character)
                if cursor == len(compact):
                    if not prior or words - matched_words <= 1:
                        return True
                    break
            # Keep viable prefix alternatives for inserted fragments such as
            # Sx / SmiX, but not unrelated multi-character words.
            recognizable = words - matched_words <= 1
            if progressed and cursor < len(compact) and (not prior or recognizable):
                next_states.add(cursor)
            if prior and recognizable:
                next_states.add(prior)
        states = next_states
    return False


def _word_char(character: str) -> bool:
    return re.match(r"[^\W_]", character) is not None


def _screen(
    values: list[str],
    screening: ResearchScreeningContext,
    *,
    identifier_values: list[str] | None = None,
    _encoded_depth: int = 0,
    _allow_repair: bool = True,
    _repair_budget: list[int] | None = None,
) -> str | None:
    if _repair_budget is None:
        _repair_budget = [_MAX_ENCODED_REPAIR_WORK]
    reason = _privacy_reason(values, screening, identifier_values)
    if reason is not None:
        return reason
    if _known_encoded_overlap(values, screening):
        return "encoded_leakage"
    try:
        percent_decoded = tuple(
            decoded
            for value in values
            if (decoded := unquote(value, errors="strict")) != value
        )
    except UnicodeDecodeError:
        return "encoded_leakage"
    encoded_candidates = _base64_candidates(values)
    if _encoded_depth >= _MAX_ENCODED_SCREEN_DEPTH:
        if percent_decoded:
            return "encoded_leakage"
        for candidate, opaque_is_leakage in encoded_candidates:
            decoded = _decode_base64(candidate)
            if decoded is None:
                continue
            has_control = any(
                unicodedata.category(character) == "Cc" for character in decoded
            )
            if opaque_is_leakage or not has_control:
                return "encoded_leakage"
        return (
            _repaired_encoded_reason(
                encoded_candidates, screening, _encoded_depth, _repair_budget
            )
            if _allow_repair
            else None
        )
    for decoded in percent_decoded:
        if (
            _screen(
                [decoded],
                screening,
                identifier_values=[decoded],
                _encoded_depth=_encoded_depth + 1,
                _allow_repair=_allow_repair,
                _repair_budget=_repair_budget,
            )
            is not None
        ):
            return "encoded_leakage"
    for candidate, opaque_is_leakage in encoded_candidates:
        decoded = _decode_base64(candidate)
        if decoded is None:
            continue
        if (
            any(unicodedata.category(character) == "Cc" for character in decoded)
            and opaque_is_leakage
        ):
            return "encoded_leakage"
        # Untagged base64 can still contain whitespace/control separators in a
        # protected quotation. Apply the bounded recursive privacy screen too.
        if (
            _screen(
                [decoded],
                screening,
                identifier_values=[decoded],
                _encoded_depth=_encoded_depth + 1,
                _allow_repair=_allow_repair,
                _repair_budget=_repair_budget,
            )
            is not None
        ):
            return "encoded_leakage"
    return (
        _repaired_encoded_reason(
            encoded_candidates, screening, _encoded_depth, _repair_budget
        )
        if _allow_repair
        else None
    )


def _privacy_reason(
    values: list[str],
    screening: ResearchScreeningContext,
    identifier_values: list[str] | None,
) -> str | None:
    if _contains(values, screening.private_canaries):
        return "private_canary"
    structured_identifiers = tuple(
        value
        for value in screening.participant_identifiers
        if not any(character.isspace() for character in _normalized(value))
        and any(not _word_char(character) for character in _normalized(value))
    )
    if _contains_at_boundaries(values, screening.participant_identifiers) or any(
        _recognizable_overlap([value], structured_identifiers) for value in values
    ):
        return "participant_identifier"
    if _contains(values, screening.private_quotations):
        return "private_quotation"
    if _contains(values, screening.sensitive_details):
        return "sensitive_detail"
    combined = "\n".join(values)
    identifier_text = "\n".join(
        values if identifier_values is None else identifier_values
    )
    normalized_identifiers = _normalized(identifier_text)
    email_view = _EMAIL_COMMENT.sub("", normalized_identifiers)
    without_year_ranges = _YEAR_RANGE.sub(" ", normalized_identifiers)
    without_dates = _CALENDAR_DATE.sub(" ", without_year_ranges)
    without_quantities = _PUBLIC_GROUPED_QUANTITY.sub(" ", without_dates)
    if _EMAIL.search(email_view) or _has_phone_number(without_quantities):
        return "direct_identifier"
    if _recognizable_overlap(
        values,
        (
            *screening.private_texts,
            *screening.private_canaries,
            *screening.private_quotations,
            *screening.sensitive_details,
        ),
    ):
        return "recognizable_private_overlap"
    normalized = _normalized(combined)
    if any(_normalized(pattern) in normalized for pattern in _INJECTION_PATTERNS):
        return "prompt_injection"
    return None


def _has_phone_number(value: str) -> bool:
    return any(
        7 <= sum(character.isdigit() for character in match.group(0)) <= 15
        for match in _PHONE.finditer(value)
    )


def _brief_integrity(serialized: str) -> bytes:
    return hmac.digest(_BRIEF_INTEGRITY_KEY, serialized.encode("utf-8"), "sha256")


def _decode_base64(value: str) -> str | None:
    missing_padding = -len(value) % 4
    if missing_padding:
        if "=" in value or missing_padding == 3:
            return None
        value += "=" * missing_padding
    try:
        raw = base64.b64decode(value, altchars=b"-_", validate=True)
        decoded = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return decoded


def _encoded_substrings(candidates):
    for candidate in candidates:
        for start in range(len(candidate)):
            for end in range(start + 3, len(candidate) + 1):
                if start or end != len(candidate):
                    yield candidate[start:end]


def _repaired_encoded_reason(candidates, screening, depth, budget) -> str | None:
    candidates = tuple(dict.fromkeys(value for value, _ in candidates))
    embedded = ResearchScreeningContext(
        private_canaries=screening.private_canaries,
        participant_identifiers=tuple(
            value
            for value in screening.participant_identifiers
            if len(_normalized(value).encode("utf-8")) > 3
        ),
        private_quotations=screening.private_quotations,
        sensitive_details=screening.sensitive_details,
        private_texts=screening.private_texts,
    )
    # Short participant names keep their delimited one-quartet policy. Private
    # comparison texts/canaries still receive embedded reconstruction.
    for context, views in (
        (screening, iter(candidates)),
        (embedded, _encoded_substrings(candidates)),
    ):
        protected = set(
            (
                *context.private_canaries,
                *context.participant_identifiers,
                *context.private_quotations,
                *context.sensitive_details,
                *context.private_texts,
            )
        )
        if not protected:
            continue
        comparison_cost = sum(len(value) for value in protected) * 16
        seen = set()
        for candidate in views:
            for index in range(len(candidate)):
                if budget[0] < len(candidate):
                    return "encoded_leakage"
                budget[0] -= len(candidate)
                decoded = _decode_base64(candidate[:index] + candidate[index + 1 :])
                if decoded is None or decoded in seen:
                    continue
                seen.add(decoded)
                cost = len(decoded) * (1 + len(protected)) + comparison_cost
                if budget[0] < cost:
                    return "encoded_leakage"
                budget[0] -= cost
                # A repaired branch uses the existing depth/privacy checks,
                # without recursively enumerating more character deletions.
                if (
                    depth >= _MAX_ENCODED_SCREEN_DEPTH
                    or _screen(
                        [decoded],
                        context,
                        identifier_values=[decoded],
                        _encoded_depth=depth + 1,
                        _allow_repair=False,
                        _repair_budget=budget,
                    )
                    is not None
                ):
                    return "encoded_leakage"
    return None


def _known_encoded_overlap(
    values: list[str], screening: ResearchScreeningContext
) -> bool:
    public_views = tuple(_normalized_for_encoding(value) for value in values)
    protected = (
        *screening.private_canaries,
        *screening.participant_identifiers,
        *screening.private_quotations,
        *screening.sensitive_details,
        *screening.private_texts,
    )
    for private in dict.fromkeys(protected):
        texts = dict.fromkeys(
            (
                private,
                private.casefold(),
                _normalized(private),
                *(
                    unicodedata.normalize(form, value)
                    for value in (private, private.casefold())
                    for form in ("NFC", "NFD", "NFKC", "NFKD")
                ),
            )
        )
        for text in texts:
            raw = text.encode("utf-8")
            encodings = (
                base64.b64encode(raw).decode("ascii").rstrip("="),
                base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
            )
            for encoded in dict.fromkeys(encodings):
                for public in public_views:
                    if len(encoded) <= 4:
                        if _short_encoded_match(public, encoded):
                            return True
                        continue
                    stream = iter(public)
                    if all(
                        any(character == expected for character in stream)
                        for expected in encoded
                    ):
                        return True
    return False


def _short_encoded_match(public: str, encoded: str) -> bool:
    # One base64 quartet represents at most three protected bytes. Permit one
    # inserted character within a delimited candidate, rather than collecting
    # unrelated initials anywhere in prose. Slash remains a URL-path delimiter.
    patterns = (
        re.escape(encoded[:index]) + r".?" + re.escape(encoded[index:])
        for index in range(len(encoded) + 1)
    )
    return any(
        re.search(rf"(?<![A-Za-z0-9+_-]){pattern}(?![A-Za-z0-9+_-])", public)
        is not None
        for pattern in patterns
    )


def _base64_candidates(values: list[str]) -> tuple[tuple[str, bool], ...]:
    raw = "\n".join(values)
    normalized = "\n".join(_normalized_for_encoding(value) for value in values)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/_-"
    # Original views retain terminal padding. Folded views reconstruct an
    # unpadded candidate, including when invalid padding occurs inside a token.
    separator_folded = tuple(
        "".join(character if character in alphabet else " " for character in view)
        for view in (raw, normalized)
    )
    views = tuple(dict.fromkeys((raw, normalized, *separator_folded)))
    ordinary_contiguous = tuple(
        (match.group(0), match.group(0).endswith("="))
        for view in views
        for match in _BASE64.finditer(view)
    )
    path_contiguous = tuple(
        (match.group(1), match.group(1).endswith("="))
        for view in views
        for match in _PATH_BASE64.finditer(view)
    )
    ordinary_folded = tuple(
        (match.group(0).replace(" ", ""), False)
        for view in views
        for match in _FOLDED_BASE64.finditer(view)
    )
    path_folded = tuple(
        (match.group(1).replace(" ", ""), False)
        for view in views
        for match in _PATH_FOLDED_BASE64.finditer(view)
    )
    return (*ordinary_contiguous, *path_contiguous, *ordinary_folded, *path_folded)


def _normalized_for_encoding(value: str) -> str:
    return "".join(
        " " if character.isspace() else character
        for character in unicodedata.normalize("NFKD", value)
        if unicodedata.category(character) not in {"Cf", "Mn"}
    )


def _recognizable_overlap(values: list[str], private_texts: tuple[str, ...]) -> bool:
    public_normalized = _normalized(" ".join(values))
    public_tokens = _WORDS.findall(public_normalized)
    public_compact = _compact_protected(public_normalized)
    public_punctuation = "".join(
        char for char in public_normalized if unicodedata.category(char).startswith("P")
    )
    public_ngrams = {}
    for private_text in private_texts:
        private_normalized = _normalized(private_text)
        private_compact = _compact_protected(private_normalized)
        if private_compact:
            protected_stream = iter(public_compact)
            if all(
                any(char == expected for char in protected_stream)
                for expected in private_compact
            ):
                return True
        else:
            # Punctuation-only protected content is still data. Its complete
            # punctuation sequence is compared conservatively across separators.
            private_punctuation = "".join(
                char
                for char in private_normalized
                if unicodedata.category(char).startswith("P")
            )
            punctuation_stream = iter(public_punctuation)
            if private_punctuation and all(
                any(char == expected for char in punctuation_stream)
                for expected in private_punctuation
            ):
                return True
        tokens = _WORDS.findall(private_normalized)
        if not tokens:
            continue
        # Longer sources retain the four-token excerpt window.
        size = min(4, len(tokens))
        if size not in public_ngrams:
            public_ngrams[size] = {
                tuple(public_tokens[index : index + size])
                for index in range(len(public_tokens) - size + 1)
            }
        private_ngrams = {
            tuple(tokens[index : index + size])
            for index in range(len(tokens) - size + 1)
        }
        if public_ngrams[size] & private_ngrams:
            return True
    return False


def _compact_protected(normalized: str) -> str:
    # Keep Unicode symbols (including emoji/currency) as meaningful protected
    # content, alongside letters, numbers and retained marks. Formatting,
    # whitespace and punctuation are separators rather than a reason to skip.
    return "".join(
        char for char in normalized if unicodedata.category(char)[0] in "LMNS"
    )
