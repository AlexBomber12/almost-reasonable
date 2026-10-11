"""Application-owned Stage 0 adapter for the existing MissionStateMachine.

Mission documents use its unchanged closed serialization and validation. SQLite
owns the document and protected evidence bindings in one transaction; the Privacy
ledger remains an ordered, pending-first fence, never a claimed atomic store.
"""

from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime
import json

from app.core.shared_graph import validate_shared_graph
from app.core.store import EventStore
from app.privacy.application import PrivacyReadGate

from .mission import (
    CapabilityPolicyInvariants,
    ConfirmationOutcome,
    EvidenceProvenance,
    Mission,
    MissionConfirmation,
    MissionDocument,
    MissionEvidenceSource,
    MissionProposal,
    MissionPurpose,
    MissionStateMachine,
    _document_from_dict,
    _document_to_dict,
    _MISSION_PURPOSE_TEXT,
    _validate_stored_mission,
    _validate_invariants,
)


@dataclass(frozen=True, slots=True)
class SyntheticMissionConfirmationApproval:
    proposal_id: str
    shared_operation: str
    outcome: ConfirmationOutcome


class _Repository:
    def __init__(self, db, initial):
        self.db = db
        self.initial = initial

    def load(self):
        row = self.db.execute(
            "SELECT status, document FROM shared_mission_document WHERE singleton=1"
        ).fetchone()
        if row is None:
            return MissionDocument(self.initial, (), None)
        if row[0] != "ready":
            raise ValueError("mission evidence requires reassessment")
        return _document_from_dict(json.loads(row[1]), self.initial)

    def save(self, document):
        raw = json.dumps(
            _document_to_dict(document),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.db.execute(
            "INSERT INTO shared_mission_document VALUES (1, 'ready', ?, ?) ON CONFLICT(singleton) DO UPDATE SET current_version=excluded.current_version, document=excluded.document",
            (document.current.version, raw),
        )


class SharedMissionAdapter:
    def __init__(
        self,
        events: EventStore,
        gate: PrivacyReadGate,
        *,
        initial: Mission,
        invariants: CapabilityPolicyInvariants,
        clock,
        confirmations: tuple[SyntheticMissionConfirmationApproval, ...] = (),
    ):
        if (
            type(gate) is not PrivacyReadGate
            or gate._store is not events
            or type(confirmations) is not tuple
            or any(
                type(c) is not SyntheticMissionConfirmationApproval
                for c in confirmations
            )
        ):
            raise ValueError("trusted synthetic mission application required")
        _validate_stored_mission(initial)
        _validate_invariants(invariants)
        if (
            initial.parent_version is not None
            or invariants.deployment_stage != "simulation"
        ):
            raise ValueError(
                "synthetic root mission and simulation invariants required"
            )
        self._events, self._gate = events, gate
        self._initial, self._invariants, self._clock = initial, invariants, clock
        self._confirmations = confirmations

    @staticmethod
    def _active(db, operation):
        row = db.execute(
            "SELECT decided_at FROM shared_operations WHERE operation_id=? AND status='active' AND mission_evidence=1",
            (operation,),
        ).fetchone()
        if row is None:
            raise ValueError("mission shared evidence unavailable")
        return datetime.fromisoformat(row[0].replace("Z", "+00:00"))

    def _authority(self, db, original, evidence):
        self._active(db, original)
        original_events = {
            e[0]
            for e in db.execute(
                "SELECT event_id FROM protected_shared_sources WHERE operation_id=?",
                (original,),
            )
        }
        original_principals = {
            e[0]
            for e in db.execute(
                "SELECT source_principal FROM protected_shared_sources WHERE operation_id=?",
                (original,),
            )
        }
        document = _Repository(db, self._initial).load()
        proposed_at = max(
            (
                r.proposal.decided_at
                for r in document.proposals
                if r.proposal.initiating_source_id == original
            ),
            default=self._active(db, original),
        )
        for source in evidence:
            if (
                source.evidence_id != source.source_id
                or source.provenance is not EvidenceProvenance.AGENT_OBSERVATION
            ):
                return False
            later = self._active(db, source.evidence_id)
            new = db.execute(
                "SELECT s.event_id, s.source_principal, e.occurred_at FROM protected_shared_sources s JOIN event_envelopes e USING(event_id) WHERE s.operation_id=? AND s.role<>'inherited'",
                (source.evidence_id,),
            ).fetchall()
            if later <= proposed_at or any(
                e in original_events
                or p in original_principals
                or datetime.fromisoformat(occurred.replace("Z", "+00:00"))
                <= proposed_at
                for e, p, occurred in new
            ):
                return False
        return True

    def _run(self, callback):
        def run():
            with closing(self._events._open()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self._events._verify_current_schema(db)
                validate_shared_graph(db)
                repository = _Repository(db, self._initial)
                document = repository.load()
                if (
                    document.invariants is not None
                    and document.invariants != self._invariants
                ):
                    raise ValueError("capability-policy invariants changed")
                machine = MissionStateMachine(
                    repository,
                    lambda original, evidence: self._authority(db, original, evidence),
                )
                result = callback(db, repository, machine)
                validate_shared_graph(db)
                return result

        return self._gate.authorized(run)

    def current_mission(self):
        return self._run(lambda _db, _repository, machine: machine.current_mission())

    def no_change(self):
        return "no_change", self.current_mission().version

    def propose(
        self,
        *,
        operation_id,
        shared_operation,
        parent_version,
        proposed_version,
        correlation_id,
        purpose: MissionPurpose,
        counterevidence_checked: bool,
    ):
        if counterevidence_checked is not True or type(purpose) is not MissionPurpose:
            raise ValueError("mission counterevidence check required")

        def propose(db, repository, machine):
            self._active(db, shared_operation)
            current = db.execute(
                "SELECT MAX(revision) FROM shared_operations WHERE item_id=(SELECT item_id FROM shared_operations WHERE operation_id=?) AND status='active'",
                (shared_operation,),
            ).fetchone()[0]
            revision = db.execute(
                "SELECT revision FROM shared_operations WHERE operation_id=?",
                (shared_operation,),
            ).fetchone()[0]
            if current != revision:
                raise ValueError("mission requires current shared revision")
            now = self._clock()
            if now < self._active(db, shared_operation):
                raise ValueError("mission proposal predates shared evidence")
            proposal = MissionProposal(
                operation_id,
                _MISSION_PURPOSE_TEXT[purpose],
                parent_version,
                shared_operation,
                ("Compare current abstract intellectual evidence.",),
                ("Competing explanations remain plausible.",),
                proposed_version,
                now,
                correlation_id,
                "synthetic-model",
                "shared-mission-stage0-v1",
                self._invariants,
                purpose,
                shared_operation,
            )
            existing = [
                r
                for r in repository.load().proposals
                if r.proposal.proposal_id == operation_id
            ]
            if existing:
                if replace(existing[0].proposal, decided_at=now) != proposal:
                    raise ValueError("mission operation identity conflict")
                return existing[0]
            result = machine.propose(proposal)
            db.execute(
                "INSERT INTO protected_mission_shared VALUES (?, 'proposal', ?, 1)",
                (operation_id, shared_operation),
            )
            return result

        return self._run(propose)

    def confirm(self, approval, *, correlation_id, counterevidence_checked: bool):
        if (
            type(approval) is not SyntheticMissionConfirmationApproval
            or approval not in self._confirmations
            or counterevidence_checked is not True
        ):
            raise ValueError("independent mission application approval required")

        def confirm(db, repository, machine):
            self._active(db, approval.shared_operation)
            confirmation = MissionConfirmation(
                approval.proposal_id,
                approval.shared_operation,
                correlation_id,
                self._clock(),
                approval.outcome,
                ("Independent abstract evidence reviewed.",),
                ("Competing explanations remain plausible.",),
                (approval.shared_operation,),
                self._invariants,
                (
                    MissionEvidenceSource(
                        approval.shared_operation,
                        approval.shared_operation,
                        EvidenceProvenance.AGENT_OBSERVATION,
                    ),
                ),
            )
            existing = [
                r
                for r in repository.load().proposals
                if r.proposal.proposal_id == approval.proposal_id
            ]
            if existing and existing[0].confirmation is not None:
                if (
                    replace(
                        existing[0].confirmation, decided_at=confirmation.decided_at
                    )
                    != confirmation
                ):
                    raise ValueError("mission confirmation identity conflict")
                return existing[0]
            result = machine.confirm(confirmation)
            db.execute(
                "INSERT INTO protected_mission_shared VALUES (?, 'confirmation', ?, 1)",
                (approval.proposal_id, approval.shared_operation),
            )
            return result

        return self._run(confirm)
