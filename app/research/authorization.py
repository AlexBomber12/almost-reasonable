"""Fail-closed authorization for sending a research brief outside the process."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
import hashlib
import hmac
import json
import secrets

from .brief import ResearchBrief


_AUTHORIZATION_AUTHORITY = object()
_AUTHORIZATION_INTEGRITY_KEY = secrets.token_bytes(32)
_SYNTHETIC_AUTHORIZATION_TTL = timedelta(minutes=10)


class SyntheticPublicResearchFixture(StrEnum):
    """Closed application-owned inputs used by deterministic offline tests."""

    AURORAL_SUBSTORMS = "auroral-substorms-v1"
    AURORAL_SUBSTORMS_SINGLE_RESULT = "auroral-substorms-single-result-v1"


@dataclass(frozen=True, slots=True, repr=False)
class ResearchAuthorization:
    """Opaque, short-lived evidence that one exact public brief was authorized."""

    issuer: str
    subject_digest: str
    brief_config_version: str
    provider_config_version: str
    issued_at: datetime
    expires_at: datetime
    _authority: object = field(repr=False, compare=False)
    _integrity: bytes = field(default=b"", repr=False, compare=False)

    def permits(
        self,
        brief: ResearchBrief,
        *,
        provider_config_version: str,
        now: datetime,
    ) -> bool:
        if (
            self._authority is not _AUTHORIZATION_AUTHORITY
            or not isinstance(self._integrity, bytes)
            or type(brief) is not ResearchBrief
            or not brief.built_by_screened_pipeline
            or not _valid_timestamp(now)
        ):
            return False
        try:
            expected = _authorization_integrity(
                self.issuer,
                self.subject_digest,
                self.brief_config_version,
                self.provider_config_version,
                self.issued_at,
                self.expires_at,
            )
            subject_digest = _brief_digest(brief)
        except (AttributeError, TypeError, ValueError):
            return False
        return (
            hmac.compare_digest(self._integrity, expected)
            and self.issuer
            in {
                f"synthetic-fixture:{fixture.value}"
                for fixture in SyntheticPublicResearchFixture
            }
            and hmac.compare_digest(self.subject_digest, subject_digest)
            and self.brief_config_version == brief.trusted_config_version
            and self.provider_config_version == provider_config_version
            and self.issued_at <= now < self.expires_at
        )


def authorize_synthetic_public_research(
    fixture: SyntheticPublicResearchFixture,
    brief: ResearchBrief,
    *,
    provider_config_version: str,
    issued_at: datetime,
) -> ResearchAuthorization:
    """Authorize one closed public fixture; arbitrary text has no issuer here."""

    if type(fixture) is not SyntheticPublicResearchFixture:
        raise TypeError("fixture must be a SyntheticPublicResearchFixture")
    if type(brief) is not ResearchBrief or not brief.built_by_screened_pipeline:
        raise ValueError("brief did not pass the screened construction pipeline")
    if not isinstance(provider_config_version, str) or not provider_config_version:
        raise ValueError("invalid provider configuration version")
    if not _valid_timestamp(issued_at):
        raise ValueError("invalid authorization timestamp")
    if brief.to_json() != _synthetic_fixture_json(fixture):
        raise ValueError("brief does not match the trusted synthetic fixture")

    authorization = ResearchAuthorization(
        issuer=f"synthetic-fixture:{fixture.value}",
        subject_digest=_brief_digest(brief),
        brief_config_version=brief.trusted_config_version,
        provider_config_version=provider_config_version,
        issued_at=issued_at,
        expires_at=issued_at + _SYNTHETIC_AUTHORIZATION_TTL,
        _authority=_AUTHORIZATION_AUTHORITY,
    )
    object.__setattr__(
        authorization,
        "_integrity",
        _authorization_integrity(
            authorization.issuer,
            authorization.subject_digest,
            authorization.brief_config_version,
            authorization.provider_config_version,
            authorization.issued_at,
            authorization.expires_at,
        ),
    )
    return authorization


def _synthetic_fixture_json(fixture: SyntheticPublicResearchFixture) -> str:
    if fixture in {
        SyntheticPublicResearchFixture.AURORAL_SUBSTORMS,
        SyntheticPublicResearchFixture.AURORAL_SUBSTORMS_SINGLE_RESULT,
    }:
        result_budget = (
            1
            if fixture is SyntheticPublicResearchFixture.AURORAL_SUBSTORMS_SINGLE_RESULT
            else 3
        )
        return json.dumps(
            {
                "question": "How do auroral substorms form?",
                "broad_topic": None,
                "purpose": None,
                "language_preferences": [],
                "time_requirement": None,
                "public_reference_identifiers": [],
                "result_budget": result_budget,
                "trusted_config_version": "research-config-v1",
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    raise ValueError("unknown synthetic fixture")


def _brief_digest(brief: ResearchBrief) -> str:
    return hashlib.sha256(brief.to_json().encode("utf-8")).hexdigest()


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    try:
        return value.utcoffset() is not None
    except (OverflowError, ValueError):
        return False


def _authorization_integrity(
    issuer: str,
    subject_digest: str,
    brief_config_version: str,
    provider_config_version: str,
    issued_at: datetime,
    expires_at: datetime,
) -> bytes:
    if not _valid_timestamp(issued_at) or not _valid_timestamp(expires_at):
        raise ValueError("invalid authorization timestamps")
    serialized = json.dumps(
        {
            "issuer": issuer,
            "subject_digest": subject_digest,
            "brief_config_version": brief_config_version,
            "provider_config_version": provider_config_version,
            "issued_at": issued_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hmac.digest(
        _AUTHORIZATION_INTEGRITY_KEY, serialized.encode("utf-8"), "sha256"
    )
