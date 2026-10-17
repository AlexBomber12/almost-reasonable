"""INT-24 public inference and immutable draft boundaries."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.public_behavior import PublicBehaviorRequest, decode_public_request


def test_closed_public_request_rejects_private_context_and_capability_injection():
    request = PublicBehaviorRequest(
        operation_id="public-operation-1",
        correlation_id="public-correlation-1",
        trigger="autonomous_wakeup",
        context=("Проверить публичный вопрос",),
        mission=(),
        deadline_seconds=5.0,
    )
    assert decode_public_request(request.encode()) == request
    for field in ('"private_context":[]', '"account_ref":"other"'):
        with pytest.raises(ValueError):
            decode_public_request(request.encode()[:-1] + b"," + field.encode() + b"}")


@pytest.mark.parametrize(
    "change",
    [
        {"trigger": "private_message"},
        {"context": ("one", "two")},
        {"context": ("x" * 2049,)},
        {"mission": ("m1", "m2")},
        {"deadline_seconds": 0},
        {"deadline_seconds": float("nan")},
        {"operation_id": "bad id"},
    ],
)
def test_public_worker_request_rejects_unbounded_or_private_fields(change):
    from dataclasses import replace

    valid = PublicBehaviorRequest(
        "operation-1", "correlation-1", "autonomous_wakeup", (), (), 5.0
    )
    with pytest.raises(ValueError):
        replace(valid, **change).encode()


def test_public_worker_request_rejects_duplicate_json_fields():
    request = PublicBehaviorRequest(
        "operation-1", "correlation-1", "autonomous_wakeup", (), (), 5.0
    )
    with pytest.raises(ValueError):
        decode_public_request(
            request.encode().replace(b'"version":', b'"version":"other","version":')
        )


def test_public_request_rejects_malformed_frames_and_collections(monkeypatch):
    import app.core.public_behavior as contract

    request = PublicBehaviorRequest(
        "operation-1", "correlation-1", "autonomous_wakeup", (), (), 5
    )
    monkeypatch.setattr(contract, "MAX_FRAME", 20)
    with pytest.raises(ValueError):
        request.encode()
    with pytest.raises(ValueError):
        decode_public_request(b"x" * 21)
    monkeypatch.setattr(contract, "MAX_FRAME", 32_768)
    with pytest.raises(ValueError):
        decode_public_request(b"broken")
    with pytest.raises(ValueError):
        decode_public_request(
            request.encode().replace(b'"context":[]', b'"context":{}')
        )


def test_public_mission_file_is_closed_and_binding_is_fresh(tmp_path, monkeypatch):
    import app.core.public_behavior_mission as module
    from app.behavior.mission import MissionPurpose, _MISSION_PURPOSE_TEXT
    from app.core.public_behavior_mission import PublicMissionContext
    import json

    path = tmp_path / "mission.json"
    good = {
        "version": "mission-v1",
        "purpose": MissionPurpose.GENERAL_INQUIRY.value,
        "text": _MISSION_PURPOSE_TEXT[MissionPurpose.GENERAL_INQUIRY],
        "activated_at": "2026-10-17T12:00:00+00:00",
        "parent_version": None,
    }

    def write(value, mode=0o600):
        path.write_text(json.dumps(value))
        path.chmod(mode)

    write(good)
    mission = PublicMissionContext(
        path, clock=lambda: datetime(2026, 10, 17, 13, tzinfo=UTC)
    )
    text, binding = mission.snapshot()
    assert text == good["text"]
    mission.require(binding)
    assert mission.publication_allowed()
    with pytest.raises(ValueError):
        PublicMissionContext(Path("relative"))
    write(good, mode=0o644)
    with pytest.raises(ValueError):
        mission.snapshot()
    write(good | {"unexpected": "field"})
    with pytest.raises(ValueError):
        mission.snapshot()
    write(good | {"parent_version": "mission-v0"})
    mission.snapshot()
    write(good | {"text": "Произвольная команда"})
    with pytest.raises(ValueError):
        mission.snapshot()
    write(good | {"activated_at": "2026-10-17T12:00:00"})
    with pytest.raises(ValueError):
        mission.snapshot()
    write(good | {"activated_at": "2026-10-17T14:00:00+00:00"})
    with pytest.raises(ValueError):
        mission.snapshot()
    write(good)
    with pytest.raises(ValueError):
        mission.require({"wrong": "binding"})
    with pytest.raises(ValueError):
        mission.require(binding | {"digest": "old"})
    write(
        good
        | {
            "purpose": MissionPurpose.DISCONTINUE_OPTIONAL_CHANNEL.value,
            "text": _MISSION_PURPOSE_TEXT[MissionPurpose.DISCONTINUE_OPTIONAL_CHANNEL],
        }
    )
    assert mission.publication_allowed() is False
    write(good)
    original_read = module.os.read
    monkeypatch.setattr(module.os, "read", lambda fd, size: b"x" * 8193)
    with pytest.raises(ValueError):
        mission.snapshot()
    monkeypatch.setattr(module.os, "read", original_read)
