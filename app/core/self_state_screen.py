"""Versioned heuristic screening for untrusted mutable-state text.

This deterministic H-class control recognizes a bounded set of common unsafe
representations.  Passing it grants no authority, capability, or publication
permission; stored self-state remains untrusted application data.
"""

import base64
import binascii
from dataclasses import dataclass
from enum import StrEnum
from html import unescape as html_unescape
import re
import unicodedata
from urllib.parse import unquote_to_bytes

from .store import MAX_STATE_TEXT_BYTES


SELF_STATE_SCREEN_VERSION = "self-state-screen-v1"

_MAX_TRANSFORM_DEPTH = 3
_MAX_ENCODING_DEPTH = 2
_MAX_REPRESENTATIONS = 24
_PERCENT_ESCAPE = re.compile(r"%[A-Fa-f0-9]{2}")
_BASE64_TEXT = re.compile(r"[A-Za-z0-9+/_-]{8,}={0,2}\Z")
_BASE32_TEXT = re.compile(r"[A-Z2-7]{8,}={0,6}\Z", re.IGNORECASE)
_HEX_TEXT = re.compile(r"(?:[A-Fa-f0-9]{2}){4,}\Z")

# Only characters useful for recognizing common visual substitutions are
# folded. Mixed-script text is not rejected merely for being mixed-script.
_COMMON_CONFUSABLES = str.maketrans(
    {
        "а": "a",
        "е": "e",
        "і": "i",
        "ј": "j",
        "о": "o",
        "р": "p",
        "с": "c",
        "х": "x",
        "у": "y",
        "А": "A",
        "В": "B",
        "Е": "E",
        "К": "K",
        "М": "M",
        "Н": "H",
        "О": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "Х": "X",
    }
)


class SelfStateScreenDecision(StrEnum):
    """Outcome of deterministic heuristic inspection."""

    ALLOW = "allow"
    REJECT = "reject"


class SelfStateScreenReason(StrEnum):
    """Stable, non-sensitive reason codes returned by the screen."""

    ALLOWED = "allowed"
    INVALID_TEXT = "invalid_text"
    INSPECTION_LIMIT = "inspection_limit"
    UNSAFE_UNICODE = "unsafe_unicode"
    INSTRUCTION = "instruction"
    CREDENTIAL = "credential"
    EXTERNAL_ACTION = "external_action"


@dataclass(frozen=True, slots=True)
class SelfStateScreenResult:
    """Typed screening result that never contains the inspected input."""

    version: str
    decision: SelfStateScreenDecision
    reason: SelfStateScreenReason

    @property
    def allowed(self) -> bool:
        return self.decision is SelfStateScreenDecision.ALLOW


