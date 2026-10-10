"""Isolated durable two-phase state machine for major mission revision."""

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
import json
from pathlib import Path
import re
from typing import Protocol
import unicodedata


MISSION_SCHEMA_VERSION = "mission-store-v1"
MAX_MISSION_TEXT_UTF8_BYTES = 4096
MAX_MISSION_COLLECTION_ITEMS = 32
MAX_MISSION_COLLECTION_UTF8_BYTES = 16 * 1024
MAX_MISSION_IDENTIFIER_COLLECTION_UTF8_BYTES = 2 * 1024
MAX_MISSION_RECORD_TEXT_UTF8_BYTES = 32 * 1024
MAX_MISSION_HISTORY_RECORDS = 256
_FORBIDDEN_MISSION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:https?://|ftp://|www\.|\b(?:[a-z0-9-]+\.)+[a-z]{2,63}\b)",
        r"\bignore\s+(?:all\s+)?(?:the\s+)?previous\b",
        r"\b(?:system\s+prompt|instructions?|credential|password|secret|"
        r"api\s*key|private\s*key|budget|permissions?|security\s+settings?|"
        r"deployment|emergency\s+stop|shell|docker)\b",
        r"\b(?:call|invoke|execute|run|use)\s+(?:the\s+)?"
        r"(?:tool|shell|command)\b",
        r"игнорир\w*.{0,30}предыдущ\w*",
        r"(?:системн\w*\s+промпт\w*|инструкци\w*|уч[её]тн\w*\s+данн\w*|"
        r"парол\w*|секрет\w*|ключ(?:и|а|ей|ом|у)?|бюджет\w*|разрешени\w*|"
        r"прав\w*\s+доступ\w*|настройк\w*\s+безопасност\w*|"
        r"аварийн\w*\s+останов\w*|разв[её]ртыван\w*|депло\w*|шелл\w*|"
        r"докер\w*)",
        r"(?:выполни|запусти|вызови|используй|управляй|раскрой|измени)"
        r".{0,40}(?:инструмент\w*|команд\w*|настройк\w*|ключ\w*|доступ\w*)",
    )
)
_MISSION_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class MissionStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ConfirmationOutcome(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class MissionPurpose(StrEnum):
    GENERAL_INQUIRY = "general_inquiry"
    RESEARCH = "research"
    DIALOGUE = "dialogue"
    REFLECTION = "reflection"
    DISCONTINUE_OPTIONAL_CHANNEL = "discontinue_optional_channel"


_MISSION_PURPOSE_TEXT = {
    MissionPurpose.GENERAL_INQUIRY: (
        "Исследовать проверяемые идеи и поддерживать содержательный диалог"
    ),
    MissionPurpose.RESEARCH: (
        "Исследовать проверяемые идеи без обязательной публикации"
    ),
    MissionPurpose.DIALOGUE: "Вести содержательный диалог без обязательной публикации",
    MissionPurpose.REFLECTION: "Осмыслять и пересматривать долгосрочное направление",
    MissionPurpose.DISCONTINUE_OPTIONAL_CHANNEL: (
        "Прекратить необязательную публикацию в Threads"
    ),
}


class EvidenceProvenance(StrEnum):
    PARTICIPANT = "participant"
    PUBLIC_SOURCE = "public_source"
    AGENT_OBSERVATION = "agent_observation"
    VERIFIED_SOURCE = "verified_source"


@dataclass(frozen=True, slots=True)
class MissionEvidenceSource:
    evidence_id: str
    source_id: str
    provenance: EvidenceProvenance


@dataclass(frozen=True, slots=True)
class CapabilityPolicyInvariants:
    core_self_version: str
    operational_policy_version: str
    credentials_version: str
    provider_budget_version: str
    deployment_stage: str
    tool_permissions_version: str
    emergency_stop_available: bool

    def to_dict(self) -> dict[str, str | bool]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Mission:
    version: str
    text: str
    activated_at: datetime
    parent_version: str | None
    purpose: MissionPurpose = MissionPurpose.GENERAL_INQUIRY


@dataclass(frozen=True, slots=True)
class MissionProposal:
    proposal_id: str
    proposed_mission: str
    current_mission_version: str
    initiating_event_id: str
    supporting_reasons: tuple[str, ...]
    counterevidence: tuple[str, ...]
    version_identity: str
    decided_at: datetime
    correlation_id: str
    model_identifier: str
    prompt_version: str
    invariants: CapabilityPolicyInvariants
    purpose: MissionPurpose
    initiating_source_id: str


@dataclass(frozen=True, slots=True)
class MissionConfirmation:
    proposal_id: str
    event_id: str
    correlation_id: str
    decided_at: datetime
    outcome: ConfirmationOutcome
    reasons: tuple[str, ...]
    counterevidence: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    invariants: CapabilityPolicyInvariants
    evidence_sources: tuple[MissionEvidenceSource, ...]


@dataclass(frozen=True, slots=True)
class MissionRecord:
    proposal: MissionProposal
    status: MissionStatus
    confirmation: MissionConfirmation | None


@dataclass(frozen=True, slots=True)
class MissionDocument:
    current: Mission
    proposals: tuple[MissionRecord, ...]
    invariants: CapabilityPolicyInvariants | None


class MissionRepository(Protocol):
    def load(self) -> MissionDocument: ...

    def save(self, document: MissionDocument) -> None: ...


class EvidenceAuthority(Protocol):
    """Application-owned verifier for protected source/provenance records."""

    def __call__(
        self,
        initiating_source_id: str,
        evidence_sources: tuple[MissionEvidenceSource, ...],
    ) -> bool: ...


class JsonMissionRepository:
    """Versioned atomic JSON repository, isolated from the shared EventStore."""

    def __init__(self, path: Path, initial_mission: Mission):
        self._path = Path(path)
        _validate_stored_mission(initial_mission)
        if initial_mission.parent_version is not None:
            raise ValueError("initial mission must be a trusted root")
        self._initial_mission = initial_mission
        if not self._path.exists():
            self.save(MissionDocument(initial_mission, (), None))

    def load(self) -> MissionDocument:
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != MISSION_SCHEMA_VERSION:
            raise ValueError("unsupported mission repository schema")
        return _document_from_dict(raw, self._initial_mission)

    def save(self, document: MissionDocument) -> None:
        payload = _document_to_dict(document)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            encoding="utf-8",
        )
        temporary.replace(self._path)


