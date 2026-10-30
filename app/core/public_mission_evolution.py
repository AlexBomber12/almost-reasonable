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
from app.core.free_mission import FREE_PURPOSES, free_context, public_prompt_version
from app.core.provider_spend import ProviderPersistenceError


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
            table_existed = (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='public_mission_state'"
                ).fetchone()
                is not None
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS public_mission_state("
                "id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL)"
            )
            row = db.execute(
                "SELECT document FROM public_mission_state WHERE id=1"
            ).fetchone()
            created_state = row is None
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
                existing_document = _document_from_dict(json.loads(row[0]), self.root)
                if not app.free_missions_enabled and any(
                    record.proposal.purpose.value in FREE_PURPOSES
                    for record in existing_document.proposals
                ):
                    raise ValueError(
                        "persisted free mission requires enabled configuration"
                    )
        try:
            app.mission.bind_evolution(
                app.journal.path, self.authorized, expected_root=self.root
            )
        except Exception:
            if created_state:
                with app.journal.transaction() as db:
                    if table_existed:
                        db.execute("DELETE FROM public_mission_state WHERE id=1")
                    else:
                        db.execute("DROP TABLE public_mission_state")
            raise

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

    def pending_in(self, db):
        document = _Repository(db, self.root).load()
        return next(
            (
                item
                for item in document.proposals
                if item.status is MissionStatus.PENDING
            ),
            None,
        )

    def proposal_capacity_in(self, db):
        document = _Repository(db, self.root).load()
        return len(document.proposals) < MAX_MISSION_HISTORY_RECORDS

    def _proposal_handoff_capacity_in(self, db, document):
        if len(document.proposals) >= MAX_MISSION_HISTORY_RECORDS:
            return False
        if self.app.free_missions_enabled:
            return True
        profile = self.app.current_research_profile()
        return profile is not None and bool(self.app._available_topics_in(db, profile))

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
        """Finish an uncommitted intent whose admitted authority is obsolete."""
        with self.app.journal.transaction() as db:
            row = db.execute(
                "SELECT phase FROM intents WHERE operation=?", (operation,)
            ).fetchone()
            if row is None or row[0] != "intent":
                return False
            if self._stale_in(db, payload):
                self._hold_in(db, operation)
                return True
            return False

    def _stale_in(self, db, payload, document=None):
        if document is None:
            document = _Repository(db, self.root).load()
        pending = next(
            (
                item
                for item in document.proposals
                if item.status is MissionStatus.PENDING
            ),
            None,
        )
        return (
            payload.get("mission_invariants") is None
            or payload["mission"]["version"] != document.current.version
            or payload.get("mission_pending_id")
            != (None if pending is None else pending.proposal.proposal_id)
            or not self.app.source_valid(payload)
            or self.app.ledger.control_generation() != payload["generation"]
            or self.invariants().to_dict() != payload["mission_invariants"]
            or (
                payload.get("mission_action") in ("confirm", "reject")
                and pending is not None
                and pending.proposal.invariants.to_dict()
                != payload["mission_invariants"]
            )
            or (
                payload.get("mission_action") == "propose"
                and not self._proposal_handoff_capacity_in(db, document)
            )
        )

    @staticmethod
    def _hold_in(db, operation):
        db.execute(
            "UPDATE intents SET phase='held' WHERE operation=? AND phase='intent'",
            (operation,),
        )

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
        if self.app.free_missions_enabled:
            return (
                (
                    free_context(None if record is None else record.proposal)
                    if record is not None
                    or len(document.proposals) < MAX_MISSION_HISTORY_RECORDS
                    else ()
                ),
                None if record is None else record.proposal.proposal_id,
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
            if source.provenance is EvidenceProvenance.AGENT_DELIBERATION:
                if not self._deliberation_authorized(source):
                    return False
            elif source.provenance is EvidenceProvenance.AGENT_OBSERVATION:
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
            if record.proposal.purpose.value in FREE_PURPOSES and record.status in (
                MissionStatus.PENDING,
                MissionStatus.CONFIRMED,
            ):
                with sqlite3.connect(self.app.journal.path) as db:
                    row = db.execute(
                        "SELECT phase,payload FROM intents WHERE operation=?",
                        (record.proposal.correlation_id,),
                    ).fetchone()
                origin = None if row is None else json.loads(row[1])
                if (
                    not self.app.free_missions_enabled
                    or row is None
                    or row[0] != "linked"
                    or origin.get("mission_free") is not True
                    or origin.get("mission_action") != "propose"
                    or not self.app.source_valid(origin)
                ):
                    raise ValueError("free mission proposal source withdrawn")
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
                        None
                        if app.research_client is None
                        else str(app.research_client._path),
                        None
                        if app.research_client is None
                        else app.research_client._expected_worker_uid,
                        app.ledger.control_generation(),
                    )
                    + (("free-mission-v1",) if app.free_missions_enabled else ())
                )
            ),
            True,
        )

    def apply(self, operation, payload):
        """Resolve a persisted intent once; SQLite serializes document CAS changes."""
        with self.app.service.store.conversation_control_fence():
            committed = self._apply(operation, payload)
        if not committed:
            raise ValueError("public mission controls changed")

    def _apply(self, operation, payload):
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
            if self._stale_in(db, payload, document):
                self._hold_in(db, operation)
                return False
            free = payload.get("mission_free", False)
            if type(free) is not bool or free != app.free_missions_enabled:
                raise ValueError("free mission mode changed")
            if action == "propose" and pending is not None:
                raise ValueError("public mission proposal already pending")
            if action in ("confirm", "reject", "abandon") and (
                pending is None or pending.proposal.decided_at >= at
            ):
                raise ValueError("independent later mission decision required")
            if action not in ("propose", "confirm", "reject", "abandon"):
                raise ValueError("invalid public mission action")
            invariants = self.invariants()
            if invariants.to_dict() != payload["mission_invariants"]:
                self._hold_in(db, operation)
                return False
            db.execute("SAVEPOINT mission_effect")
            if action == "propose":
                purpose = MissionPurpose(payload["mission_purpose"])
                attempt = app.provider.usage_attempt(operation) if free else None
                machine.propose(
                    MissionProposal(
                        _identity(operation, "mission-proposal"),
                        payload["free_mission_text"]
                        if free
                        else _MISSION_PURPOSE_TEXT[purpose],
                        document.current.version,
                        payload["event"],
                        tuple(payload["mission_reasons"])
                        if free
                        else ("public deliberation",),
                        tuple(payload["mission_counterevidence"])
                        if free
                        else ("none recorded",),
                        _identity(operation, "mission-version"),
                        at,
                        payload["correlation"],
                        attempt.model_identifier
                        if free
                        else "public-behavior-worker-v1",
                        attempt.prompt_version if free else "threads-public-prompt-v1",
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
                        tuple(payload["mission_reasons"])
                        if free
                        else ("public deliberation",),
                        tuple(payload["mission_counterevidence"])
                        if free
                        else ("none recorded",),
                        tuple(source.evidence_id for source in sources),
                        invariants,
                        sources,
                    )
                )
            app.guard()
            app.require_mission(payload)
            if (
                not app.source_valid(payload)
                or app.ledger.control_generation() != payload["generation"]
                or self.invariants() != invariants
            ):
                db.execute("ROLLBACK TO mission_effect")
                db.execute("RELEASE mission_effect")
                self._hold_in(db, operation)
                return False
            if app.operating_window is not None:
                app.operating_window.bind_transition(
                    db, operation, payload, repository.load().current
                )
            db.execute("RELEASE mission_effect")
            db.execute(
                "UPDATE intents SET phase='linked' WHERE operation=? AND phase='intent'",
                (operation,),
            )
            return True

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
        if payload.get("mission_free"):
            operation = payload["correlation"]
            attempt = self.app.provider.usage_attempt(operation)
            if (
                payload["kind"] != "wake"
                or attempt.started_at <= proposal.decided_at
                or attempt.started_at > datetime.fromisoformat(payload["decided_at"])
            ):
                raise ValueError("independent later autonomous deliberation required")
            sources = (
                MissionEvidenceSource(
                    "deliberation:" + operation,
                    self._deliberation_binding(payload),
                    EvidenceProvenance.AGENT_DELIBERATION,
                ),
            )
            if not self.authorized(proposal.initiating_source_id, sources):
                raise ValueError("free mission deliberation evidence invalid")
            return sources
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

    def _deliberation_authorized(self, source):
        if not self.app.free_missions_enabled or not source.evidence_id.startswith(
            "deliberation:"
        ):
            return False
        operation = source.evidence_id.removeprefix("deliberation:")
        with sqlite3.connect(self.app.journal.path) as db:
            row = db.execute(
                "SELECT phase,payload FROM intents WHERE operation=?", (operation,)
            ).fetchone()
            document = _Repository(db, self.root).load()
        if row is None or row[0] not in ("intent", "linked"):
            return False
        payload = json.loads(row[1])
        pending_proposal = next(
            (
                record.proposal
                for record in document.proposals
                if record.proposal.proposal_id == payload.get("mission_pending_id")
            ),
            None,
        )
        if (
            pending_proposal is None
            or payload.get("mission", {}).get("version")
            != pending_proposal.current_mission_version
            or payload.get("event") == pending_proposal.initiating_event_id
            or operation == pending_proposal.correlation_id
        ):
            return False
        if (
            payload.get("mission_free") is not True
            or payload.get("kind") != "wake"
            or payload.get("decision") != "revise_mission"
            or payload.get("mission_action") not in ("confirm", "reject")
            or payload.get("generation") != self.app.ledger.control_generation()
            or payload.get("correlation") != operation
            or source.source_id != self._deliberation_binding(payload)
            or not self.app.source_valid(payload)
            or not self.app.provider.attempt_exists(operation)
        ):
            return False
        try:
            attempt = self.app.provider.usage_attempt(operation)
        except ProviderPersistenceError:
            return False
        return (
            attempt.outcome == "completed"
            and attempt.started_at > pending_proposal.decided_at
            and attempt.correlation_id == operation
            and attempt.prompt_version
            == public_prompt_version(tuple(payload["mission_evolution"]), True)
            and attempt.started_at <= datetime.fromisoformat(payload["decided_at"])
        )

    def _deliberation_binding(self, payload):
        return self.source_id(
            json.dumps(
                {key: value for key, value in payload.items() if key != "phase"},
                sort_keys=True,
                ensure_ascii=False,
            ),
            payload["generation"],
        )

    def intellectual_context_operations_in(self, db):
        document = _Repository(db, self.root).load()
        operations = []
        for record in document.proposals:
            if (
                record.status is MissionStatus.PENDING
                or record.proposal.version_identity == document.current.version
            ):
                operations.append(record.proposal.correlation_id)
                if record.confirmation is not None:
                    operations.append(record.confirmation.correlation_id)
        return tuple(operations)
