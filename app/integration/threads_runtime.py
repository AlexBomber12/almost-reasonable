"""Default-disabled public behavior to Stage 3 Threads coordinator."""

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path

from app.behavior.deliberation import (
    BehaviorCapabilities,
    DeliberationBudget,
    DeliberationEngine,
    DeliberationInput,
    DeliberationOutcome,
    PriorResultCategory,
    TriggerType,
)
from app.behavior.durable_scheduler import DurableScheduler, SQLiteSchedulerRepository
from app.behavior.russian_gate import RussianOutputPolicy
from app.core.agent_proposals import (
    AgentDecision,
    NoActionProposal,
    PublicationProposal,
    ReplyProposal,
)
from app.core.core_self import initial_core_self
from app.core.public_behavior import MAX_CONTEXT, public_context_digest
from app.core.public_behavior_mission import PublicMissionContext
from app.core.provider_spend import ProviderStateStore
from app.integration.threads_stage0 import (
    MAX_INTENTS,
    Stage0Journal,
    ThreadsStage0,
    _binding,
    _identity,
)
from app.owner_control.store import OwnerStore
from app.privacy.ledger import PrivacyLedger
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
    validate_approval,
    validate_content,
)
from app.threads.service import PolicyDeniedError, ThreadsService
from app.threads.store import ThreadsStore
from app.threads.transport import ThreadsHTTPTransport
from app.behavior.worker_client import PublicBehaviorClient

PUBLIC_RUNTIME_VERSION = "threads-public-runtime-v1"


@dataclass(frozen=True, slots=True)
class LocalOperator:
    """Trusted local administrative procedure: UID is read from the kernel."""

    uid: int
    actor: str

    def authenticate(self):
        if type(self.uid) is not int or self.uid != os.geteuid():
            raise PolicyDeniedError("local operator UID mismatch")
        require_reference("operator actor", self.actor)
        return self.actor


@dataclass(frozen=True, slots=True)
class PublicApprovalResult:
    operation_id: str
    state: OutboxState
    approval_reference: str


class PublicAuthorization:
    def __init__(self, app):
        self.app = app

    def current_approval(self, *, account_ref, operation_id, approval_reference, now):
        if account_ref != self.app.account:
            return None
        with self.app.journal.transaction() as db:
            row = db.execute(
                "SELECT * FROM approvals WHERE reference=? AND operation=?",
                (approval_reference, operation_id),
            ).fetchone()
        if row is not None and row["purpose"] == ApprovalPurpose.PUBLICATION.value:
            self.app.guard()
        return (
            None
            if row is None
            else issue_approval(
                reference=approval_reference,
                account_ref=account_ref,
                operation_id=operation_id,
                expires_at=self.app.parse_time(row["expires"]),
                revoked=bool(row["revoked"]),
                purpose=ApprovalPurpose(row["purpose"]),
            )
        )

    def current_publication_authorization(self, *, item, approval_reference, now):
        self.app.guard()
        with self.app.journal.transaction() as db:
            self.app.guard()
            generation = self.app.ledger.control_generation()
            intent = db.execute(
                "SELECT phase,payload FROM intents WHERE operation=?",
                (item.operation_id,),
            ).fetchone()
            approval = db.execute(
                "SELECT * FROM approvals WHERE reference=? AND operation=?",
                (approval_reference, item.operation_id),
            ).fetchone()
            source = db.execute(
                "SELECT active FROM sources WHERE draft=?", (item.draft_id,)
            ).fetchone()
            payload = None if intent is None else json.loads(intent["payload"])
            valid = (
                payload is not None
                and payload.get("runtime_version") == PUBLIC_RUNTIME_VERSION
                and intent["phase"] == "linked"
                and approval is not None
                and approval["binding"] == _binding(item)
                and source is not None
                and source[0] == 1
                and payload.get("generation") == generation
                and payload.get("draft") == item.draft_id
                and payload.get("content") == item.content
                and item.account_ref == self.app.account
                and item.source_provenance is SourceProvenance.PUBLIC
                and self.app.source_valid(payload)
            )
            if valid:
                try:
                    self.app.require_mission(payload)
                    valid = (
                        self.app.mission is None
                        or self.app.mission.publication_allowed()
                    )
                    if valid:
                        self.app.require_mission(payload)
                except ValueError:
                    valid = False
            valid = valid and self.app.ledger.control_generation() == generation
            self.app.guard()
            current = (
                None
                if approval is None
                else issue_approval(
                    reference=approval_reference,
                    account_ref=self.app.account,
                    operation_id=item.operation_id,
                    expires_at=self.app.parse_time(approval["expires"]),
                    revoked=bool(approval["revoked"]),
                    purpose=ApprovalPurpose(approval["purpose"]),
                )
            )
        return PublicationAuthorizationSnapshot(current, valid)


