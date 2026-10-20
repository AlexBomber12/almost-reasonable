"""Closed operator-owned catalog authority with separate private/public projections."""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from app.core.public_research_authority import (
    CONFIG,
    PURPOSE,
    validate_trusted_directories,
)
from app.trusted.public_research import PublicResearchRequest, encode_public_request
from app.research.provider import default_hosted_search_config

VERSION = "public-research-profile-v1"
PUBLIC_PURPOSE = "public-deliberation-catalog-research-v1"
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,120}\Z")


def provider_configuration() -> dict[str, object]:
    """The complete fixed one-attempt provider policy reviewed by the operator."""
    config = replace(default_hosted_search_config(), enabled=True, max_attempts=1)
    result = asdict(config)
    result["search_context_size"] = config.search_context_size.value
    return result


@dataclass(frozen=True, slots=True, repr=False)
class CatalogTopic:
    topic_id: str
    question: str
    limit: int


@dataclass(frozen=True, slots=True, repr=False)
class ResearchProfile:
    role: str
    profile_id: str
    authorization: str
    application_scope: str
    peer_uid: int
    issued_at: datetime
    expires_at: datetime
    reviewer: str
    reviewed_at: datetime
    aggregate_limit: int
    topics: tuple[CatalogTopic, ...]
    digest: str
    principal_id: str | None = None
    authenticated_source_ref: str | None = None
    purpose: str = PURPOSE
    account_ref: str | None = None

    def topic(self, choice: str) -> CatalogTopic | None:
        if type(choice) is not str:
            return None
        return next(
            (
                topic
                for topic in self.topics
                if choice in {topic.topic_id, topic.question}
            ),
            None,
        )

    def request(self, operation_id: str, choice: str) -> PublicResearchRequest:
        topic = self.topic(choice)
        if topic is None:
            raise ValueError("unknown reviewed public question")
        request = PublicResearchRequest(
            operation_id, topic.question, self.authorization
        )
        encode_public_request(request)
        return request


def load_research_profile(path: str | Path, *, role: str) -> ResearchProfile:
    """Read a protected profile without accepting duplicate or unknown fields."""
    path = Path(path)
    if role not in {"application", "worker"} or not path.is_absolute():
        raise ValueError("invalid profile location")
    if path.resolve(strict=True) != path:
        raise ValueError("profile path contains a link")
    validate_trusted_directories(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > 16_384
        ):
            raise ValueError("unsafe profile file")
        raw = os.read(descriptor, 16_385)
    finally:
        os.close(descriptor)
    if len(raw) > 16_384:
        raise ValueError("oversized profile")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate profile field")
            value[key] = item
        return value

    data = json.loads(raw, object_pairs_hook=unique)
    common = {
        "version",
        "role",
        "purpose",
        "configuration",
        "provider_configuration",
        "profile_id",
        "authorization",
        "application_scope",
        "peer_uid",
        "issued_at",
        "expires_at",
        "reviewer",
        "reviewed_at",
        "aggregate_limit",
        "topics",
    }
    private = {"principal_id", "authenticated_source_ref"}
    if type(data) is not dict:
        raise ValueError("invalid profile fields")
    purpose = data.get("purpose")
    if purpose not in (PURPOSE, PUBLIC_PURPOSE) or set(data) != common | (
        private if purpose == PURPOSE and role == "application" else set()
    ) | ({"account_ref"} if purpose == PUBLIC_PURPOSE else set()):
        raise ValueError("invalid profile fields")
    if (
        data["version"] != VERSION
        or data["role"] != role
        or data["purpose"] != purpose
        or data["configuration"] != CONFIG
        or type(data["provider_configuration"]) is not dict
        or json.dumps(data["provider_configuration"], sort_keys=True)
        != json.dumps(provider_configuration(), sort_keys=True)
        or type(data["profile_id"]) is not str
        or _TOKEN.fullmatch(data["profile_id"]) is None
        or type(data["authorization"]) is not str
        or _TOKEN.fullmatch(data["authorization"]) is None
        or type(data["application_scope"]) is not str
        or _IDENTIFIER.fullmatch(data["application_scope"]) is None
        or type(data["peer_uid"]) is not int
        or not 0 <= data["peer_uid"] <= 2**32 - 2
        or type(data["reviewer"]) is not str
        or not data["reviewer"].strip()
        or type(data["aggregate_limit"]) is not int
        or not 1 <= data["aggregate_limit"] <= 100
    ):
        raise ValueError("invalid profile authority")
    issued = datetime.fromisoformat(data["issued_at"])
    expires = datetime.fromisoformat(data["expires_at"])
    reviewed = datetime.fromisoformat(data["reviewed_at"])
    if (
        issued.tzinfo is None
        or expires.tzinfo is None
        or reviewed.tzinfo is None
        or not reviewed <= issued < expires <= issued + timedelta(days=30)
    ):
        raise ValueError("invalid profile interval")
    values = data["topics"]
    if type(values) is not list or not 1 <= len(values) <= 16:
        raise ValueError("invalid profile catalog")
    topics = []
    for value in values:
        if type(value) is not dict or set(value) != {"id", "question", "limit"}:
            raise ValueError("invalid catalog entry")
        if (
            type(value["id"]) is not str
            or _IDENTIFIER.fullmatch(value["id"]) is None
            or type(value["limit"]) is not int
            or not 1 <= value["limit"] <= data["aggregate_limit"]
        ):
            raise ValueError("invalid catalog limit")
        request = PublicResearchRequest(
            "profile-validation", value["question"], data["authorization"]
        )
        encode_public_request(request)
        if purpose == PUBLIC_PURPOSE and len(value["question"].encode("utf-8")) > 512:
            raise ValueError("public catalog question exceeds deliberation bound")
        topics.append(CatalogTopic(value["id"], value["question"], value["limit"]))
    if len({topic.topic_id for topic in topics}) != len(topics) or len(
        {topic.question for topic in topics}
    ) != len(topics):
        raise ValueError("duplicate catalog choice")
    if {topic.topic_id for topic in topics} & {topic.question for topic in topics}:
        raise ValueError("ambiguous catalog choice")
    principal = source = account_ref = None
    if purpose == PUBLIC_PURPOSE:
        account_ref = data["account_ref"]
        if type(account_ref) is not str or _IDENTIFIER.fullmatch(account_ref) is None:
            raise ValueError("invalid public profile account")
    elif role == "application":
        principal, source = data["principal_id"], data["authenticated_source_ref"]
        if (
            type(principal) is not str
            or not principal
            or type(source) is not str
            or not source
        ):
            raise ValueError("invalid private profile scope")
    public = {key: data[key] for key in common if key != "role"}
    digest = hashlib.sha256(
        json.dumps(
            public, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    return ResearchProfile(
        role,
        data["profile_id"],
        data["authorization"],
        data["application_scope"],
        data["peer_uid"],
        issued,
        expires,
        data["reviewer"],
        reviewed,
        data["aggregate_limit"],
        tuple(topics),
        digest,
        principal,
        source,
        purpose,
        account_ref,
    )