class MissionStateMachine:
    def __init__(
        self, repository: MissionRepository, evidence_authority: EvidenceAuthority
    ):
        self._repository = repository
        self._evidence_authority = evidence_authority

    def current_mission(self) -> Mission:
        return self._repository.load().current

    def propose(self, proposal: MissionProposal) -> MissionRecord:
        document = self._repository.load()
        _validate_proposal(proposal)
        if proposal.current_mission_version != document.current.version:
            raise ValueError("proposal does not reference the current mission")
        if proposal.decided_at < document.current.activated_at:
            raise ValueError("proposal predates the current mission")
        if proposal.version_identity == document.current.version or any(
            item.proposal.version_identity == proposal.version_identity
            for item in document.proposals
        ):
            raise ValueError("mission version identity already exists")
        if any(
            item.proposal.proposal_id == proposal.proposal_id
            for item in document.proposals
        ):
            raise ValueError("proposal already exists")
        if len(document.proposals) >= MAX_MISSION_HISTORY_RECORDS:
            raise ValueError("mission history reached its record limit")
        if any(
            item.status is MissionStatus.PENDING
            and item.proposal.invariants != proposal.invariants
            for item in document.proposals
        ):
            raise ValueError("capability-policy invariants changed")
        record = MissionRecord(proposal, MissionStatus.PENDING, None)
        self._repository.save(
            MissionDocument(
                document.current,
                (*document.proposals, record),
                proposal.invariants,
            )
        )
        return record

    def confirm(self, confirmation: MissionConfirmation) -> MissionRecord:
        if not isinstance(confirmation, MissionConfirmation):
            raise TypeError("confirmation must be a MissionConfirmation")
        document = self._repository.load()
        matches = [
            (index, item)
            for index, item in enumerate(document.proposals)
            if item.proposal.proposal_id == confirmation.proposal_id
        ]
        if not matches:
            raise ValueError("unknown proposal")
        index, existing = matches[0]
        if existing.status is not MissionStatus.PENDING:
            raise ValueError("proposal is already resolved")
        if existing.proposal.current_mission_version != document.current.version:
            raise ValueError("proposal parent is no longer the current mission")
        _validate_confirmation(existing.proposal, confirmation)
        if (
            self._evidence_authority(
                existing.proposal.initiating_source_id,
                confirmation.evidence_sources,
            )
            is not True
        ):
            raise ValueError("confirmation evidence is not independently authorized")
        status = MissionStatus(confirmation.outcome.value)
        resolved = MissionRecord(existing.proposal, status, confirmation)
        proposals = list(document.proposals)
        proposals[index] = resolved
        current = document.current
        if status is MissionStatus.CONFIRMED:
            proposals = [
                MissionRecord(item.proposal, MissionStatus.SUPERSEDED, None)
                if sibling_index != index
                and item.status is MissionStatus.PENDING
                and item.proposal.current_mission_version
                == existing.proposal.current_mission_version
                else item
                for sibling_index, item in enumerate(proposals)
            ]
            current = Mission(
                existing.proposal.version_identity,
                existing.proposal.proposed_mission,
                confirmation.decided_at,
                existing.proposal.current_mission_version,
                existing.proposal.purpose,
            )
        self._repository.save(
            MissionDocument(current, tuple(proposals), document.invariants)
        )
        return resolved


