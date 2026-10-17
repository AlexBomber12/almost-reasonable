"""Operator-owned, exact public research approval files.

These files are local bootstrap inputs, never participant or model messages. The
application and worker receive different projections of the operator decision.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import stat

from app.trusted.public_research import PublicResearchRequest, encode_public_request

PURPOSE = "private-conversation-public-research-v1"
CONFIG = "public-research-v1"


@dataclass(frozen=True, slots=True, repr=False)
class WorkerApproval:
    request: PublicResearchRequest
    peer_uid: int
    issued_at: datetime
    expires_at: datetime
    reviewer: str
    reviewed_at: datetime
    file_sha256: str


@dataclass(frozen=True, slots=True, repr=False)
class ApplicationApproval:
    request: PublicResearchRequest
    peer_uid: int
    principal_id: str
    authenticated_source_ref: str
    event_id: str
    input_sha256: str
    issued_at: datetime
    expires_at: datetime
    reviewer: str
    reviewed_at: datetime
    file_sha256: str


def load_operator_approval(path: str | Path, *, role: str):
    """Read a local operator-reviewed file with no writable shared path component."""
    path = Path(path)
    if role not in {"application", "worker"} or not path.is_absolute():
        raise ValueError("invalid approval location")
    if path.resolve(strict=True) != path:
        raise ValueError("approval path must not contain links")
    parent = path.parent.stat()
    if parent.st_mode & 0o022:
        raise ValueError("approval directory is writable by other accounts")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or info.st_mode & 0o077
            or info.st_size > 8192
        ):
            raise ValueError("unsafe approval file")
        raw = os.read(descriptor, 8193)
    finally:
        os.close(descriptor)
    if len(raw) > 8192:
        raise ValueError("oversized approval file")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate approval field")
            value[key] = item
        return value

    data = json.loads(raw, object_pairs_hook=unique)
    common = {
        "version",
        "role",
        "purpose",
        "configuration",
        "execution_allowance",
        "operation_id",
        "question",
        "authorization",
        "peer_uid",
        "issued_at",
        "expires_at",
        "reviewer",
        "reviewed_at",
    }
    expected = common | (
        {"principal_id", "authenticated_source_ref", "event_id", "input_sha256"}
        if role == "application"
        else set()
    )
    if (
        type(data) is not dict
        or set(data) != expected
        or type(data["execution_allowance"]) is not int
        or any(
            data[key] != value
            for key, value in (
                ("version", "int23-operator-approval-v1"),
                ("role", role),
                ("purpose", PURPOSE),
                ("configuration", CONFIG),
                ("execution_allowance", 1),
            )
        )
    ):
        raise ValueError("invalid approval contract")
    if (
        type(data["peer_uid"]) is not int
        or not 0 <= data["peer_uid"] <= 2**32 - 2
        or type(data["reviewer"]) is not str
        or not data["reviewer"].strip()
    ):
        raise ValueError("invalid operator evidence")
    issued = datetime.fromisoformat(data["issued_at"])
    expires = datetime.fromisoformat(data["expires_at"])
    reviewed = datetime.fromisoformat(data["reviewed_at"])
    if (
        issued.tzinfo is None
        or expires.tzinfo is None
        or reviewed.tzinfo is None
        or not reviewed <= issued < expires <= issued + timedelta(minutes=10)
    ):
        raise ValueError("invalid approval interval")
    request = PublicResearchRequest(
        data["operation_id"], data["question"], data["authorization"]
    )
    encode_public_request(request)
    file_sha256 = hashlib.sha256(raw).hexdigest()
    if role == "worker":
        return WorkerApproval(
            request,
            data["peer_uid"],
            issued,
            expires,
            data["reviewer"],
            reviewed,
            file_sha256,
        )
    if (
        type(data["principal_id"]) is not str
        or not data["principal_id"]
        or type(data["authenticated_source_ref"]) is not str
        or not data["authenticated_source_ref"]
        or type(data["event_id"]) is not str
        or not data["event_id"]
        or type(data["input_sha256"]) is not str
        or len(data["input_sha256"]) != 64
        or any(
            character not in "0123456789abcdef" for character in data["input_sha256"]
        )
    ):
        raise ValueError("invalid private input binding")
    return ApplicationApproval(
        request,
        data["peer_uid"],
        data["principal_id"],
        data["authenticated_source_ref"],
        data["event_id"],
        data["input_sha256"],
        issued,
        expires,
        data["reviewer"],
        reviewed,
        file_sha256,
    )
