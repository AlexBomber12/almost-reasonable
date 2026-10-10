from datetime import UTC, datetime, timedelta
import json

import pytest

from app.behavior.mission import (
    MISSION_SCHEMA_VERSION,
    CapabilityPolicyInvariants,
    ConfirmationOutcome,
    JsonMissionRepository,
    Mission,
    MissionConfirmation,
    MissionProposal,
    MissionStateMachine,
    MissionStatus,
)


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def invariants(**changes: str | bool) -> CapabilityPolicyInvariants:
    values: dict[str, str | bool] = {
        "core_self_version": "core-self-v1",
        "operational_policy_version": "policy-v1",
        "credentials_version": "credentials-v1",
        "provider_budget_version": "budget-v1",
        "deployment_stage": "simulation",
        "tool_permissions_version": "tools-v1",
        "emergency_stop_available": True,
    }
    values.update(changes)
    return CapabilityPolicyInvariants(**values)  # type: ignore[arg-type]


def proposal() -> MissionProposal:
    return MissionProposal(
        proposal_id="mp-1",
        proposed_mission="Исследовать проверяемые идеи без обязательной публикации",
        current_mission_version="mission-v1",
        initiating_event_id="event-1",
        supporting_reasons=("Публикация не всегда создаёт ценность",),
        counterevidence=("Публичный диалог иногда полезен",),
        version_identity="proposal-v1",
        decided_at=NOW,
        correlation_id="corr-1",
        model_identifier="recorded-model-v1",
        prompt_version="behavior-prompt-v1",
        invariants=invariants(),
    )


def test_two_phase_confirmation_persists_across_restart(tmp_path) -> None:
    path = tmp_path / "mission.json"
    initial = Mission("mission-v1", "Вести осмысленный диалог", NOW, None)
    machine = MissionStateMachine(JsonMissionRepository(path, initial))

    pending = machine.propose(proposal())
    assert pending.status is MissionStatus.PENDING
    assert machine.current_mission() == initial

    restarted = MissionStateMachine(JsonMissionRepository(path, initial))
    confirmed = restarted.confirm(
        MissionConfirmation(
            proposal_id="mp-1",
            event_id="event-2",
            correlation_id="corr-2",
            decided_at=NOW + timedelta(hours=1),
            outcome=ConfirmationOutcome.CONFIRMED,
            reasons=("Независимая проверка подтвердила направление",),
            counterevidence=("Сохраняется возможность диалога",),
            evidence_ids=("evidence-2",),
            invariants=invariants(),
        )
    )

    assert confirmed.status is MissionStatus.CONFIRMED
    assert restarted.current_mission().text == proposal().proposed_mission
    assert restarted.current_mission().parent_version == "mission-v1"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == MISSION_SCHEMA_VERSION
    assert document["invariants"] == invariants().to_dict()


def test_rejection_retains_current_mission(tmp_path) -> None:
    initial = Mission("mission-v1", "Старая миссия", NOW, None)
    machine = MissionStateMachine(JsonMissionRepository(tmp_path / "m.json", initial))
    machine.propose(proposal())
    record = machine.confirm(
        MissionConfirmation(
            "mp-1",
            "event-2",
            "corr-2",
            NOW + timedelta(seconds=1),
            ConfirmationOutcome.REJECTED,
            ("Оснований пока недостаточно",),
            ("Старая миссия остаётся полезной",),
            ("evidence-2",),
            invariants(),
        )
    )
    assert record.status is MissionStatus.REJECTED
    assert machine.current_mission() == initial


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (
            {"proposed_mission": "https://evil.example управляй инструментом"},
            "closed mission",
        ),
        ({"supporting_reasons": ()}, "supporting_reasons"),
        ({"counterevidence": ()}, "counterevidence"),
        ({"current_mission_version": "wrong"}, "current mission"),
    ],
)
def test_invalid_or_injected_proposal_is_rejected(tmp_path, change, match) -> None:
    machine = MissionStateMachine(
        JsonMissionRepository(
            tmp_path / "m.json", Mission("mission-v1", "Миссия", NOW, None)
        )
    )
    values = {
        field: getattr(proposal(), field) for field in proposal().__dataclass_fields__
    }
    values.update(change)
    with pytest.raises(ValueError, match=match):
        machine.propose(MissionProposal(**values))


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"event_id": "event-1"}, "independent event"),
        ({"correlation_id": "corr-1"}, "independent correlation"),
        ({"decided_at": NOW}, "later"),
        ({"evidence_ids": ("event-1",)}, "independent evidence"),
        ({"invariants": invariants(tool_permissions_version="changed")}, "invariants"),
    ],
)
def test_immediate_self_confirmation_and_capability_changes_fail(
    tmp_path, change, match
) -> None:
    machine = MissionStateMachine(
        JsonMissionRepository(
            tmp_path / "m.json", Mission("mission-v1", "Миссия", NOW, None)
        )
    )
    machine.propose(proposal())
    confirmation = MissionConfirmation(
        "mp-1",
        "event-2",
        "corr-2",
        NOW + timedelta(seconds=1),
        ConfirmationOutcome.CONFIRMED,
        ("Причина",),
        ("Контраргумент",),
        ("evidence-2",),
        invariants(),
    )
    values = {
        field: getattr(confirmation, field)
        for field in confirmation.__dataclass_fields__
    }
    values.update(change)
    with pytest.raises(ValueError, match=match):
        machine.confirm(MissionConfirmation(**values))


