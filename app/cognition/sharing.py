"""Disabled-by-design private-to-shared abstraction validation boundary."""

from collections.abc import Callable
from datetime import datetime
import re
import unicodedata

from app.core.events import (
    PrivateConversationEvent,
    TrustedPrincipalContext,
    VerificationStatus,
)
from app.core.store import EventStore, MAX_STATE_TEXT_BYTES

from .contracts import (
    AbstractAuditRecord,
    AbstractCandidate,
    AbstractDecisionStatus,
    AbstractReason,
    AbstractToSharedProposal,
    AbstractValidationResult,
    CognitionCapabilities,
    MemoryKind,
    PrivacyScreenDecision,
    PrivacyScreenResult,
)
from .read_store import retrieve_exact_private_event


PRIVACY_SCREEN_VERSION = "abstract-privacy-screen-v1"
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_IDENTIFIER = re.compile(
    r"(?:(?<![\w@])[\w.!#$%&'*+/=?^`{|}~-]+@"
    r"[\w-]+(?:\.[\w-]+)+(?![\w@])|"
    r"(?<!\w)@\w{2,32}\b|"
    r"\b(?:https?://|www\.)\S+|"
    r"\b(?:\+?\d[\d ()-]{7,}\d)\b)",
    re.IGNORECASE,
)
_QUOTATION = re.compile(r"[\"'“”‘’«»]")
_WORD = re.compile(r"\w+", re.UNICODE)
_IDNA_DOT_TRANSLATION = str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": "."})
_SHAREABLE_KINDS = {
    MemoryKind.DURABLE_INTEREST,
    MemoryKind.BELIEF_OR_HYPOTHESIS,
    MemoryKind.GOAL_OR_INTENTION,
    MemoryKind.UNRESOLVED_QUESTION,
}
_MAX_PROTECTED_SOURCES = 50
_DEFAULT_IGNORABLE_CODEPOINTS = frozenset(
    codepoint
    for start, end in (
        (0x00AD, 0x00AD),
        (0x034F, 0x034F),
        (0x061C, 0x061C),
        (0x115F, 0x1160),
        (0x17B4, 0x17B5),
        (0x180B, 0x180F),
        (0x200B, 0x200F),
        (0x202A, 0x202E),
        (0x2060, 0x206F),
        (0x3164, 0x3164),
        (0xFE00, 0xFE0F),
        (0xFEFF, 0xFEFF),
        (0xFFA0, 0xFFA0),
        (0x1BCA0, 0x1BCA3),
        (0x1D173, 0x1D17A),
        (0xE0000, 0xE0FFF),
    )
    for codepoint in range(start, end + 1)
)


def _normalize_privacy_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).translate(_IDNA_DOT_TRANSLATION)
    return "".join(
        " " if character.isspace() else character
        for character in normalized
        if character.isspace()
        or (
            unicodedata.category(character) not in {"Cc", "Cf"}
            and ord(character) not in _DEFAULT_IGNORABLE_CODEPOINTS
        )
    )


def _canonical_identifier_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", _normalize_privacy_text(value))
    return "".join(
        character
        for character in decomposed
        if unicodedata.category(character) not in {"Mn", "Mc", "Me"}
    )