_RULES = (
    (
        SelfStateScreenReason.CREDENTIAL,
        re.compile(
            r"\b(?:api[ _-]?key|access[ _-]?token|password|secret)\b\s*"
            r"(?::|=|\b(?:is|was|equals?)\b)\s*\S",
            re.IGNORECASE,
        ),
    ),
    (
        SelfStateScreenReason.CREDENTIAL,
        re.compile(
            r"\bbearer\s+[a-z0-9._~-]{8,}|"
            r"\b(?:sk|pk)-[a-z0-9_-]{16,}\b|"
            r"\bgh[pousr]_[a-z0-9]{20,}\b|"
            r"\bAKIA[A-Z0-9]{16}\b|"
            r"\beyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\b|"
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
            re.IGNORECASE,
        ),
    ),
    (
        SelfStateScreenReason.EXTERNAL_ACTION,
        re.compile(r"\b[a-z][a-z0-9+.-]{1,20}://|\bwww\.", re.IGNORECASE),
    ),
    (
        SelfStateScreenReason.INSTRUCTION,
        re.compile(
            r"(?m)^\s*(?:system|developer|assistant|tool|admin|administrator)\s*:|"
            r"</?tool\b|\b(?:tool|function)_call\b|\[/?inst\]",
            re.IGNORECASE,
        ),
    ),
    (
        SelfStateScreenReason.INSTRUCTION,
        re.compile(
            r"\bignore\s+(?:(?:all\s+)?(?:previous|prior)\s+)?instructions\b|"
            r"\b(?:disregard|discard|forget|override|obey|follow)\b.{0,40}"
            r"\b(?:directive|instruction|message|prompt|rule|policy)\w*\b|"
            r"\b(?:i\s+am|i'm|as)\s+(?:an?\s+|the\s+)?"
            r"(?:admin|administrator|developer|system)\b.{0,50}"
            r"\b(?:obey|follow|ignore|override|instruction|authority)\w*\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        SelfStateScreenReason.INSTRUCTION,
        re.compile(
            r"\b(?:игнорир\w*|забуд\w*|отброс\w*|переопредел\w*|следуй\w*|"
            r"подчин\w*)\b.{0,60}\b(?:инструкц\w*|директив\w*|сообщен\w*|"
            r"правил\w*|политик\w*)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        SelfStateScreenReason.EXTERNAL_ACTION,
        re.compile(
            r"(?m)^\s*(?:(?:(?:goal|objective|instruction|request)\s*:\s*|"
            r"(?:please|kindly)\s+)(?:deploy|publish|post|send|delete|execute|run|"
            r"call|invoke|разверни|опубликуй|отправь|удали|выполни|"
            r"запусти|вызови)|(?:deploy|publish|post|send|delete|execute|run|"
            r"call|invoke|разверни|опубликуй|отправь|удали|выполни|запусти|"
            r"вызови)\s+(?:a|an|the|this|that|these|those|my|our|all|any|on|"
            r"to|from|record|records|file|files|data|message|email|command|shell|"
            r"tool|model|service|site|app)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        SelfStateScreenReason.EXTERNAL_ACTION,
        re.compile(
            r"\b(?:run|execute|call|invoke|access|read)\s+(?:the\s+)?"
            r"(?:shell|terminal|tool|command|filesystem|secret)\b|"
            r"\b(?:send|transmit|upload|publish|post|export|share|reveal|expose)\b"
            r".{0,50}\b(?:private|secret|credential|record|data|prompt)\w*\b|"
            r"\b(?:bypass|without)\s+(?:approval|authorization)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)


def screen_self_state_text(value: object) -> SelfStateScreenResult:
    """Screen one text value using bounded, deterministic transformations."""

    if not isinstance(value, str) or not value:
        return _reject(SelfStateScreenReason.INVALID_TEXT)
    try:
        if len(value.encode("utf-8")) > MAX_STATE_TEXT_BYTES:
            return _reject(SelfStateScreenReason.INSPECTION_LIMIT)
    except UnicodeEncodeError:
        return _reject(SelfStateScreenReason.INVALID_TEXT)

    representations, failure = _text_representations(value)
    if failure is not None:
        return _reject(failure)

    inspected: list[str] = []
    failure = _extend_inspected(representations, inspected)
    if failure is not None:
        return _reject(failure)

    position = 0
    encoding_depth = 0
    while position < len(inspected):
        representation = inspected[position]
        position += 1
        for reason, pattern in _RULES:
            if pattern.search(representation) is not None:
                return _reject(reason)
        decoded = _decoded_texts(representation)
        if decoded:
            if encoding_depth >= _MAX_ENCODING_DEPTH:
                return _reject(SelfStateScreenReason.INSPECTION_LIMIT)
            encoding_depth += 1
            for decoded_text in decoded:
                decoded_representations, failure = _text_representations(decoded_text)
                if failure is not None:
                    return _reject(failure)
                failure = _extend_inspected(decoded_representations, inspected)
                if failure is not None:
                    return _reject(failure)

    return SelfStateScreenResult(
        SELF_STATE_SCREEN_VERSION,
        SelfStateScreenDecision.ALLOW,
        SelfStateScreenReason.ALLOWED,
    )


def _reject(reason: SelfStateScreenReason) -> SelfStateScreenResult:
    return SelfStateScreenResult(
        SELF_STATE_SCREEN_VERSION, SelfStateScreenDecision.REJECT, reason
    )


def _text_representations(
    value: str,
) -> tuple[tuple[str, ...], SelfStateScreenReason | None]:
    current = unicodedata.normalize("NFKC", value)
    representations = [current]
    for _ in range(_MAX_TRANSFORM_DEPTH):
        transformed = current
        if _PERCENT_ESCAPE.search(transformed) is not None:
            try:
                transformed = unquote_to_bytes(transformed).decode("utf-8")
            except UnicodeDecodeError:
                return (), SelfStateScreenReason.INVALID_TEXT
        transformed = unicodedata.normalize("NFKC", html_unescape(transformed))
        if transformed == current:
            break
        current = transformed
        representations.append(current)
    if _PERCENT_ESCAPE.search(current) is not None or html_unescape(current) != current:
        return (), SelfStateScreenReason.INSPECTION_LIMIT
    return tuple(representations), None


def _extend_inspected(
    representations: tuple[str, ...], inspected: list[str]
) -> SelfStateScreenReason | None:
    for representation in representations:
        if _has_unsafe_unicode(representation):
            return SelfStateScreenReason.UNSAFE_UNICODE
        variants = (
            representation,
            _strip_combining_marks(representation),
            representation.translate(_COMMON_CONFUSABLES),
        )
        for variant in variants:
            collapsed = " ".join(variant.split())
            for candidate in (variant, collapsed):
                if candidate not in inspected:
                    inspected.append(candidate)
                if len(inspected) > _MAX_REPRESENTATIONS:
                    return SelfStateScreenReason.INSPECTION_LIMIT
    return None


def _strip_combining_marks(value: str) -> str:
    return unicodedata.normalize(
        "NFKC",
        "".join(
            character
            for character in unicodedata.normalize("NFKD", value)
            if not unicodedata.category(character).startswith("M")
        ),
    )


def _has_unsafe_unicode(value: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        and character not in "\n\t"
        for character in value
    )


def _decoded_texts(value: str) -> tuple[str, ...]:
    candidate = value.translate(str.maketrans("", "", " \n\t"))
    decoded_values: list[bytes] = []
    if _HEX_TEXT.fullmatch(candidate) is not None:
        decoded_values.append(bytes.fromhex(candidate))
    if _BASE64_TEXT.fullmatch(candidate) is not None:
        unpadded = candidate.rstrip("=")
        if len(unpadded) % 4 != 1:
            padded = unpadded + "=" * (-len(unpadded) % 4)
            try:
                decoded_values.append(
                    base64.b64decode(padded, altchars=b"-_", validate=True)
                )
            except binascii.Error:
                pass
    if _BASE32_TEXT.fullmatch(candidate) is not None:
        unpadded = candidate.rstrip("=")
        padded = unpadded + "=" * (-len(unpadded) % 8)
        try:
            decoded_values.append(base64.b32decode(padded, casefold=True))
        except binascii.Error:
            pass

    decoded_texts: list[str] = []
    for decoded in decoded_values:
        try:
            text = decoded.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text not in decoded_texts:
            decoded_texts.append(text)
    return tuple(decoded_texts)
