"""Explicitly enabled, offline Stage 0 composition; no production entrypoint.

The journal owns handoff identities, not Threads transitions. Started inference
is never retried. Only the reviewed literal catalog can cross the public gate.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3

from app.behavior.deliberation import (
    BehaviorCapabilities,
    BehaviorIntent,
    DeliberationBudget,
    DeliberationEngine,
    DeliberationInput,
    DeliberationOutcome,
    PriorResultCategory,
    RecordedBehaviorModel,
    TriggerType,
)
from app.behavior.durable_scheduler import DurableScheduler, SQLiteSchedulerRepository
from app.behavior.russian_gate import RussianOutputPolicy
from app.behavior.mission import (
    CapabilityPolicyInvariants,
    Mission,
    MissionPurpose,
    _MISSION_PURPOSE_TEXT,
)
from app.behavior.shared_mission import SharedMissionAdapter
from app.behavior.scheduler import SchedulerOutcome
from app.core.agent_proposals import (
    AgentDecision,
    NoActionProposal,
    PublicationProposal,
    ReplyProposal,
)
from app.core.core_self import initial_core_self
from app.core.store import EventStore
from app.core.provider_spend import ProviderStateStore
from app.owner_control.store import OwnerStore
from app.privacy.ledger import PrivacyLedger
from app.privacy.application import PrivacyReadBlocked, PrivacyReadGate
from app.threads.domain import (
    AccountPolicy,
    ApprovalPurpose,
    ConversationOutcome,
    InboundKind,
    OutboxState,
    PublicationAuthorizationSnapshot,
    PublicationType,
    SourceProvenance,
    Stage,
    issue_approval,
    require_reference,
    validate_content,
)
from app.threads.service import PolicyDeniedError, ThreadsService
from app.threads.simulator import ThreadsSimulator
from app.threads.store import ThreadsStore, ThreadsStoreError


PUBLIC_DRAFTS = (
    "Мне интересно сравнивать разные объяснения и проверять основания выводов.",
    "Спасибо за вопрос. Мне кажется, полезно сначала уточнить основания вывода.",
)
MAX_INTENTS = 4096
_REVIEWED = {f"public-{i}": content for i, content in enumerate(PUBLIC_DRAFTS)}
STAGE0_MISSION_INVARIANTS = CapabilityPolicyInvariants(
    initial_core_self().version,
    "policy-v1",
    "credentials-v1",
    "budget-v1",
    "simulation",
    "tools-v1",
    True,
)


class VerifiedMissionContext:
    """Short fenced reads of the closed mission; never a model authority source."""

    def __init__(self, events, gate, *, initial, invariants, clock):
        if type(events) is not EventStore or type(gate) is not PrivacyReadGate:
            raise ValueError("configured mission stores required")
        if (
            type(initial.purpose) is not MissionPurpose
            or initial.text != _MISSION_PURPOSE_TEXT[initial.purpose]
        ):
            raise ValueError("initial mission must be reviewed closed content")
        self.events, self.gate = events, gate
        self.adapter = SharedMissionAdapter(
            events, gate, initial=initial, invariants=invariants, clock=clock
        )

    def snapshot(self):
        try:
            mission = self.adapter.verified_snapshot()
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OSError,
            sqlite3.Error,
            PrivacyReadBlocked,
        ) as error:
            raise ValueError("verified mission unavailable") from error
        binding = hashlib.sha256(
            json.dumps(
                [
                    mission.version,
                    mission.text,
                    mission.activated_at.isoformat(),
                    mission.parent_version,
                    mission.purpose.value,
                ],
                ensure_ascii=True,
            ).encode()
        ).hexdigest()
        return mission.text, {"version": mission.version, "digest": binding}

    def require(self, binding):
        if type(binding) is not dict or set(binding) != {"version", "digest"}:
            raise ValueError("mission binding missing")
        _, current = self.snapshot()
        if current != binding:
            raise ValueError("verified mission changed")


def _identity(*parts):
    return (
        "stage0-"
        + hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode()).hexdigest()
    )


def _binding(item):
    return json.dumps(
        [
            item.account_ref,
            item.operation_id,
            item.publication_type,
            item.parent_message_id,
            item.draft_id,
            item.content,
            item.source_provenance,
            item.privacy_authorized,
            item.russian_output_authorized,
            item.correlation_id,
        ]
    )


class Stage0Journal:
    """Separate versioned WAL journal, bounded without forgetting effect identity."""

    def __init__(self, path, account, *, mission_required=False):
        self.path = Path(path)
        require_reference("account", account)
        with self.transaction() as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if tables and "stage0_schema" not in tables:
                raise ValueError("foreign Stage 0 database")
            db.execute(
                "CREATE TABLE IF NOT EXISTS stage0_schema(version INTEGER, account TEXT, mission_required INTEGER NOT NULL DEFAULT 0, mission_initialized INTEGER NOT NULL DEFAULT 0)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(stage0_schema)")}
            if "mission_required" not in columns:
                db.execute(
                    "ALTER TABLE stage0_schema ADD COLUMN mission_required INTEGER NOT NULL DEFAULT 0"
                )
            if "mission_initialized" not in columns:
                db.execute(
                    "ALTER TABLE stage0_schema ADD COLUMN mission_initialized INTEGER NOT NULL DEFAULT 0"
                )
            rows = db.execute(
                "SELECT version, account, mission_required, mission_initialized FROM stage0_schema"
            ).fetchall()
            self.needs_mission_initialization = mission_required and (
                not rows or rows[0][3] == 0
            )
            if rows and (
                len(rows) != 1
                or rows[0][0] not in (1, 2)
                or rows[0][1] != account
                or rows[0][0] == 2
                and bool(rows[0][2]) != mission_required
            ):
                raise ValueError("unsupported Stage 0 schema or account")
            if not rows:
                db.execute(
                    "INSERT INTO stage0_schema VALUES(2,?,?,0)",
                    (account, int(mission_required)),
                )
            elif rows[0][0] == 1:
                # Legacy unfinished intents keep their old payload: no invented binding.
                db.execute(
                    "UPDATE stage0_schema SET version=2, mission_required=?",
                    (int(mission_required),),
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS intents(operation TEXT PRIMARY KEY, phase TEXT NOT NULL CHECK(phase IN ('started','intent','linked','rejected','held')), payload TEXT NOT NULL CHECK(length(payload)<=4096))"
            )
            if rows and rows[0][0] == 1:
                db.execute("ALTER TABLE intents RENAME TO old_intents")
                db.execute(
                    "CREATE TABLE intents(operation TEXT PRIMARY KEY, phase TEXT NOT NULL CHECK(phase IN ('started','intent','linked','rejected','held')), payload TEXT NOT NULL CHECK(length(payload)<=4096))"
                )
                db.execute("INSERT INTO intents SELECT * FROM old_intents")
                db.execute("DROP TABLE old_intents")
            db.execute(
                "CREATE TABLE IF NOT EXISTS sources(draft TEXT PRIMARY KEY, active INTEGER NOT NULL CHECK(active IN (0,1)))"
            )
            if not rows:
                db.executemany(
                    "INSERT INTO sources VALUES(?,1)",
                    ((f"public-{i}",) for i in range(len(PUBLIC_DRAFTS))),
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS approvals(reference TEXT PRIMARY KEY, operation TEXT NOT NULL, binding TEXT NOT NULL, expires TEXT NOT NULL, revoked INTEGER NOT NULL, purpose TEXT NOT NULL, actor TEXT NOT NULL)"
            )

    def mark_mission_initialized(self):
        with self.transaction() as db:
            db.execute(
                "UPDATE stage0_schema SET mission_initialized=1 WHERE mission_required=1"
            )

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @contextmanager
    def handoff_lock(self, operation):
        # Single-host SQLite deployment, matching the existing outbox liveness model.
        with open(str(self.path) + "." + operation + ".lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def start(self, operation, origin):
        with self.transaction() as db:
            if db.execute(
                "SELECT 1 FROM intents WHERE operation=?", (operation,)
            ).fetchone():
                return False
            if db.execute("SELECT count(*) FROM intents").fetchone()[0] >= MAX_INTENTS:
                raise ValueError("Stage 0 journal capacity reached")
            db.execute(
                "INSERT INTO intents VALUES(?,'started',?)",
                (operation, json.dumps(origin, ensure_ascii=False)),
            )
            return True

    def finish(self, operation, payload, *, rejected=False, held=False):
        with self.transaction() as db:
            db.execute(
                "UPDATE intents SET phase=?, payload=? WHERE operation=? AND phase='started'",
                (
                    "held" if held else "rejected" if rejected else "intent",
                    json.dumps(payload, ensure_ascii=False),
                    operation,
                ),
            )

    def hold_intent(self, operation):
        with self.transaction() as db:
            db.execute(
                "UPDATE intents SET phase='held' WHERE operation=? AND phase='intent'",
                (operation,),
            )

    def linked(self, operation):
        with self.transaction() as db:
            db.execute(
                "UPDATE intents SET phase='linked' WHERE operation=? AND phase='intent'",
                (operation,),
            )

    def read(self, operation):
        with self.transaction() as db:
            row = db.execute(
                "SELECT phase, payload FROM intents WHERE operation=?", (operation,)
            ).fetchone()
        if row is None:
            raise ValueError("Stage 0 intent not found")
        return {**json.loads(row["payload"]), "phase": row["phase"]}

    def recoverable(self):
        with self.transaction() as db:
            return tuple(
                row[0]
                for row in db.execute(
                    "SELECT operation FROM intents WHERE phase='intent' ORDER BY rowid"
                )
            )

    def claimed_events(self, kind, resumed):
        with self.transaction() as db:
            values = [
                json.loads(row[0]) for row in db.execute("SELECT payload FROM intents")
            ]
        return tuple(
            value["event"]
            for value in values
            if value["kind"] == kind and value.get("resumed", False) == resumed
        )

    def revoke_source(self, draft):
        with self.transaction() as db:
            db.execute("UPDATE sources SET active=0 WHERE draft=?", (draft,))

    def revoke_approval(self, reference):
        with self.transaction() as db:
            db.execute("UPDATE approvals SET revoked=1 WHERE reference=?", (reference,))


@dataclass(frozen=True, slots=True)
class SyntheticOperator:
    """Constructor-only local fixture capability, never parsed from public input."""

    actor: str
    enabled: bool = False

    def __post_init__(self):
        require_reference("synthetic actor", self.actor)
        if type(self.enabled) is not bool:
            raise ValueError("invalid synthetic fixture")


class Stage0Authorization:
    """CurrentAuthorizationProvider for literal synthetic public sources only.

    No private consent is issued here. A changed ledger generation invalidates
    old intents conservatively; unavailable control planes fail closed.
    """

    def __init__(self, application):
        self.app = application

    def current_approval(self, *, account_ref, operation_id, approval_reference, now):
        self.app.guard()
        if account_ref != self.app.account:
            return None
        with self.app.journal.transaction() as db:
            row = db.execute(
                "SELECT * FROM approvals WHERE reference=? AND operation=?",
                (approval_reference, operation_id),
            ).fetchone()
        if (
            row is None
            or row["actor"] != self.app.operator.actor
            or not self.app.operator.enabled
        ):
            return None
        return issue_approval(
            reference=approval_reference,
            account_ref=account_ref,
            operation_id=operation_id,
            expires_at=datetime.fromisoformat(row["expires"]),
            revoked=bool(row["revoked"]),
            purpose=ApprovalPurpose(row["purpose"]),
        )

    def current_publication_authorization(self, *, item, approval_reference, now):
        self.app.guard()
        with self.app.journal.transaction() as db:
            # Recheck after acquiring the approval/source writer fence. Revocation
            # cannot commit during this snapshot; other stores are fresh reads,
            # not a claimed distributed transaction. No effect occurs here.
            self.app.guard()
            generation = self.app.ledger.control_generation()
            intent = db.execute(
                "SELECT payload, phase FROM intents WHERE operation=?",
                (item.operation_id,),
            ).fetchone()
            payload = json.loads(intent[0])
            self.app.require_mission(payload)
            source_valid = (
                self.app.source_valid(payload)
                and self.app.ledger.control_generation() == generation
            )
            self.app.guard()
            approval = db.execute(
                "SELECT * FROM approvals WHERE reference=? AND operation=?",
                (approval_reference, item.operation_id),
            ).fetchone()
            source = db.execute(
                "SELECT active FROM sources WHERE draft=?", (payload.get("draft"),)
            ).fetchone()
            authorized = (
                item.account_ref == self.app.account
                and approval is not None
                and approval["binding"] == _binding(item)
                and approval["actor"] == self.app.operator.actor
                and self.app.operator.enabled
                and intent["phase"] == "linked"
                and payload.get("draft") == item.draft_id
                and payload.get("content") == item.content
                and _REVIEWED.get(item.draft_id) == item.content
                and item.source_provenance is SourceProvenance.PUBLIC
                and generation == payload.get("generation")
                and source_valid
                and source is not None
                and source[0] == 1
            )
            current = (
                None
                if approval is None
                else issue_approval(
                    reference=approval_reference,
                    account_ref=self.app.account,
                    operation_id=item.operation_id,
                    expires_at=datetime.fromisoformat(approval["expires"]),
                    revoked=bool(approval["revoked"]),
                    purpose=ApprovalPurpose(approval["purpose"]),
                )
            )
        # No database transaction spans a model or simulated provider call.
        return PublicationAuthorizationSnapshot(current, authorized)


class ThreadsStage0:
    """Application-owned account/route policy: no invented private principal.

    ActionPolicy's TrustedActionContext is for authenticated private routes and
    is inapplicable here. This coordinator validates its own exact public route.
    """

    def __init__(
        self,
        journal_path,
        scheduler_path,
        threads_path,
        *,
        model,
        simulator,
        policy,
        owner,
        provider,
        ledger,
        clock,
        wakeup_policy,
        operator,
        mission_events=None,
        mission_gate=None,
        initial_mission=None,
        mission_invariants=None,
        enabled=False,
        checkpoint=lambda _point: None,
    ):
        if (
            type(model) is not RecordedBehaviorModel
            or type(simulator) is not ThreadsSimulator
            or type(owner) is not OwnerStore
            or type(provider) is not ProviderStateStore
            or type(ledger) is not PrivacyLedger
            or type(operator) is not SyntheticOperator
            or type(policy) is not AccountPolicy
        ):
            raise ValueError("exact synthetic components required")
        if type(enabled) is not bool:
            raise ValueError("invalid Stage 0 enablement")
        self.enabled, self.account = enabled, policy.account_ref
        self.model, self.simulator, self.policy = model, simulator, policy
        self.owner, self.provider, self.ledger = owner, provider, ledger
        self.clock, self.operator, self.checkpoint = clock, operator, checkpoint
        mission_parts = (
            mission_events,
            mission_gate,
            initial_mission,
            mission_invariants,
        )
        if any(part is not None for part in mission_parts):
            if (
                not all(part is not None for part in mission_parts)
                or type(mission_events) is not EventStore
                or type(mission_gate) is not PrivacyReadGate
                or type(initial_mission) is not Mission
                or type(mission_invariants) is not CapabilityPolicyInvariants
                or mission_invariants != STAGE0_MISSION_INVARIANTS
                or mission_events is not provider._event_store
                or mission_gate._store is not mission_events
                or mission_gate._ledger is not ledger
            ):
                raise ValueError("mission stores and controls must match application")
            self.mission = VerifiedMissionContext(
                mission_events,
                mission_gate,
                initial=initial_mission,
                invariants=mission_invariants,
                clock=clock.now,
            )
        else:
            self.mission = None
        self.journal = Stage0Journal(
            journal_path, self.account, mission_required=self.mission is not None
        )
        if self.mission is not None and self.journal.needs_mission_initialization:
            self.mission.adapter.initialize_document()
            self.journal.mark_mission_initialized()
        self.engine = DeliberationEngine(model, clock.now)
        self.authorization = Stage0Authorization(self)
        self.service = ThreadsService(
            ThreadsStore(threads_path),
            simulator,
            policy,
            utc_now=clock.now,
            authorization_provider=self.authorization,
            poll_guard=self.guard,
        )
        self.scheduler = DurableScheduler(
            wakeup_policy,
            SQLiteSchedulerRepository(Path(scheduler_path)),
            clock,
            self._wake_deliberate,
            owner=owner,
            provider=provider,
        )

    def guard(self):
        if (
            not self.enabled
            or self.policy.stage is not Stage.SIMULATION
            or type(self.simulator) is not ThreadsSimulator
            or self.simulator.account_ref != self.account
            or self.policy.account_ref != self.account
            or self.service.simulator is not self.simulator
            or self.service.policy is not self.policy
            or self.service.authorization_provider is not self.authorization
            or type(self.model) is not RecordedBehaviorModel
            or self.engine._model is not self.model
            or self.owner.active() is not False
            or self.provider.is_spend_suspended() is not False
            or not self.policy.simulation_enabled
            or not self.policy.account_authorized
        ):
            raise PolicyDeniedError("Stage 0 controls deny operation")

    def wakeup_operation(self, event_id):
        require_reference("synthetic wake-up", event_id)
        return _identity(self.account, "wake", event_id)

    def _wake_deliberate(self, event, timeout):
        operation = self.wakeup_operation(event.event_id)
        self._decide(
            operation,
            {"kind": "wake", "event": event.event_id, "correlation": operation},
            (),
            timeout,
        )
        payload = self.journal.read(operation)
        if payload["phase"] in ("started", "rejected", "held"):
            raise ValueError("deliberation unavailable or rejected")
        try:
            self.require_mission(payload)
        except ValueError:
            self.journal.hold_intent(operation)
            raise
        return AgentDecision(payload["decision"])

    def run(self, pending=()):
        self.guard()
        result = self.scheduler.run(pending)
        self.checkpoint("after_scheduler")
        self.recover()
        return result

    def _decide(self, operation, origin, context, timeout=5):
        self.guard()
        self.checkpoint("before_deliberation")
        try:
            existing = self.journal.read(operation)
        except ValueError:
            existing = None
        if existing is not None:
            self.require_mission(existing)
            return
        mission_text = ()
        if self.mission is not None:
            try:
                text, binding = self.mission.snapshot()
            except ValueError:
                if self.journal.start(operation, origin):
                    self.journal.finish(operation, origin, held=True)
                else:
                    self.require_mission(self.journal.read(operation))
                return
            mission_text = (text,)
            origin = {**origin, "mission": binding}
        if not self.journal.start(operation, origin):
            self.require_mission(self.journal.read(operation))
            return
        self.checkpoint("after_started")
        self.guard()
        now = self.clock.now()
        generation = self.ledger.control_generation()
        self.guard()
        result = self.engine.deliberate(
            DeliberationInput(
                origin["correlation"],
                context,
                initial_core_self(),
                mission_text,
                (),
                BehaviorCapabilities(
                    "threads-stage0-v1",
                    False,
                    False,
                    not bool(self.policy.publication_blockers(PublicationType.REPLY)),
                    False,
                    not bool(self.policy.publication_blockers(PublicationType.POST)),
                ),
                TriggerType.AUTONOMOUS_WAKEUP
                if origin["kind"] == "wake"
                else TriggerType.PUBLIC_MESSAGE,
                DeliberationBudget(1, 1, timeout),
                now,
                now + timedelta(seconds=timeout),
                PriorResultCategory.NONE,
                origin["kind"] != "wake",
            )
        )
        self.checkpoint("after_deliberation")
        payload = {
            **origin,
            "generation": generation,
            "decided_at": now.isoformat(),
            "result": result.outcome.value,
        }
        try:
            self.require_mission(payload)
        except ValueError:
            self.journal.finish(operation, payload, held=True)
            return
        proposal = result.proposal
        rejected = result.outcome not in (
            DeliberationOutcome.PROPOSED,
            DeliberationOutcome.NO_ACTION,
        )
        if type(proposal) is NoActionProposal:
            payload.update(decision="no_action", reason=proposal.reason_category.value)
        elif type(proposal) in (PublicationProposal, ReplyProposal):
            content = (
                proposal.draft
                if type(proposal) is PublicationProposal
                else proposal.content
            )
            delivery = RussianOutputPolicy(max_regenerations=0).deliver(content)
            route_ok = (origin["kind"] == "wake") == (
                type(proposal) is PublicationProposal
            )
            if delivery.used_fallback or content not in PUBLIC_DRAFTS or not route_ok:
                rejected = True
            else:
                validate_content(content)
                payload.update(
                    decision=proposal.decision.value,
                    content=content,
                    draft=f"public-{PUBLIC_DRAFTS.index(content)}",
                )
        elif type(proposal) is BehaviorIntent and origin["kind"] != "wake":
            parameters = dict(proposal.parameters)
            if (
                proposal.decision is AgentDecision.DEFER
                and parameters["resume_condition"] == "new_public_evidence"
                and not origin.get("resumed")
            ):
                payload.update(decision="defer", condition="new_public_evidence")
            elif (
                proposal.decision is AgentDecision.TERMINATE_CONVERSATION
                and parameters["scope"] == "conversation"
            ):
                payload.update(
                    decision="terminate_conversation",
                    cooldown=parameters["cooldown_seconds"],
                )
            else:
                rejected = True
        else:
            rejected = True
        self.guard()
        self.journal.finish(operation, payload, rejected=rejected)
        self.checkpoint("after_intent")

    def source_valid(self, payload):
        if payload["kind"] == "wake":
            return True
        event = self.service.store.inbound_event(
            self.account, InboundKind(payload["kind"]), payload["event"]
        )

        if event is None or self._origin(
            event, resumed=payload.get("resumed", False)
        ) != {
            key: payload[key]
            for key in self._origin(event, resumed=payload.get("resumed", False))
        }:
            return False
        # A source may remain valid while its public conversation becomes
        # suppressed. Reply permission is rechecked at handoff and every effect.
        return (
            payload.get("decision") != "reply"
            or self.service.store.conversation_control(
                self.account, event.kind, event.conversation_id, now=self.clock.now()
            )
            is None
        )

    def require_mission(self, payload):
        if self.mission is not None:
            self.mission.require(payload.get("mission"))

    def _origin(self, event, *, resumed=False):
        return {
            "kind": event.kind.value,
            "event": event.event_id,
            "source": event.source_ref,
            "occurred_at": event.occurred_at.isoformat(),
            "conversation": event.conversation_id,
            "parent": event.parent_message_id,
            "correlation": event.correlation_id,
            "resumed": resumed,
        }

    def recover(self):
        self.guard()
        blocked = []
        for operation in self.journal.recoverable():
            payload = self.journal.read(operation)
            if payload["kind"] == "wake" and not any(
                run.selected_event_id == payload["event"]
                and run.outcome
                in (SchedulerOutcome.PROPOSED, SchedulerOutcome.NO_ACTION)
                and run.decision == AgentDecision(payload["decision"])
                for run in self.scheduler.repository.outcomes()
            ):
                blocked.append(operation)
                continue  # Intent held until durable scheduler acceptance; never infer success.
            try:
                self._materialize(operation, payload)
            except (PolicyDeniedError, ThreadsStoreError):
                blocked.append(operation)
        return tuple(blocked)

    def _materialize(self, operation, payload):
        with self.journal.handoff_lock(operation):
            payload = self.journal.read(operation)
            if payload["phase"] != "intent":
                return
            self._handoff(operation, payload)

    def _handoff(self, operation, payload):
        self.guard()
        try:
            self.require_mission(payload)
        except ValueError as error:
            raise PolicyDeniedError("Stage 0 mission changed") from error
        if not self.source_valid(payload):
            raise PolicyDeniedError("Stage 0 source authorization changed")
        decision = AgentDecision(payload["decision"])
        if decision in (AgentDecision.PROPOSE_PUBLICATION, AgentDecision.REPLY):
            publication_type = (
                PublicationType.POST
                if payload["kind"] == "wake"
                else PublicationType.REPLY
            )
            if (
                self.policy.publication_blockers(publication_type)
                or _REVIEWED.get(payload["draft"]) != payload["content"]
                or self.ledger.control_generation() != payload["generation"]
            ):
                raise PolicyDeniedError("Stage 0 publication authorization changed")
            with self.journal.transaction() as db:
                source = db.execute(
                    "SELECT active FROM sources WHERE draft=?", (payload["draft"],)
                ).fetchone()
            if source is None or source[0] != 1:
                raise PolicyDeniedError("Stage 0 public source unavailable")
            item, _ = self.service.propose(
                operation_id=operation,
                publication_type=PublicationType.POST
                if payload["kind"] == "wake"
                else PublicationType.REPLY,
                parent_message_id=None
                if payload["kind"] == "wake"
                else payload["event"],
                draft_id=payload["draft"],
                content=payload["content"],
                source_provenance=SourceProvenance.PUBLIC,
                privacy_authorized=True,
                russian_output_authorized=True,
                correlation_id=payload["correlation"],
                now=self.clock.now(),
            )
            self.checkpoint("after_outbox")
            if item.state is OutboxState.PROPOSED:
                self.service.submit_for_approval(operation, now=self.clock.now())
            if payload["kind"] != "wake":
                event = self.service.store.inbound_event(
                    self.account, InboundKind(payload["kind"]), payload["event"]
                )
                if event is None:
                    raise PolicyDeniedError("Stage 0 reply source unavailable")
                self.service.store.acknowledge_reply_proposal(
                    operation, event, now=self.clock.now()
                )
                self.checkpoint("after_reply_ack")
        elif payload["kind"] != "wake":
            outcome = ConversationOutcome(decision.value)
            cooldown = payload.get("cooldown", 0)
            if cooldown:
                outcome = ConversationOutcome.COOLDOWN
            if not self.service.store.conversation_outcome_recorded(operation):
                self.service.store.record_conversation_outcome(
                    outcome_id=operation,
                    correlation_id=payload["correlation"],
                    account_ref=self.account,
                    stream=InboundKind(payload["kind"]),
                    inbound_event_id=payload["event"],
                    outcome=outcome,
                    reason=payload.get("reason", "behavior_decision"),
                    resume_condition=payload.get("condition"),
                    now=datetime.fromisoformat(payload["decided_at"]),
                    control_expires_at=None
                    if not cooldown
                    else datetime.fromisoformat(payload["decided_at"])
                    + timedelta(seconds=cooldown),
                )
                self.checkpoint("after_outcome")
        self.journal.linked(operation)

    def poll(self, stream, *, correlation_id, **kwargs):
        self.guard()
        return self.service.poll(
            stream, now=self.clock.now(), correlation_id=correlation_id, **kwargs
        )

    def process_inbound(self, stream, *, resume=False, limit=100):
        self.guard()
        self.recover()
        events = (
            tuple(
                item.event
                for item in self.service.store.deferred_inbound_events(
                    self.account,
                    stream,
                    limit=limit,
                    now=self.clock.now(),
                    excluded_event_ids=self.journal.claimed_events(
                        stream.value, resume
                    ),
                )
            )
            if resume
            else self.service.store.pending_inbound_events(
                self.account,
                stream,
                limit=limit,
                now=self.clock.now(),
                excluded_event_ids=self.journal.claimed_events(stream.value, resume),
            )
        )
        operations = []
        for event in events:
            control = self.service.store.conversation_control(
                self.account, stream, event.conversation_id, now=self.clock.now()
            )
            if control is not None or event.source_ref != self.simulator.source_ref:
                continue
            operation = _identity(
                self.account,
                stream.value,
                event.event_id,
                "resume" if resume else "initial",
            )
            try:
                self._decide(
                    operation, self._origin(event, resumed=resume), (event.content,)
                )
            except ValueError:
                continue  # A stale claimed intent cannot starve another conversation.
            payload = self.journal.read(operation)
            if payload["phase"] == "intent":
                try:
                    self._materialize(operation, payload)
                except (PolicyDeniedError, ThreadsStoreError):
                    continue  # Hold this intent; independent conversation work remains eligible.
                operations.append(operation)
        return tuple(operations)

    def approve(
        self, operation, operator, *, expires_at, purpose=ApprovalPurpose.PUBLICATION
    ):
        self.guard()
        if (
            type(operator) is not SyntheticOperator
            or operator is not self.operator
            or not operator.enabled
        ):
            raise PolicyDeniedError("configured synthetic operator fixture required")
        item = self.service.store.get(operation)
        if (
            item.account_ref != self.account
            or self.journal.read(operation)["phase"] != "linked"
        ):
            raise PolicyDeniedError("unlinked synthetic publication")
        if purpose is ApprovalPurpose.PUBLICATION:
            try:
                self.require_mission(self.journal.read(operation))
            except ValueError as error:
                raise PolicyDeniedError("Stage 0 mission changed") from error
        reference = _identity(
            operation, expires_at.isoformat(), purpose.value, operator.actor
        )
        approval = issue_approval(
            reference=reference,
            account_ref=self.account,
            operation_id=operation,
            expires_at=expires_at,
            purpose=purpose,
        )
        with self.journal.transaction() as db:
            if (
                db.execute("SELECT count(*) FROM approvals").fetchone()[0]
                >= MAX_INTENTS
            ):
                raise ValueError("Stage 0 approval capacity reached")
            db.execute(
                "INSERT OR IGNORE INTO approvals VALUES(?,?,?,?,0,?,?)",
                (
                    reference,
                    operation,
                    _binding(item),
                    expires_at.isoformat(),
                    purpose.value,
                    operator.actor,
                ),
            )
        return approval
