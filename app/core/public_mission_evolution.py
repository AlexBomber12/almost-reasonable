"""Trusted, journal-backed adapter for optional public mission evolution."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import sqlite3

from app.behavior.mission import (
    CapabilityPolicyInvariants,
    ConfirmationOutcome,
    EvidenceProvenance,
    MissionAbandonment,
    MissionConfirmation,
    MissionDocument,
    MissionEvidenceSource,
    MissionProposal,
    MissionPurpose,
    MissionStateMachine,
    MissionStatus,
    MAX_MISSION_HISTORY_RECORDS,
    _MISSION_PURPOSE_TEXT,
    _document_from_dict,
    _document_to_dict,
)
from app.core.core_self import CORE_SELF_VERSION
from app.core.public_behavior import public_context_digest
from app.integration.threads_stage0 import _identity
from app.threads.domain import InboundKind


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(slots=True)
class _Repository:
    db: object
    root: object

    def load(self):
        raw = self.db.execute(
            "SELECT document FROM public_mission_state WHERE id=1"
        ).fetchone()
        if raw is None:
            raise ValueError("public mission state missing")
        return _document_from_dict(json.loads(raw[0]), self.root)

    def save(self, document):
        self.db.execute(
            "UPDATE public_mission_state SET document=? WHERE id=1",
            (json.dumps(_document_to_dict(document), ensure_ascii=False),),
        )


class PublicMissionEvolution:
    """One transition per durable public intent, with trusted source bindings."""

    def __init__(self, app):
        self.app = app
        self.root = app.mission.root_mission()
        if self.root.parent_version is not None:
            raise ValueError("public mission evolution requires a trusted root mission")
        with app.journal.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS public_mission_state("
                "id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL)"
            )
            row = db.execute(
                "SELECT document FROM public_mission_state WHERE id=1"
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO public_mission_state VALUES(1,?)",
                    (
                        json.dumps(
                            _document_to_dict(MissionDocument(self.root, (), None))
                        ),
                    ),
                )
            else:
                _document_from_dict(json.loads(row[0]), self.root)
        app.mission.bind_evolution(app.journal.path, self.authorized)

    def document(self):
        with self.app.journal.transaction() as db:
            document = _Repository(db, self.root).load()
        self._require_authorized(document)
        return document

    def pending(self):
        return next(
            (
                record
                for record in self.document().proposals
                if record.status is MissionStatus.PENDING
            ),
            None,
        )

    def pending_id_in(self, db):
        document = _Repository(db, self.root).load()
        record = next(
            (
                item
                for item in document.proposals
                if item.status is MissionStatus.PENDING
            ),
            None,
        )
        return None if record is None else record.proposal.proposal_id

    def offered_evidence_in(self, db, payload):
        operation = payload.get("mission_evidence_operation")
        if operation is None:
            return True
        row = db.execute(
            "SELECT r.profile_digest,r.evidence,i.payload FROM public_research_ops r "
            "JOIN public_research_completions c ON c.operation=r.operation "
            "JOIN intents i ON i.operation=r.operation "
            "WHERE r.operation=? AND r.phase='completed'",
            (operation,),
        ).fetchone()
        if row is None:
            return False
        research_payload = json.loads(row[2])
        return (
            row[0] == payload["offered_profile_digest"]
            and public_context_digest((row[1],)) == payload["mission_evidence_digest"]
            and research_payload["profile_digest"] == row[0]
            and research_payload["generation"] == payload["generation"]
            and self.app.source_valid(research_payload)
        )

    def hold_if_stale(self, operation, payload):
        """Finish an intent whose exact parent or pending proposal was replaced."""
        with self.app.journal.transaction() as db:
            document = _Repository(db, self.root).load()
            pending = next(
                (
                    item.proposal.proposal_id
                    for item in document.proposals
                    if item.status is MissionStatus.PENDING
                ),
                None,
            )
            stale = (
                payload["mission"]["version"] != document.current.version
                or payload.get("mission_pending_id") != pending
            )
            if stale:
                db.execute(
                    "UPDATE intents SET phase='held' WHERE operation=? AND phase='intent'",
                    (operation,),
                )
            return stale

    def model_context(self):
        return self.decision_context()[0]

    def decision_context(self):
        document = self.document()
        record = next(
            (
                item
                for item in document.proposals
                if item.status is MissionStatus.PENDING
            ),
            None,
        )
        if record is not None:
            return (
                "pending",
                record.proposal.purpose.value,
            ), record.proposal.proposal_id
        profile = self.app.current_research_profile()
        return (
            ("enabled",)
            if len(document.proposals) < MAX_MISSION_HISTORY_RECORDS
            and profile
            and self.app.available_topics(profile)
            else ()
        ), None

    def source_id(self, content, generation=None):
        if generation is None:
            generation = self.app.ledger.control_generation()
        return _digest(str(generation) + ":" + content)

    def authorized(self, initiating_source_id, sources):
        if not sources or all(
            source.source_id == initiating_source_id for source in sources
        ):
            return False
        for source in sources:
            if source.provenance is EvidenceProvenance.AGENT_OBSERVATION:
                with sqlite3.connect(self.app.journal.path) as db:
                    row = db.execute(
                        "SELECT r.phase,r.evidence,r.profile_digest,i.payload FROM public_research_ops r "
                        "JOIN intents i ON i.operation=r.operation "
                        "JOIN public_research_completions c ON c.operation=r.operation "
                        "WHERE r.operation=?",
                        (source.evidence_id,),
                    ).fetchone()
                research_payload = None if row is None else json.loads(row[3])
                if (
                    row is None
                    or row[0] != "completed"
                    or research_payload["generation"]
                    != self.app.ledger.control_generation()
                    or source.source_id
                    != self.source_id(row[1], research_payload["generation"])
                    or research_payload["profile_digest"] != row[2]
                    or not self.app.source_valid(research_payload)
                ):
                    return False
            else:
                return False
        return True

    def _require_authorized(self, document):
        for record in document.proposals:
            if record.confirmation is not None and not self.authorized(
                record.proposal.initiating_source_id,
                record.confirmation.evidence_sources,
            ):
                raise ValueError("public mission evidence withdrawn")

    def invariants(self):
        app = self.app
        policy = app.policy
        profile = app.current_research_profile()
        policy_fields = (
            policy.account_ref,
            policy.stage.value,
            policy.simulation_enabled,
            policy.account_authorized,
            tuple(sorted(policy.scopes)),
            policy.privacy_gate_available,
            policy.russian_output_gate_available,
            policy.publication_capability_enabled,
            policy.emergency_stop,
        )
        return CapabilityPolicyInvariants(
            CORE_SELF_VERSION,
            _digest(repr(policy_fields)),
            _digest(str(app.model.socket_path) + ":" + str(app.model.expected_uid)),
            _digest(str(app.provider.is_spend_suspended())),
            policy.stage.value,
            _digest(
                repr(
                    (
                        tuple(sorted(policy.scopes)),
                        policy.publication_capability_enabled,
                        app.research_enabled,
                        app.research_scope,
                        None if profile is None else profile.digest,
                        str(app.research_client._path),
                        app.research_client._expected_worker_uid,
                        app.ledger.control_generation(),
                    )
                )
            ),
            True,
        )

    def apply(self, operation, payload):
        """Resolve a persisted intent once; SQLite serializes document CAS changes."""
        app = self.app
        app.guard()
        app.require_mission(payload)
        if (
            not app.source_valid(payload)
            or app.ledger.control_generation() != payload["generation"]
        ):
            raise ValueError("public mission source changed")
        with app.journal.transaction() as db:
            row = db.execute(
                "SELECT phase FROM intents WHERE operation=?", (operation,)
            ).fetchone()
            if row is None or row[0] != "intent":
                raise ValueError("public mission intent is not pending")
            repository = _Repository(db, self.root)
            document = repository.load()
            self._require_authorized(document)
            machine = MissionStateMachine(repository, self.authorized)
            if payload["mission"]["version"] != document.current.version:
                raise ValueError("public mission parent changed")
            at = app.clock.now()
            action = payload["mission_action"]
            pending = next(
                (
                    item
                    for item in document.proposals
                    if item.status is MissionStatus.PENDING
                ),
                None,
            )
            if payload.get("mission_pending_id") != (
                None if pending is None else pending.proposal.proposal_id
            ):
                raise ValueError("public mission proposal changed")
            if action == "propose" and pending is not None:
                raise ValueError("public mission proposal already pending")
            if action in ("confirm", "reject", "abandon") and (
                pending is None or pending.proposal.decided_at >= at
            ):
                raise ValueError("independent later mission decision required")
            if action not in ("propose", "confirm", "reject", "abandon"):
                raise ValueError("invalid public mission action")
            invariants = self.invariants()
            if invariants.to_dict() != payload["mission_invariants"] or (
                action in ("confirm", "reject")
                and invariants != pending.proposal.invariants
            ):
                raise ValueError("public mission controls changed")
            if action == "propose":
                purpose = MissionPurpose(payload["mission_purpose"])
                machine.propose(
                    MissionProposal(
                        _identity(operation, "mission-proposal"),
                        _MISSION_PURPOSE_TEXT[purpose],
                        document.current.version,
                        payload["event"],
                        ("public deliberation",),
                        ("none recorded",),
                        _identity(operation, "mission-version"),
                        at,
                        payload["correlation"],
                        "public-behavior-worker-v1",
                        "threads-public-prompt-v1",
                        invariants,
                        purpose,
                        self._initiating_source(operation, payload),
                    )
                )
            elif action == "abandon":
                machine.abandon(
                    MissionAbandonment(
                        pending.proposal.proposal_id,
                        document.current.version,
                        payload["event"],
                        payload["correlation"],
                        at,
                        invariants,
                    )
                )
            else:
                sources = self._confirmation_sources(payload, pending.proposal)
                machine.confirm(
                    MissionConfirmation(
                        pending.proposal.proposal_id,
                        payload["event"],
                        payload["correlation"],
                        at,
                        ConfirmationOutcome.CONFIRMED
                        if action == "confirm"
                        else ConfirmationOutcome.REJECTED,
                        ("public deliberation",),
                        ("none recorded",),
                        tuple(source.evidence_id for source in sources),
                        invariants,
                        sources,
                    )
                )
            app.guard()
            if (
                app.ledger.control_generation() != payload["generation"]
                or self.invariants() != invariants
            ):
                raise ValueError("public mission source changed")
            db.execute(
                "UPDATE intents SET phase='linked' WHERE operation=? AND phase='intent'",
                (operation,),
            )

    def _initiating_source(self, operation, payload):
        if payload["kind"] == "wake":
            return operation
        event = self.app.service.store.inbound_event(
            self.app.account, InboundKind(payload["kind"]), payload["event"]
        )
        if event is None:
            raise ValueError("public mission source missing")
        return self.source_id(event.content, payload["generation"])

    def _confirmation_sources(self, payload, proposal):
        operation = payload.get("mission_evidence_operation")
        if operation is None:
            raise ValueError("independent public research evidence was not offered")
        with sqlite3.connect(self.app.journal.path) as db:
            row = db.execute(
                "SELECT r.operation,r.evidence,c.completed_at,i.payload FROM public_research_ops r "
                "JOIN public_research_completions c ON c.operation=r.operation "
                "JOIN intents i ON i.operation=r.operation "
                "WHERE r.phase='completed' "
                "AND r.operation=?",
                (operation,),
            ).fetchone()
        research_payload = None if row is None else json.loads(row[3])
        if (
            row is None
            or public_context_digest((row[1],)) != payload["mission_evidence_digest"]
            or research_payload["generation"] != payload["generation"]
            or research_payload.get("mission_invariants")
            != proposal.invariants.to_dict()
            or datetime.fromisoformat(row[2]) <= proposal.decided_at
            or datetime.fromisoformat(row[2])
            >= datetime.fromisoformat(payload["decided_at"])
        ):
            raise ValueError("independent later public research evidence missing")
        sources = (
            MissionEvidenceSource(
                row[0],
                self.source_id(row[1], research_payload["generation"]),
                EvidenceProvenance.AGENT_OBSERVATION,
            ),
        )
        if not self.authorized(proposal.initiating_source_id, sources):
            raise ValueError("independent public mission evidence invalid")
        return sources