def screen_abstract_candidate(
    candidate: AbstractCandidate,
    sources: tuple[PrivateConversationEvent, ...],
) -> PrivacyScreenResult:
    """Conservative H screen for obvious identity, quotation, and copy leakage."""

    normalized_candidate = _normalize_privacy_text(candidate.value)
    identifier_candidate = _canonical_identifier_text(candidate.value)
    if _IDENTIFIER.search(identifier_candidate) is not None:
        return PrivacyScreenResult(
            PRIVACY_SCREEN_VERSION,
            PrivacyScreenDecision.REJECT,
            AbstractReason.IDENTIFIER.value,
        )
    if _QUOTATION.search(normalized_candidate) is not None:
        return PrivacyScreenResult(
            PRIVACY_SCREEN_VERSION,
            PrivacyScreenDecision.REJECT,
            AbstractReason.QUOTATION.value,
        )
    candidate_words = _normalized_words(normalized_candidate)
    for source in sources:
        source_words = _normalized_words(source.content)
        common_words = set(candidate_words) & set(source_words)
        if any(len(word) >= 16 for word in common_words):
            return PrivacyScreenResult(
                PRIVACY_SCREEN_VERSION,
                PrivacyScreenDecision.REJECT,
                AbstractReason.EXCESSIVE_SOURCE_OVERLAP.value,
            )
        overlap = len(common_words)
        denominator = max(1, min(len(set(candidate_words)), len(set(source_words))))
        if overlap / denominator > 0.6:
            return PrivacyScreenResult(
                PRIVACY_SCREEN_VERSION,
                PrivacyScreenDecision.REJECT,
                AbstractReason.EXCESSIVE_SOURCE_OVERLAP.value,
            )
    return PrivacyScreenResult(
        PRIVACY_SCREEN_VERSION,
        PrivacyScreenDecision.ALLOW,
        "allowed",
    )


def _normalized_words(value: str) -> tuple[str, ...]:
    normalized = _canonical_identifier_text(value).casefold()
    return tuple(_WORD.findall(normalized))


def _contains_protected_marker(value: str, marker: str) -> bool:
    normalized_value = _canonical_identifier_text(value).casefold()
    normalized_marker = _canonical_identifier_text(marker).casefold()
    pattern = re.compile(rf"(?<!\w){re.escape(normalized_marker)}(?!\w)")
    return pattern.search(normalized_value) is not None


