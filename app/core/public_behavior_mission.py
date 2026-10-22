"""Protected, closed public mission snapshot for Stage 3 behavior."""

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from app.behavior.mission import (
    Mission,
    MissionPurpose,
    _MISSION_PURPOSE_TEXT,
    _document_from_dict,
)
from app.core.public_behavior import _unique_fields
from app.threads.domain import require_reference


class PublicMissionContext:
    """Read-only application view of an operator-provisioned public mission file."""

    def __init__(self, path: Path, *, clock=lambda: datetime.now(UTC)):
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError("absolute public mission path required")
        self.path = path
        self.clock = clock
        self._evolution_path = None
        self._root_digest = None
        self.snapshot()

    def snapshot(self):
        text, binding, root = self._root_snapshot()
        if self._evolution_path is None:
            return text, binding
        if binding["digest"] != self._root_digest:
            raise ValueError("public mission root changed")
        with sqlite3.connect(self._evolution_path) as db:
            row = db.execute(
                "SELECT document FROM public_mission_state WHERE id=1"
            ).fetchone()
        if row is None:
            raise ValueError("public mission state missing")
        document = _document_from_dict(json.loads(row[0]), root)
        for record in document.proposals:
            if record.confirmation is not None and not self._evidence_authority(
                record.proposal.initiating_source_id,
                record.confirmation.evidence_sources,
            ):
                raise ValueError("public mission evidence withdrawn")
        current = document.current
        if current == root:
            return text, binding
        value = {
            "version": current.version,
            "purpose": current.purpose.value,
            "text": current.text,
            "activated_at": current.activated_at.isoformat(),
            "parent_version": current.parent_version,
        }
        canonical = json.dumps(value, sort_keys=True, ensure_ascii=True).encode()
        return current.text, {
            "version": current.version,
            "digest": hashlib.sha256(canonical).hexdigest(),
            "purpose": current.purpose.value,
        }

    def bind_evolution(self, journal_path, evidence_authority, *, expected_root=None):
        if self._evolution_path is not None:
            raise ValueError("public mission evolution already bound")
        _, binding, root = self._root_snapshot()
        if expected_root is not None and root != expected_root:
            raise ValueError("public mission root changed during evolution binding")
        self._root_digest = binding["digest"]
        self._evolution_path = Path(journal_path)
        self._evidence_authority = evidence_authority
        try:
            self.snapshot()
        except Exception:
            self._root_digest = None
            self._evolution_path = None
            self._evidence_authority = None
            raise

    def root_mission(self):
        return self._root_snapshot()[2]

    def _root_snapshot(self):
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 8192
            ):
                raise ValueError("public mission file is unsafe")
            data = os.read(descriptor, 8193)
        finally:
            os.close(descriptor)
        if len(data) > 8192:
            raise ValueError("public mission file is too large")
        value = json.loads(data, object_pairs_hook=_unique_fields)
        if type(value) is not dict or set(value) != {
            "version",
            "purpose",
            "text",
            "activated_at",
            "parent_version",
        }:
            raise ValueError("invalid public mission record")
        require_reference("mission version", value["version"])
        if value["parent_version"] is not None:
            require_reference("parent mission version", value["parent_version"])
        purpose = MissionPurpose(value["purpose"])
        if value["text"] != _MISSION_PURPOSE_TEXT[purpose]:
            raise ValueError("public mission is not closed content")
        activated = datetime.fromisoformat(value["activated_at"])
        if activated.tzinfo is None or activated > self.clock():
            raise ValueError("public mission time is not active")
        canonical = json.dumps(value, sort_keys=True, ensure_ascii=True).encode()
        return (
            value["text"],
            {
                "version": value["version"],
                "digest": hashlib.sha256(canonical).hexdigest(),
                "purpose": purpose.value,
            },
            Mission(
                value["version"],
                value["text"],
                activated,
                value["parent_version"],
                purpose,
            ),
        )

    def require(self, binding):
        if type(binding) is not dict or set(binding) != {
            "version",
            "digest",
            "purpose",
        }:
            raise ValueError("public mission binding missing")
        if self.snapshot()[1] != binding:
            raise ValueError("public mission changed")

    def publication_allowed(self):
        return (
            self.snapshot()[1]["purpose"]
            != MissionPurpose.DISCONTINUE_OPTIONAL_CHANNEL.value
        )