class ThreadsRuntime(ThreadsStage0):
    """Public-only coordinator; Stage 0's exact fake guards remain unchanged."""

    def __init__(
        self,
        journal_path,
        scheduler_path,
        threads_path,
        *,
        model,
        transport,
        policy,
        owner,
        provider,
        ledger,
        clock,
        wakeup_policy,
        operator,
        mission=None,
        enabled=False,
        live_enabled=False,
        checkpoint=lambda _point: None,
    ):
        if (
            type(model) is not PublicBehaviorClient
            or type(transport) is not ThreadsHTTPTransport
            or type(policy) is not AccountPolicy
            or type(owner) is not OwnerStore
            or type(provider) is not ProviderStateStore
            or type(ledger) is not PrivacyLedger
            or type(operator) is not LocalOperator
            or type(enabled) is not bool
            or type(live_enabled) is not bool
            or policy.stage is not Stage.STAGE_3
            or transport.account_ref != policy.account_ref
        ):
            raise ValueError("exact public runtime components required")
        self.enabled, self.live_enabled = enabled, live_enabled
        self.account, self.model, self.simulator = policy.account_ref, model, transport
        self.policy, self.owner, self.provider, self.ledger = (
            policy,
            owner,
            provider,
            ledger,
        )
        self.clock, self.operator, self.checkpoint = clock, operator, checkpoint
        self.model.reservation_evidence = self.reservation_evidence
        self.model.provider_store = provider
        if mission is not None and type(mission) is not PublicMissionContext:
            raise ValueError("verified mission context required")
        self.mission = mission
        journal_existed = Path(journal_path).exists()
        self.journal = Stage0Journal(
            journal_path, self.account, mission_required=mission is not None
        )
        with self.journal.transaction() as db:
            existing = db.execute(
                "SELECT name FROM sqlite_master WHERE name='public_runtime_schema'"
            ).fetchone()
            if existing is None:
                if journal_existed:
                    raise ValueError("Stage 0 journal cannot authorize public runtime")
                db.execute(
                    "CREATE TABLE public_runtime_schema(version TEXT NOT NULL, account TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT INTO public_runtime_schema VALUES(?,?)",
                    (PUBLIC_RUNTIME_VERSION, self.account),
                )
            elif [
                tuple(row)
                for row in db.execute(
                    "SELECT version,account FROM public_runtime_schema"
                )
            ] != [(PUBLIC_RUNTIME_VERSION, self.account)]:
                raise ValueError("invalid public runtime journal")
            db.execute(
                "CREATE TABLE IF NOT EXISTS public_audit(sequence INTEGER PRIMARY KEY, "
                "operation TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL, "
                "at TEXT NOT NULL, reference TEXT NOT NULL)"
            )
        if mission is not None and self.journal.needs_mission_initialization:
            self.journal.mark_mission_initialized()
        self.engine = DeliberationEngine(model, clock.now)
        self.authorization = PublicAuthorization(self)
        self.service = ThreadsService(
            ThreadsStore(threads_path),
            transport,
            policy,
            utc_now=clock.now,
            authorization_provider=self.authorization,
            poll_guard=self.guard,
            live_enabled=True,
        )
        self.scheduler = DurableScheduler(
            wakeup_policy,
            SQLiteSchedulerRepository(Path(scheduler_path)),
            clock,
            self._wake_deliberate,
            owner=owner,
            provider=provider,
        )

    @staticmethod
    def parse_time(value):
        from datetime import datetime

        return datetime.fromisoformat(value)

    def guard(self):
        if (
            not self.enabled
            or not self.live_enabled
            or self.policy.stage is not Stage.STAGE_3
            or self.service.simulator is not self.simulator
            or self.service.policy is not self.policy
            or self.service.authorization_provider is not self.authorization
            or self.engine._model is not self.model
            or self.simulator.account_ref != self.account
            or self.policy.account_ref != self.account
            or self.owner.active() is not False
            or self.provider.is_spend_suspended() is not False
            or self.policy.emergency_stop
            or not self.policy.account_authorized
            or not self.policy.privacy_gate_available
            or not self.policy.russian_output_gate_available
            or not self.policy.publication_capability_enabled
        ):
            raise PolicyDeniedError("public runtime controls deny operation")

    def attest_worker_request(self, request):
        try:
            payload = self.journal.read(request.operation_id)
        except ValueError:
            return False
        return (
            payload["phase"] == "started"
            and payload.get("runtime_version") == PUBLIC_RUNTIME_VERSION
            and request.operation_id == request.correlation_id
            and payload["trigger"] == request.trigger
            and payload["context_digest"] == public_context_digest(request.context)
            and tuple(payload["mission_text"]) == request.mission
            and 0 < request.deadline_seconds <= payload["timeout"]
        )

    def reservation_evidence(self, request):
        """Assert only the started operation fields to the UID authenticated worker."""
        if not self.attest_worker_request(request):
            raise ValueError("public operation is not reserved")
        payload = self.journal.read(request.operation_id)
        return {
            "account": self.account,
            "kind": payload["kind"],
            "event": payload["event"],
            "resumed": bool(payload.get("resumed")),
            "runtime_version": payload["runtime_version"],
            "phase": payload["phase"],
            "context_digest": payload["context_digest"],
            "mission": payload["mission_text"],
            "timeout": payload["timeout"],
        }

    def _decide(self, operation, origin, context, timeout=5):
        self.guard()
        try:
            self.journal.read(operation)
            return
        except ValueError:
            pass
        origin = {**origin, "runtime_version": PUBLIC_RUNTIME_VERSION}
        if any(
            type(text) is not str or len(text.encode()) > MAX_CONTEXT
            for text in context
        ):
            if self.journal.start(operation, origin):
                self.journal.finish(
                    operation,
                    {**origin, "reason": "public_context_too_large"},
                    rejected=True,
                )
            return
        mission_text = ()
        if self.mission is not None:
            text, binding = self.mission.snapshot()
            mission_text = (text,)
            origin = {**origin, "mission": binding}
        trigger = "autonomous_wakeup" if origin["kind"] == "wake" else "public_message"
        origin = {
            **origin,
            "trigger": trigger,
            "context_digest": public_context_digest(context),
            "mission_text": mission_text,
            "timeout": timeout,
        }
        if not self.journal.start(operation, origin):
            return
        self.checkpoint("after_started")
        self.guard()
        now = self.clock.now()
        generation = self.ledger.control_generation()
        result = self.engine.deliberate(
            DeliberationInput(
                operation,
                context,
                initial_core_self(),
                mission_text,
                (),
                BehaviorCapabilities(
                    "threads-public-v1",
                    False,
                    False,
                    not bool(
                        self.policy.publication_blockers(
                            PublicationType.REPLY, live_transport_enabled=True
                        )
                    ),
                    False,
                    not bool(
                        self.policy.publication_blockers(
                            PublicationType.POST, live_transport_enabled=True
                        )
                    ),
                ),
                TriggerType(trigger),
                DeliberationBudget(1, 1, timeout),
                now,
                now + timedelta(seconds=timeout),
                PriorResultCategory.NONE,
                False,
            )
        )
        payload = {
            **origin,
            "generation": generation,
            "decided_at": now.isoformat(),
            "result": result.outcome.value,
        }
        rejected = result.outcome not in (
            DeliberationOutcome.PROPOSED,
            DeliberationOutcome.NO_ACTION,
        )
        proposal = result.proposal
        if type(proposal) is NoActionProposal:
            payload.update(decision="no_action", reason=proposal.reason_category.value)
        elif type(proposal) in (PublicationProposal, ReplyProposal):
            content = (
                proposal.draft
                if type(proposal) is PublicationProposal
                else proposal.content
            )
            route_ok = (origin["kind"] == "wake") == (
                type(proposal) is PublicationProposal
            )
            if (
                not route_ok
                or RussianOutputPolicy(max_regenerations=0)
                .deliver(content)
                .used_fallback
            ):
                rejected = True
            else:
                try:
                    validate_content(content)
                except ValueError:
                    rejected = True
                else:
                    payload.update(
                        decision=proposal.decision.value,
                        content=content,
                        draft="public-"
                        + hashlib.sha256(content.encode()).hexdigest()[:32],
                    )
        else:
            rejected = True
        self.guard()
        if self.ledger.control_generation() != generation:
            self.journal.finish(operation, payload, held=True)
            return
        try:
            self.require_mission(payload)
        except ValueError:
            self.journal.finish(operation, payload, held=True)
            return
        self.journal.finish(operation, payload, rejected=rejected)
        self.checkpoint("after_intent")

    def _handoff(self, operation, payload):
        self.guard()
        if payload.get("runtime_version") != PUBLIC_RUNTIME_VERSION:
            raise PolicyDeniedError("foreign public intent")
        try:
            self.require_mission(payload)
        except ValueError as error:
            raise PolicyDeniedError("public mission changed") from error
        if (
            not self.source_valid(payload)
            or self.ledger.control_generation() != payload["generation"]
        ):
            raise PolicyDeniedError("public source changed")
        decision = AgentDecision(payload["decision"])
        if decision in (AgentDecision.REPLY, AgentDecision.PROPOSE_PUBLICATION):
            if self.mission is not None and not self.mission.publication_allowed():
                raise PolicyDeniedError("public mission excludes publication")
            try:
                self.require_mission(payload)
            except ValueError as error:
                raise PolicyDeniedError("public mission changed") from error
            kind = (
                PublicationType.POST
                if payload["kind"] == "wake"
                else PublicationType.REPLY
            )
            if self.policy.publication_blockers(kind, live_transport_enabled=True):
                raise PolicyDeniedError("public route unavailable")
            with self.journal.transaction() as db:
                db.execute(
                    "INSERT OR IGNORE INTO sources VALUES(?,1)", (payload["draft"],)
                )
                if (
                    db.execute(
                        "SELECT active FROM sources WHERE draft=?", (payload["draft"],)
                    ).fetchone()[0]
                    != 1
                ):
                    raise PolicyDeniedError("public draft revoked")
            item, _ = self.service.propose(
                operation_id=operation,
                publication_type=kind,
                parent_message_id=None
                if kind is PublicationType.POST
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
            if kind is PublicationType.REPLY:
                event = self.service.store.inbound_event(
                    self.account, InboundKind(payload["kind"]), payload["event"]
                )
                if event is None:
                    raise PolicyDeniedError("public reply source unavailable")
                self.service.store.acknowledge_reply_proposal(
                    operation, event, now=self.clock.now()
                )
        elif decision is AgentDecision.NO_ACTION and payload["kind"] != "wake":
            if not self.service.store.conversation_outcome_recorded(operation):
                self.service.store.record_conversation_outcome(
                    outcome_id=operation,
                    correlation_id=payload["correlation"],
                    account_ref=self.account,
                    stream=InboundKind(payload["kind"]),
                    inbound_event_id=payload["event"],
                    outcome=ConversationOutcome.NO_ACTION,
                    reason=payload["reason"],
                    resume_condition=None,
                    now=self.parse_time(payload["decided_at"]),
                    control_expires_at=None,
                )
        self.journal.linked(operation)

    def readiness(self):
        if not self.enabled or not self.live_enabled:
            return ("runtime_disabled",)
        blockers = list(self.service.stage_3_readiness())
        if self.owner.active():
            blockers.append("owner_stop")
        if self.provider.is_spend_suspended():
            blockers.append("provider_spend_suspended")
        return tuple(blockers)

    def draft(self, operation):
        self.guard()
        return self.service.store.get(operation)

    def _public_recovery_item(self, operation):
        item = self.service.store.get(operation)
        payload = self.journal.read(operation)
        if (
            item.account_ref != self.account
            or item.source_provenance is not SourceProvenance.PUBLIC
            or payload.get("runtime_version") != PUBLIC_RUNTIME_VERSION
            or payload["phase"] != "linked"
            or payload.get("draft") != item.draft_id
            or payload.get("content") != item.content
        ):
            raise PolicyDeniedError("public recovery target is not linked")
        return item

    def approve(self, operation, *, expires_at):
        self.guard()
        actor = self.operator.authenticate()
        item = self.service.store.get(operation)
        payload = self.journal.read(operation)
        if item.account_ref != self.account or payload["phase"] != "linked":
            raise PolicyDeniedError("public draft is not linked")
        if item.state not in {OutboxState.AWAITING_APPROVAL, OutboxState.READY}:
            raise PolicyDeniedError("public draft cannot be approved in this state")
        self.require_mission(payload)
        reference = _identity(operation, _binding(item), expires_at.isoformat(), actor)
        approval = issue_approval(
            reference=reference,
            account_ref=self.account,
            operation_id=operation,
            expires_at=expires_at,
            purpose=ApprovalPurpose.PUBLICATION,
        )
        with self.journal.transaction() as db:
            if (
                db.execute("SELECT count(*) FROM approvals").fetchone()[0]
                >= MAX_INTENTS
            ):
                raise ValueError("approval capacity reached")
            db.execute(
                "INSERT OR IGNORE INTO approvals VALUES(?,?,?,?,0,?,?)",
                (
                    reference,
                    operation,
                    _binding(item),
                    expires_at.isoformat(),
                    ApprovalPurpose.PUBLICATION.value,
                    actor,
                ),
            )
            db.execute(
                "INSERT INTO public_audit(operation,action,actor,at,reference) "
                "VALUES(?,?,?,?,?)",
                (operation, "approve", actor, self.clock.now().isoformat(), reference),
            )
        if item.state is OutboxState.READY:
            self.service._check_ready_policy(item, approval, now=self.clock.now())
            approved = self.service.store.get(operation)
        else:
            approved = self.service.authorize(operation, approval, now=self.clock.now())
        return PublicApprovalResult(operation, approved.state, reference)

    def revoke(self, reference):
        actor = self.operator.authenticate()
        with self.journal.transaction() as db:
            row = db.execute(
                "SELECT operation FROM approvals WHERE reference=?", (reference,)
            ).fetchone()
            if row is None:
                raise ValueError("approval not found")
            db.execute("UPDATE approvals SET revoked=1 WHERE reference=?", (reference,))
            db.execute(
                "INSERT INTO public_audit(operation,action,actor,at,reference) "
                "VALUES(?,?,?,?,?)",
                (row[0], "revoke", actor, self.clock.now().isoformat(), reference),
            )

    def dispatch(self, operation):
        self.guard()
        self.operator.authenticate()
        item = self.service.store.get(operation)
        if item.approval_reference is None:
            raise PolicyDeniedError("public draft lacks approval")
        with self.journal.transaction() as db:
            rows = db.execute(
                "SELECT reference FROM approvals WHERE operation=? AND binding=? "
                "AND purpose=? ORDER BY rowid DESC",
                (operation, _binding(item), ApprovalPurpose.PUBLICATION.value),
            ).fetchall()
        approval = None
        for row in rows:
            candidate = self.authorization.current_approval(
                account_ref=self.account,
                operation_id=operation,
                approval_reference=row["reference"],
                now=self.clock.now(),
            )
            if candidate is None:
                continue
            try:
                validate_approval(
                    candidate,
                    account_ref=self.account,
                    operation_id=operation,
                    now=self.clock.now(),
                )
            except ValueError:
                continue
            approval = candidate
            break
        if approval is None:
            raise PolicyDeniedError("public approval unavailable")
        return self.service.dispatch(operation, approval, now=self.clock.now())

    def reconcile(self, operation, correlation_id):
        self.guard()
        self.operator.authenticate()
        return self.service.reconcile(
            operation, correlation_id=correlation_id, now=self.clock.now()
        )

    def recover_sending(self, operation):
        self.operator.authenticate()
        self._public_recovery_item(operation)
        return self.service.recover_sending(operation, now=self.clock.now())

    def approve_reconciliation_recovery(self, operation, *, expires_at):
        actor = self.operator.authenticate()
        item = self._public_recovery_item(operation)
        if item.state is not OutboxState.UNKNOWN:
            raise PolicyDeniedError("reconciliation recovery requires unknown state")
        reference = _identity(
            operation,
            ApprovalPurpose.RECOVER_RECONCILIATION.value,
            expires_at.isoformat(),
            actor,
        )
        issue_approval(
            reference=reference,
            account_ref=self.account,
            operation_id=operation,
            expires_at=expires_at,
            purpose=ApprovalPurpose.RECOVER_RECONCILIATION,
        )
        with self.journal.transaction() as db:
            if (
                db.execute("SELECT count(*) FROM approvals").fetchone()[0]
                >= MAX_INTENTS
            ):
                raise ValueError("approval capacity reached")
            db.execute(
                "INSERT OR IGNORE INTO approvals VALUES(?,?,?,?,0,?,?)",
                (
                    reference,
                    operation,
                    _binding(item),
                    expires_at.isoformat(),
                    ApprovalPurpose.RECOVER_RECONCILIATION.value,
                    actor,
                ),
            )
            db.execute(
                "INSERT INTO public_audit(operation,action,actor,at,reference) "
                "VALUES(?,?,?,?,?)",
                (
                    operation,
                    "approve_reconciliation_recovery",
                    actor,
                    self.clock.now().isoformat(),
                    reference,
                ),
            )
        return PublicApprovalResult(operation, item.state, reference)

    def recover_reconciliation_claim(self, operation, approval_reference):
        self.operator.authenticate()
        self._public_recovery_item(operation)
        approval = self.authorization.current_approval(
            account_ref=self.account,
            operation_id=operation,
            approval_reference=approval_reference,
            now=self.clock.now(),
        )
        if approval is None:
            raise PolicyDeniedError("reconciliation recovery approval unavailable")
        self.service.recover_reconciliation_claim(
            operation, approval, now=self.clock.now()
        )
        return self.service.store.get(operation)