def _validate_proposal(proposal: MissionProposal) -> None:
    if not isinstance(proposal, MissionProposal):
        raise TypeError("proposal must be a MissionProposal")
    for name in (
        "proposal_id",
        "current_mission_version",
        "initiating_event_id",
        "version_identity",
        "correlation_id",
        "model_identifier",
        "prompt_version",
        "initiating_source_id",
    ):
        if not _valid_identifier(getattr(proposal, name)):
            raise ValueError(f"{name} is invalid")
    if not _valid_text(proposal.proposed_mission):
        raise ValueError("proposed_mission is invalid")
    mission_fields = (
        proposal.proposed_mission,
        *proposal.supporting_reasons,
        *proposal.counterevidence,
    )
    if any(_contains_forbidden_mission_content(value) for value in mission_fields):
        raise ValueError(
            "closed mission text rejects instructions, links, and policy changes"
        )
    if not isinstance(proposal.purpose, MissionPurpose) or not _mission_matches_purpose(
        proposal.proposed_mission, proposal.purpose
    ):
        raise ValueError("proposed_mission does not match its allowed purpose")
    _validate_text_tuple(proposal.supporting_reasons, "supporting_reasons")
    _validate_text_tuple(proposal.counterevidence, "counterevidence")
    _validate_aggregate_text(
        (
            proposal.proposed_mission,
            *proposal.supporting_reasons,
            *proposal.counterevidence,
        )
    )
    _validate_time(proposal.decided_at)
    _validate_invariants(proposal.invariants)