class AbstractToSharedValidator:
    """Screens abstractions but deliberately exposes no shared mutation path."""

    def __init__(
        self,
        event_store: EventStore,
        *,
        capabilities: CognitionCapabilities = CognitionCapabilities(),
        privacy_screen: Callable[
            [AbstractCandidate, tuple[PrivateConversationEvent, ...]],
            PrivacyScreenResult,
        ] = screen_abstract_candidate,
    ):
        self._events = event_store
        self._capabilities = capabilities
        self._privacy_screen = privacy_screen

    def validate(
        self,
        context: TrustedPrincipalContext,
        proposal: object,
    ) -> AbstractValidationResult:
        if not self._capabilities.shared_abstraction_screening:
            return self._result(
                AbstractDecisionStatus.REJECTED,
                AbstractReason.CAPABILITY_DISABLED,
            )
        if (
            type(proposal) is not AbstractToSharedProposal
            or type(proposal.candidate) is not AbstractCandidate
        ):
            return self._result(
                AbstractDecisionStatus.REJECTED,
                AbstractReason.INVALID_PROPOSAL,
            )
        candidate = proposal.candidate
        if (
            not isinstance(candidate.memory_kind, MemoryKind)
            or candidate.memory_kind not in _SHAREABLE_KINDS
        ):
            return self._result(
                AbstractDecisionStatus.REJECTED, AbstractReason.INVALID_KIND
            )
        if candidate.verification is not VerificationStatus.UNVERIFIED:
            return self._result(
                AbstractDecisionStatus.REJECTED,
                AbstractReason.VERIFICATION_MISMATCH,
            )
        if (
            type(proposal.operation_id) is not str
            or _TOKEN.fullmatch(proposal.operation_id) is None
            or not self._valid_time(proposal.proposed_at)
            or not self._valid_value(candidate.value)
            or type(candidate.confidence) is not float
            or not 0.0 <= candidate.confidence <= 1.0
        ):
            return self._result(
                AbstractDecisionStatus.REJECTED, AbstractReason.INVALID_VALUE
            )
        protected_ids = proposal.protected_source_event_ids
        if (
            type(protected_ids) is not tuple
            or not protected_ids
            or len(protected_ids) > _MAX_PROTECTED_SOURCES
        ):
            return self._result(
                AbstractDecisionStatus.REJECTED,
                AbstractReason.MISSING_PROVENANCE,
            )
        if any(
            type(item) is not str or _TOKEN.fullmatch(item) is None
            for item in protected_ids
        ) or len(set(protected_ids)) != len(protected_ids):
            return self._result(
                AbstractDecisionStatus.REJECTED,
                AbstractReason.INVALID_PROVENANCE,
            )
        resolved_sources = tuple(
            retrieve_exact_private_event(self._events, context, item)
            for item in protected_ids
        )
        if any(item is None for item in resolved_sources):
            return self._result(
                AbstractDecisionStatus.REJECTED,
                AbstractReason.INVALID_PROVENANCE,
            )
        sources = tuple(item for item in resolved_sources if item is not None)
        audit = AbstractAuditRecord(
            proposal.operation_id,
            protected_ids,
            1,
            None,
            None,
        )
        protected_markers = (
            context.principal_id.value,
            *protected_ids,
        )
        if any(
            _contains_protected_marker(candidate.value, marker)
            for marker in protected_markers
        ):
            return self._result(
                AbstractDecisionStatus.REJECTED,
                AbstractReason.PROTECTED_METADATA,
                audit=audit,
            )
        try:
            screen = self._privacy_screen(candidate, sources)
        except Exception:
            return self._result(
                AbstractDecisionStatus.REJECTED,
                AbstractReason.PRIVACY_REJECTED,
                audit=audit,
            )
        if (
            type(screen) is not PrivacyScreenResult
            or not isinstance(screen.decision, PrivacyScreenDecision)
            or type(screen.version) is not str
            or _TOKEN.fullmatch(screen.version) is None
            or not self._valid_value(screen.reason)
        ):
            return self._result(
                AbstractDecisionStatus.REJECTED,
                AbstractReason.PRIVACY_REJECTED,
                audit=audit,
            )
        audit = AbstractAuditRecord(
            proposal.operation_id,
            protected_ids,
            1,
            screen.version,
            screen.reason,
        )
        if screen.decision is PrivacyScreenDecision.REJECT:
            reason = self._screen_reason(screen.reason)
            return self._result(AbstractDecisionStatus.REJECTED, reason, audit=audit)
        if screen.decision is PrivacyScreenDecision.UNCERTAIN:
            return self._result(
                AbstractDecisionStatus.QUARANTINED,
                AbstractReason.PRIVACY_UNCERTAIN,
                candidate=candidate,
                audit=audit,
            )
        return self._result(
            AbstractDecisionStatus.QUARANTINED,
            AbstractReason.SHARED_COMMIT_DISABLED,
            candidate=candidate,
            audit=audit,
        )

    @staticmethod
    def _screen_reason(reason: str) -> AbstractReason:
        mapped = {
            AbstractReason.IDENTIFIER.value: AbstractReason.IDENTIFIER,
            AbstractReason.QUOTATION.value: AbstractReason.QUOTATION,
            AbstractReason.EXCESSIVE_SOURCE_OVERLAP.value: AbstractReason.EXCESSIVE_SOURCE_OVERLAP,
        }
        return mapped.get(reason, AbstractReason.PRIVACY_REJECTED)

    @staticmethod
    def _valid_value(value: object) -> bool:
        if type(value) is not str or not value.strip():
            return False
        try:
            return len(value.encode("utf-8")) <= MAX_STATE_TEXT_BYTES
        except UnicodeEncodeError:
            return False

    @staticmethod
    def _valid_time(value: object) -> bool:
        if not isinstance(value, datetime):
            return False
        try:
            if value.tzinfo is None:
                return False
            return value.utcoffset() is not None
        except Exception:
            return False

    @staticmethod
    def _result(
        status: AbstractDecisionStatus,
        reason: AbstractReason,
        *,
        candidate: AbstractCandidate | None = None,
        audit: AbstractAuditRecord | None = None,
    ) -> AbstractValidationResult:
        return AbstractValidationResult(
            status=status,
            reason=reason,
            quarantined_candidate=candidate,
            audit=audit,
        )
