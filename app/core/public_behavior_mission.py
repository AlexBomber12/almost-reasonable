"""Protected, closed public mission snapshot for Stage 3 behavior."""

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import stat

from app.behavior.mission import MissionPurpose, _MISSION_PURPOSE_TEXT
from app.core.public_behavior import _unique_fields
from app.threads.domain import require_reference


class PublicMissionContext:
    """Read-only application view of an operator-provisioned public mission file."""

    def __init__(self, path: Path, *, clock=lambda: datetime.now(UTC)):
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError("absolute public mission path required")
        self.path = path
        self.clock = clock
        self.snapshot()

    def snapshot(self):
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
        return value["text"], {
            "version": value["version"],
            "digest": hashlib.sha256(canonical).hexdigest(),
            "purpose": purpose.value,
        }

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