def _validate_confirmation(
    proposal: MissionProposal,
    confirmation: MissionConfirmation,
) -> None:
    if confirmation.proposal_id != proposal.proposal_id:
        raise ValueError("confirmation does not match its proposal")
    if confirmation.event_id == proposal.initiating_event_id:
        raise ValueError("confirmation requires an independent event")
    if confirmation.correlation_id == proposal.correlation_id:
        raise ValueError("confirmation requires an independent correlation")
    _validate_time(confirmation.decided_at)
    if confirmation.decided_at <= proposal.decided_at:
        raise ValueError("confirmation must be later than the proposal")
    if not isinstance(confirmation.outcome, ConfirmationOutcome):
        raise ValueError("confirmation outcome is invalid")
    for name in ("proposal_id", "event_id", "correlation_id"):
        if not _valid_identifier(getattr(confirmation, name)):
            raise ValueError(f"{name} is invalid")
    _validate_text_tuple(confirmation.reasons, "reasons")
    _validate_text_tuple(confirmation.counterevidence, "counterevidence")
    if any(
        _contains_forbidden_mission_content(value)
        for value in (*confirmation.reasons, *confirmation.counterevidence)
    ):
        raise ValueError(
            "closed confirmation text rejects instructions, links, and policy changes"
        )
    _validate_identifier_tuple(confirmation.evidence_ids, "evidence_ids")
    _validate_evidence_sources(confirmation.evidence_sources)
    if tuple(source.evidence_id for source in confirmation.evidence_sources) != (
        confirmation.evidence_ids
    ):
        raise ValueError("each evidence ID requires matching source provenance")
    _validate_aggregate_text(
        (
            *confirmation.reasons,
            *confirmation.counterevidence,
            *confirmation.evidence_ids,
        )
    )
    if proposal.initiating_event_id in confirmation.evidence_ids or not any(
        source.source_id != proposal.initiating_source_id
        for source in confirmation.evidence_sources
    ):
        raise ValueError("confirmation requires independent evidence")
    _validate_invariants(confirmation.invariants)
    if confirmation.invariants != proposal.invariants:
        raise ValueError("capability-policy invariants changed")


def _validate_invariants(value: CapabilityPolicyInvariants) -> None:
    if not isinstance(value, CapabilityPolicyInvariants):
        raise ValueError("capability-policy invariants are invalid")
    fields = (
        value.core_self_version,
        value.operational_policy_version,
        value.credentials_version,
        value.provider_budget_version,
        value.deployment_stage,
        value.tool_permissions_version,
    )
    if (
        any(not _valid_text(item) for item in fields)
        or type(value.emergency_stop_available) is not bool
    ):
        raise ValueError("capability-policy invariants are invalid")


def _validate_text_tuple(value: tuple[str, ...], name: str) -> None:
    if (
        type(value) is not tuple
        or not value
        or len(value) > MAX_MISSION_COLLECTION_ITEMS
        or any(not _valid_text(item) for item in value)
    ):
        raise ValueError(f"{name} must contain bounded text")
    if (
        sum(len(item.encode("utf-8")) for item in value)
        > MAX_MISSION_COLLECTION_UTF8_BYTES
    ):
        raise ValueError(f"{name} exceeds its aggregate byte bound")


def _validate_aggregate_text(values: tuple[str, ...]) -> None:
    if (
        sum(len(value.encode("utf-8")) for value in values)
        > MAX_MISSION_RECORD_TEXT_UTF8_BYTES
    ):
        raise ValueError("mission record exceeds its aggregate byte bound")


def _validate_identifier_tuple(value: tuple[str, ...], name: str) -> None:
    if (
        type(value) is not tuple
        or not value
        or len(value) > MAX_MISSION_COLLECTION_ITEMS
        or any(not _valid_identifier(item) for item in value)
    ):
        raise ValueError(f"{name} must contain bounded identifiers")
    if (
        sum(len(item.encode("utf-8")) for item in value)
        > MAX_MISSION_IDENTIFIER_COLLECTION_UTF8_BYTES
    ):
        raise ValueError(f"{name} exceeds its aggregate byte bound")


def _validate_evidence_sources(value: tuple[MissionEvidenceSource, ...]) -> None:
    if (
        type(value) is not tuple
        or not value
        or len(value) > MAX_MISSION_COLLECTION_ITEMS
        or any(
            not isinstance(source, MissionEvidenceSource)
            or not _valid_identifier(source.evidence_id)
            or not _valid_identifier(source.source_id)
            or not isinstance(source.provenance, EvidenceProvenance)
            for source in value
        )
    ):
        raise ValueError("evidence_sources must contain bounded typed provenance")


