"""Whole-record omission preserves useful memory and complete provenance."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest

import test_private_public_activity as activity_cases
import test_public_private_intellectual as intellectual_cases
from test_private_public_activity import publication, POST
from test_agent_intellectual_state import private_submit

dialogue = activity_cases.dialogue
integrated = intellectual_cases.integrated


@pytest.mark.parametrize("with_public", [False, True])
def test_dependency_saturation_keeps_useful_memory_and_public_experience(
    dialogue, monkeypatch, with_public
):
    from app.cognition.contracts import InterestMemoryProposal
    from app.cognition.pipeline import CognitionEngine
    from app.core.self_state import InterestStatus
    from app.core.self_state_store import SelfStateStore
    from test_cognition_pipeline import (
        FakeExtractor,
        append_event,
        principal,
        proposal_context,
    )
    import test_cognition_pipeline as cognition

    _, _, _, _, _, _, app, a, b, events, _, _, sdk, server = dialogue
    publication(dialogue)
    runtime = app._conversations
    if not with_public:
        runtime._public_intellectual_source = None
    monkeypatch.setattr(cognition, "NOW", runtime._clock() - timedelta(minutes=2))
    roots = [
        append_event(events, principal("a"), f"source-{i}", "PRIVATE_BUDGET_A")
        for i in range(99)
    ]
    groups = (roots[:49], roots[49:98], roots[98:])
    for index, evidence in enumerate(groups):
        candidate = InterestMemoryProposal(
            replace(
                proposal_context(evidence[0], f"memory-{index}"),
                supporting_event_ids=tuple(evidence),
            ),
            f"interest-{index}",
            f"Astronomy memory {index}",
            70,
            InterestStatus.ACTIVE,
            "Синтетический интерес к астрономии.",
        )
        result = CognitionEngine(
            events, SelfStateStore(events), extractor=FakeExtractor(candidate)
        ).process(principal("a"), evidence[0], evidence_event_ids=tuple(evidence[1:]))
        assert result.status.value == "persisted", result
    result = private_submit(
        server, a, "saturated", "Astronomy: почему ты написал свой пост?"
    )
    assert result["model_state"] == "completed"
    raw = sdk.calls[-1]["input"]
    frame = json.loads(raw)
    assert len(frame["memories"]) == 2 and frame["memories_omitted"]
    assert frame["intellectual_state"] is not None
    if with_public:
        assert POST in raw and "Русская архитектура" in raw
    with events._open() as db:
        snapshot = json.loads(
            db.execute(
                "SELECT snapshot FROM agent_intellectual_operations WHERE audience='private' "
                "AND owner='participant-a' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert len(snapshot["dependencies"]) <= 100
        for admitted in frame["memories"]:
            index = int(admitted["text"].split()[-1])
            assert {"event:" + root for root in groups[index]} <= snapshot[
                "dependencies"
            ].keys()
    assert len(SelfStateStore(events).retrieve_private_candidates(principal("a"))) == 3
    private_submit(server, b, "other-participant", "Почему ты написал свой пост?")
    assert "PRIVATE_BUDGET_A" not in sdk.calls[-1]["input"]
    assert "Astronomy memory" not in sdk.calls[-1]["input"]
