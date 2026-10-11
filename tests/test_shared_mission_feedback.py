"""Regressions for current-HEAD review: proposal identity and stale evidence."""

from dataclasses import replace
from datetime import timedelta

import pytest

from app.behavior.mission import ConfirmationOutcome, MissionPurpose
from app.behavior.shared_mission import (
    SharedMissionAdapter,
    SyntheticMissionConfirmationApproval,
)
from app.cognition.shared import SharedIntelligence
from app.core.events import EventAudience
from app.core.store import EventStore
from app.privacy.application import PrivacyReadGate
import test_shared_intelligence as sharing_tests
from test_event_store import private_input
from test_cognition_pipeline import principal


@pytest.fixture
def case(tmp_path):
    shared = sharing_tests.shared.__wrapped__(tmp_path)
    return sharing_tests.mission_case.__wrapped__(shared)


def evidence(case, label, operation, *, author="b", item="independent", revision=0):
    events, gate, tick, _, _, _, _ = case
    events.append_private_input(
        principal(author),
        replace(
            private_input(label, "Synthetic independently supplied observations."),
            audience=EventAudience.private_for(principal(author).principal_id),
            occurred_at=tick[0],
        ),
    )
    approved = replace(
        sharing_tests.approval(
            f"event-{label}",
            op=operation,
            author=author,
            item=item,
            revision=revision,
        ),
        allow_mission_evidence=True,
    )
    SharedIntelligence(
        events, gate, approvals=(approved,), clock=lambda: tick[0]
    ).commit(approved)


def test_later_proposal_reusing_shared_origin_preserves_earlier_confirmation(case):
    events, gate, tick, adapter, args, proposal, approvals = case
    adapter.propose(**proposal)
    tick[0] += timedelta(seconds=1)
    evidence(case, "first-independent", "shared-b")
    adapter.confirm(approvals[2], correlation_id="corr-b", counterevidence_checked=True)
    assert adapter.current_mission().version == "mission-v2"
    tick[0] += timedelta(seconds=1)
    later = proposal | dict(
        operation_id="later-proposal",
        parent_version="mission-v2",
        proposed_version="mission-v3",
        correlation_id="corr-later",
        purpose=MissionPurpose.DIALOGUE,
    )
    adapter.propose(**later)
    assert adapter.current_mission().version == "mission-v2"
    restarted_store = EventStore(events.database_path)
    restarted_gate = PrivacyReadGate(gate._ledger, restarted_store)
    assert restarted_gate.reconcile()
    later_approval = SyntheticMissionConfirmationApproval(
        "later-proposal", "shared-c", ConfirmationOutcome.CONFIRMED
    )
    restarted = SharedMissionAdapter(
        restarted_store,
        restarted_gate,
        **(args | {"confirmations": (*approvals, later_approval)}),
    )
    assert restarted.current_mission().version == "mission-v2"
    tick[0] += timedelta(seconds=1)
    evidence(case, "next-independent", "shared-c", author="c", item="next-independent")
    restarted.confirm(
        later_approval, correlation_id="corr-c", counterevidence_checked=True
    )
    assert restarted.current_mission().version == "mission-v3"


def test_new_confirmation_rejects_superseded_evidence_but_history_remains_valid(case):
    _, _, tick, adapter, _, proposal, approvals = case
    adapter.propose(**proposal)
    tick[0] += timedelta(seconds=1)
    evidence(case, "original-evidence", "shared-b")
    tick[0] += timedelta(seconds=1)
    evidence(case, "revised-evidence", "shared-c", revision=1)
    with pytest.raises(ValueError, match="current shared revision"):
        adapter.confirm(
            approvals[2], correlation_id="corr-b", counterevidence_checked=True
        )
    assert adapter.current_mission().version == "mission-v1"
    adapter.confirm(approvals[4], correlation_id="corr-c", counterevidence_checked=True)
    assert adapter.current_mission().version == "mission-v2"
    tick[0] += timedelta(seconds=1)
    evidence(case, "later-history", "later-evidence", revision=2)
    assert adapter.current_mission().version == "mission-v2"
    assert (
        adapter.confirm(
            approvals[4], correlation_id="corr-c", counterevidence_checked=True
        ).status.value
        == "confirmed"
    )
    assert adapter.propose(**proposal).status.value == "confirmed"


def test_legacy_unique_origin_remains_readable_and_replayable(case):
    events, gate, tick, adapter, _, proposal, approvals = case
    adapter.propose(**proposal)
    tick[0] += timedelta(seconds=1)
    evidence(case, "legacy-independent", "shared-b")
    adapter.confirm(approvals[2], correlation_id="corr-b", counterevidence_checked=True)
    # Exact earlier adapter representation: source ID was the shared operation.
    # Neither its evidence, binding, timestamp nor confirmation changes.
    with events._open() as db:
        db.execute(
            "UPDATE shared_mission_document SET document=json_set(document,'$.proposals[0].proposal.initiating_source_id','shared-a')"
        )
    assert gate.reconcile()
    assert adapter.current_mission().version == "mission-v2"
    assert adapter.propose(**proposal).status.value == "confirmed"
    tick[0] += timedelta(seconds=1)
    adapter.propose(
        **(
            proposal
            | dict(
                operation_id="after-legacy",
                parent_version="mission-v2",
                proposed_version="mission-v3",
                correlation_id="later-correlation",
                purpose=MissionPurpose.DIALOGUE,
            )
        )
    )
    assert adapter.current_mission().version == "mission-v2"


def test_missing_ambiguous_or_forged_mission_source_binding_never_grants_authority(
    case,
):
    from app.behavior.mission import MissionEvidenceSource, EvidenceProvenance

    events, _, tick, adapter, _, proposal, _ = case
    adapter.propose(**proposal)
    sources = (
        MissionEvidenceSource(
            "shared-b", "shared-b", EvidenceProvenance.AGENT_OBSERVATION
        ),
    )
    with events._open() as db:
        assert not adapter._authority(db, "missing-origin", sources)
        db.execute(
            "UPDATE shared_mission_document SET document=json_set(document,'$.proposals[0].proposal.initiating_source_id','forged-origin')"
        )
        assert not adapter._authority(db, "forged-origin", sources)
        db.execute(
            "UPDATE shared_mission_document SET document=json_set(document,'$.proposals[0].proposal.initiating_source_id','shared-a')"
        )
    tick[0] += timedelta(seconds=1)
    adapter.propose(
        **(
            proposal
            | dict(
                operation_id="legacy-pending",
                proposed_version="mission-v3",
                correlation_id="second-correlation",
            )
        )
    )
    with events._open() as db:
        db.execute(
            "UPDATE shared_mission_document SET document=json_set(document,'$.proposals[1].proposal.initiating_source_id','shared-a')"
        )
        assert not adapter._authority(db, "shared-a", sources)