def _validate_time(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("decision timestamp must be timezone-aware")


def _valid_text(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_MISSION_TEXT_UTF8_BYTES
    except UnicodeEncodeError:
        return False


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and _MISSION_IDENTIFIER.fullmatch(value) is not None


def _contains_forbidden_mission_content(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    if any(
        character not in "\n\r\t" and unicodedata.category(character) in ("Cc", "Cf")
        for character in normalized
    ):
        return True
    return any(pattern.search(normalized) for pattern in _FORBIDDEN_MISSION_PATTERNS)


def _mission_matches_purpose(value: str, purpose: MissionPurpose) -> bool:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    expected = unicodedata.normalize("NFKC", _MISSION_PURPOSE_TEXT[purpose]).casefold()
    return normalized == expected


def _document_to_dict(document: MissionDocument) -> dict[str, object]:
    if len(document.proposals) > MAX_MISSION_HISTORY_RECORDS:
        raise ValueError("mission history exceeds its record limit")
    return {
        "schema_version": MISSION_SCHEMA_VERSION,
        "current": _mission_to_dict(document.current),
        "invariants": (
            document.invariants.to_dict() if document.invariants is not None else None
        ),
        "proposals": [_record_to_dict(record) for record in document.proposals],
    }


def _mission_to_dict(mission: Mission) -> dict[str, object]:
    return {
        "version": mission.version,
        "text": mission.text,
        "activated_at": mission.activated_at.isoformat(),
        "parent_version": mission.parent_version,
        "purpose": mission.purpose.value,
    }


def _proposal_to_dict(proposal: MissionProposal) -> dict[str, object]:
    result = asdict(proposal)
    result["decided_at"] = proposal.decided_at.isoformat()
    result["invariants"] = proposal.invariants.to_dict()
    return result


def _confirmation_to_dict(confirmation: MissionConfirmation) -> dict[str, object]:
    result = asdict(confirmation)
    result["decided_at"] = confirmation.decided_at.isoformat()
    result["outcome"] = confirmation.outcome.value
    result["invariants"] = confirmation.invariants.to_dict()
    return result


def _record_to_dict(record: MissionRecord) -> dict[str, object]:
    return {
        "proposal": _proposal_to_dict(record.proposal),
        "status": record.status.value,
        "confirmation": (
            _confirmation_to_dict(record.confirmation)
            if record.confirmation is not None
            else None
        ),
    }


def _document_from_dict(
    raw: dict[str, object], trusted_root: Mission
) -> MissionDocument:
    current_raw = raw["current"]
    if not isinstance(current_raw, dict):
        raise ValueError("mission repository current record is invalid")
    current = Mission(
        current_raw["version"],
        current_raw["text"],
        datetime.fromisoformat(current_raw["activated_at"]),
        current_raw["parent_version"],
        MissionPurpose(current_raw["purpose"]),
    )
    _validate_stored_mission(current)
    invariants_raw = raw["invariants"]
    stored_invariants = (
        CapabilityPolicyInvariants(**invariants_raw)
        if isinstance(invariants_raw, dict)
        else None
    )
    proposals_raw = raw["proposals"]
    if not isinstance(proposals_raw, list):
        raise ValueError("mission repository proposals are invalid")
    if len(proposals_raw) > MAX_MISSION_HISTORY_RECORDS:
        raise ValueError("mission history exceeds its record limit")
    records: list[MissionRecord] = []
    for item in proposals_raw:
        proposal_raw = item["proposal"]
        proposal_values = dict(proposal_raw)
        proposal_values["decided_at"] = datetime.fromisoformat(
            proposal_values["decided_at"]
        )
        proposal_values["supporting_reasons"] = tuple(
            proposal_values["supporting_reasons"]
        )
        proposal_values["counterevidence"] = tuple(proposal_values["counterevidence"])
        proposal_values["purpose"] = MissionPurpose(proposal_values["purpose"])
        proposal_values["invariants"] = CapabilityPolicyInvariants(
            **proposal_values["invariants"]
        )
        parsed_proposal = MissionProposal(**proposal_values)
        _validate_proposal(parsed_proposal)
        confirmation_raw = item["confirmation"]
        parsed_confirmation = None
        if confirmation_raw is not None:
            confirmation_values = dict(confirmation_raw)
            confirmation_values["decided_at"] = datetime.fromisoformat(
                confirmation_values["decided_at"]
            )
            confirmation_values["outcome"] = ConfirmationOutcome(
                confirmation_values["outcome"]
            )
            for key in ("reasons", "counterevidence", "evidence_ids"):
                confirmation_values[key] = tuple(confirmation_values[key])
            confirmation_values["evidence_sources"] = tuple(
                MissionEvidenceSource(
                    source["evidence_id"],
                    source["source_id"],
                    EvidenceProvenance(source["provenance"]),
                )
                for source in confirmation_values["evidence_sources"]
            )
            confirmation_values["invariants"] = CapabilityPolicyInvariants(
                **confirmation_values["invariants"]
            )
            parsed_confirmation = MissionConfirmation(**confirmation_values)
            _validate_confirmation(parsed_proposal, parsed_confirmation)
        status = MissionStatus(item["status"])
        resolved = status in (MissionStatus.CONFIRMED, MissionStatus.REJECTED)
        if resolved != (parsed_confirmation is not None) or (
            parsed_confirmation is not None
            and status.value != parsed_confirmation.outcome.value
        ):
            raise ValueError("mission record status is inconsistent")
        records.append(
            MissionRecord(
                parsed_proposal,
                status,
                parsed_confirmation,
            )
        )
    _validate_active_mission(current, tuple(records), trusted_root)
    return MissionDocument(current, tuple(records), stored_invariants)


def _validate_stored_mission(mission: Mission) -> None:
    if (
        not _valid_identifier(mission.version)
        or not _valid_text(mission.text)
        or mission.parent_version is not None
        and not _valid_identifier(mission.parent_version)
        or not isinstance(mission.purpose, MissionPurpose)
    ):
        raise ValueError("stored current mission is invalid")
    _validate_time(mission.activated_at)
    if mission.parent_version is not None and not _mission_matches_purpose(
        mission.text, mission.purpose
    ):
        raise ValueError("stored current mission does not match its purpose")


def _validate_active_mission(
    current: Mission,
    records: tuple[MissionRecord, ...],
    trusted_root: Mission,
) -> None:
    proposal_ids = tuple(record.proposal.proposal_id for record in records)
    version_ids = tuple(record.proposal.version_identity for record in records)
    if (
        len(set(proposal_ids)) != len(proposal_ids)
        or len(set(version_ids)) != len(version_ids)
        or trusted_root.version in version_ids
    ):
        raise ValueError("mission history contains duplicate identifiers")
    confirmed = tuple(
        record for record in records if record.status is MissionStatus.CONFIRMED
    )
    if current.parent_version is None:
        if current != trusted_root:
            raise ValueError("stored root mission differs from trusted initial state")
        if confirmed:
            raise ValueError("root mission conflicts with confirmed history")
    confirmed_by_version = {
        record.proposal.version_identity: record for record in confirmed
    }
    parent_activation = {trusted_root.version: trusted_root.activated_at}
    parent_activation.update(
        {
            version: record.confirmation.decided_at
            for version, record in confirmed_by_version.items()
            if record.confirmation is not None
        }
    )
    if current.parent_version is None:
        return

    current_record = confirmed_by_version.get(current.version)
    if (
        current_record is None
        or current_record.proposal.current_mission_version != current.parent_version
        or current_record.proposal.proposed_mission != current.text
        or current_record.proposal.purpose is not current.purpose
        or current_record.confirmation is None
        or current_record.confirmation.decided_at != current.activated_at
    ):
        raise ValueError("active mission does not match confirmed history")

    chain_versions: set[str] = set()
    cursor = current.version
    while cursor != trusted_root.version:
        if cursor in chain_versions or cursor not in confirmed_by_version:
            raise ValueError("confirmed mission history is not rooted")
        chain_versions.add(cursor)
        cursor = confirmed_by_version[cursor].proposal.current_mission_version
    if chain_versions != set(confirmed_by_version):
        raise ValueError("confirmed mission history contains a branch")
    if any(
        record.proposal.current_mission_version not in parent_activation
        or record.proposal.decided_at
        < parent_activation[record.proposal.current_mission_version]
        for record in records
    ):
        raise ValueError("mission proposal predates or lacks its parent")