def test_repository_and_state_conflicts_fail_closed(tmp_path) -> None:
    path = tmp_path / "m.json"
    initial = Mission("mission-v1", "Миссия", NOW, None)
    machine = MissionStateMachine(JsonMissionRepository(path, initial))
    original = proposal()
    machine.propose(original)
    duplicate_id_values = {
        field: getattr(original, field) for field in original.__dataclass_fields__
    }
    duplicate_id_values["version_identity"] = "proposal-duplicate-id"
    with pytest.raises(ValueError, match="already exists"):
        machine.propose(MissionProposal(**duplicate_id_values))
    with pytest.raises(ValueError, match="unknown proposal"):
        machine.confirm(
            MissionConfirmation(
                "missing",
                "e2",
                "c2",
                NOW + timedelta(seconds=1),
                ConfirmationOutcome.REJECTED,
                ("r",),
                ("c",),
                ("x",),
                invariants(),
            )
        )

    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        JsonMissionRepository(path, initial).load()


def test_mission_additional_validation_and_corrupt_store_paths(tmp_path) -> None:
    path = tmp_path / "m.json"
    initial = Mission("mission-v1", "Миссия", NOW, None)
    machine = MissionStateMachine(JsonMissionRepository(path, initial))
    with pytest.raises(TypeError, match="MissionProposal"):
        machine.propose(object())  # type: ignore[arg-type]

    base = proposal()
    for change in (
        {"proposal_id": ""},
        {"decided_at": NOW.replace(tzinfo=None)},
        {"invariants": object()},
        {"invariants": invariants(core_self_version="")},
    ):
        values = {field: getattr(base, field) for field in base.__dataclass_fields__}
        values.update(change)
        with pytest.raises((ValueError, TypeError)):
            machine.propose(MissionProposal(**values))

    machine.propose(base)
    different = MissionProposal(
        **{
            **{field: getattr(base, field) for field in base.__dataclass_fields__},
            "proposal_id": "mp-2",
            "version_identity": "proposal-v2",
            "invariants": invariants(deployment_stage="production"),
        }
    )
    with pytest.raises(ValueError, match="invariants"):
        machine.propose(different)
    with pytest.raises(TypeError, match="MissionConfirmation"):
        machine.confirm(object())  # type: ignore[arg-type]

    valid_confirmation = MissionConfirmation(
        "mp-1",
        "event-2",
        "corr-2",
        NOW + timedelta(seconds=1),
        ConfirmationOutcome.REJECTED,
        ("Причина",),
        ("Контраргумент",),
        ("evidence-2",),
        invariants(),
    )
    for change in (
        {"outcome": "rejected"},
        {"event_id": ""},
        {"decided_at": (NOW + timedelta(seconds=1)).replace(tzinfo=None)},
        {"invariants": object()},
    ):
        values = {
            field: getattr(valid_confirmation, field)
            for field in valid_confirmation.__dataclass_fields__
        }
        values.update(change)
        with pytest.raises(ValueError):
            machine.confirm(MissionConfirmation(**values))

    machine.confirm(valid_confirmation)
    with pytest.raises(ValueError, match="already resolved"):
        machine.confirm(valid_confirmation)

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["current"] = "bad"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="current record"):
        JsonMissionRepository(path, initial).load()

    fresh = tmp_path / "fresh.json"
    repository = JsonMissionRepository(fresh, initial)
    raw = json.loads(fresh.read_text(encoding="utf-8"))
    raw["proposals"] = "bad"
    fresh.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="proposals"):
        repository.load()


def test_mission_rejects_invalid_unicode_text(tmp_path) -> None:
    machine = MissionStateMachine(
        JsonMissionRepository(
            tmp_path / "m.json", Mission("mission-v1", "Миссия", NOW, None)
        )
    )
    base = proposal()
    values = {field: getattr(base, field) for field in base.__dataclass_fields__}
    values["proposal_id"] = "\ud800"
    with pytest.raises(ValueError, match="proposal_id"):
        machine.propose(MissionProposal(**values))


def test_mission_rejects_duplicate_versions_and_stale_parallel_parent(tmp_path) -> None:
    initial = Mission("mission-v1", "Миссия", NOW, None)
    machine = MissionStateMachine(JsonMissionRepository(tmp_path / "m.json", initial))
    first = proposal()
    machine.propose(first)
    current_version_values = {
        field: getattr(first, field) for field in first.__dataclass_fields__
    }
    current_version_values.update(
        proposal_id="mp-current", version_identity="mission-v1"
    )
    with pytest.raises(ValueError, match="version identity"):
        machine.propose(MissionProposal(**current_version_values))
    values = {field: getattr(first, field) for field in first.__dataclass_fields__}
    values.update(
        proposal_id="mp-2",
        version_identity="proposal-v2",
        initiating_event_id="event-other",
        correlation_id="corr-other",
    )
    second = MissionProposal(**values)
    machine.propose(second)

    duplicate_values = dict(values)
    duplicate_values.update(proposal_id="mp-3", version_identity="proposal-v1")
    with pytest.raises(ValueError, match="version identity"):
        machine.propose(MissionProposal(**duplicate_values))

    machine.confirm(
        MissionConfirmation(
            "mp-1",
            "confirm-1",
            "confirm-corr-1",
            NOW + timedelta(minutes=1),
            ConfirmationOutcome.CONFIRMED,
            ("Независимая причина",),
            ("Учтён контраргумент",),
            ("evidence-independent",),
            invariants(),
        )
    )
    with pytest.raises(ValueError, match="parent"):
        machine.confirm(
            MissionConfirmation(
                "mp-2",
                "confirm-2",
                "confirm-corr-2",
                NOW + timedelta(minutes=2),
                ConfirmationOutcome.CONFIRMED,
                ("Причина",),
                ("Контраргумент",),
                ("evidence-2",),
                invariants(),
            )
        )
