"""Default-disabled public behavior to Stage 3 Threads coordinator."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3

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
from app.research.assessment import MAX_PUBLIC_EVIDENCE_BYTES
from app.core.agent_proposals import (
    AgentDecision,
    NoActionProposal,
    PublicationProposal,
    ReplyProposal,
    ResearchProposal,
)
from app.core.core_self import initial_core_self
from app.core.public_behavior import MAX_CONTEXT, public_context_digest
from app.core.public_experience import (
    inference_context_digest,
    serialized,
    EXPERIENCE_VERSION,
)
from app.integration.public_experience import (
    PublicExperienceHistory,
    initialize_experience_storage,
)
from app.core.public_behavior import PublicMissionChoice, parse_public_proposal
from app.core.public_behavior_mission import PublicMissionContext
from app.core.public_mission_evolution import PublicMissionEvolution
from app.core.public_research_client import PublicResearchClient
from app.core.public_research_profile import (
    PUBLIC_PURPOSES,
    FREE_QUESTION_CHOICE,
    load_research_profile,
)
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


def _initialize_research_storage(db):
    """Transactionally retain historical reports while widening evidence only."""
    schema = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='public_research_ops'"
    ).fetchone()
    legacy = schema is not None and "length(evidence)<=2048" in schema[0]
    if legacy:
        db.execute(
            "ALTER TABLE public_research_ops RENAME TO public_research_ops_legacy"
        )
    db.execute(
        "CREATE TABLE IF NOT EXISTS public_research_ops(operation TEXT PRIMARY KEY, "
        "research_id TEXT NOT NULL UNIQUE, topic TEXT NOT NULL, profile_digest TEXT NOT NULL, "
        "phase TEXT NOT NULL CHECK(phase IN ('started','completed','failed','uncertain','held')), "
        f"evidence TEXT CHECK(evidence IS NULL OR length(CAST(evidence AS BLOB))<={MAX_PUBLIC_EVIDENCE_BYTES}))"
    )
    if legacy:
        db.execute(
            "INSERT INTO public_research_ops SELECT * FROM public_research_ops_legacy"
        )
        db.execute("DROP TABLE public_research_ops_legacy")


class PublicInferenceContextChanged(ValueError):
    """Persisted inference context no longer matches current public evidence."""


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
        if row is not None and row["purpose"] in (
            ApprovalPurpose.PUBLICATION.value,
            ApprovalPurpose.AUTONOMOUS_PUBLICATION.value,
        ):
            self.app.publication_guard()
        if (
            row is not None
            and row["purpose"] == ApprovalPurpose.AUTONOMOUS_PUBLICATION.value
        ):
            item = self.app.service.store.get(operation_id)
            with self.app.journal.transaction() as db:
                return self.app.public_grants.current_in(db, item, approval_reference)
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
        self.app.publication_guard()
        with self.app.journal.transaction() as db:
            self.app.publication_guard()
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
                and self.app.intellectual.valid(item.operation_id)
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
            self.app.publication_guard()
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
            if (
                current is not None
                and current.purpose is ApprovalPurpose.AUTONOMOUS_PUBLICATION
            ):
                current = self.app.public_grants.current_in(
                    db, item, approval_reference
                )
                valid = valid and current is not None
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
        research_client=None,
        research_profile_path=None,
        research_scope=None,
        research_enabled=False,
        mission_evolution_enabled=False,
        free_missions_enabled=False,
        autonomous_publication_enabled=False,
        shared_intellectual_source=None,
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
        if type(free_missions_enabled) is not bool or (
            free_missions_enabled and not mission_evolution_enabled
        ):
            raise ValueError("free mission mode requires enabled evolution")
        self.free_missions_enabled = free_missions_enabled
        if type(autonomous_publication_enabled) is not bool:
            raise ValueError("invalid autonomous publication mode")
        self.autonomous_publication_enabled = autonomous_publication_enabled
        from app.integration.public_grant import PublicPublicationGrants

        self.public_grants = PublicPublicationGrants(self)
        if type(mission_evolution_enabled) is not bool or (
            mission_evolution_enabled
            and (
                mission is None or (not research_enabled and not free_missions_enabled)
            )
        ):
            raise ValueError("invalid public mission evolution configuration")
        self.enabled, self.live_enabled = enabled, live_enabled
        from app.integration.shared_intellectual_source import SharedIntellectualSource

        if shared_intellectual_source is not None and (
            type(shared_intellectual_source) is not SharedIntellectualSource
            or shared_intellectual_source.account != policy.account_ref
        ):
            raise ValueError("shared intellectual source account mismatch")
        if shared_intellectual_source is not None and os.path.samefile(
            shared_intellectual_source.shared._gate._ledger.path, ledger.path
        ):
            raise ValueError("shared influence requires independent privacy ledgers")
        self.shared_intellectual_source = shared_intellectual_source
        self.operating_window = None
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
        if type(research_enabled) is not bool or (
            research_enabled
            and (
                type(research_client) is not PublicResearchClient
                or research_profile_path is None
                or not research_scope
                or mission is None
            )
        ):
            raise ValueError("invalid public research configuration")
        self.research_enabled = research_enabled
        self.research_client = research_client
        self.research_profile_path = research_profile_path
        self.research_scope = research_scope
        self.model.public_context = self.inference_public_context
        self.model.public_experience = self.inference_public_experience
        self.model.operation_deadline = self.inference_deadline
        self.model.utc_now = clock.now
        if mission is not None and type(mission) is not PublicMissionContext:
            raise ValueError("verified mission context required")
        if (
            mission_evolution_enabled
            and mission.root_mission().parent_version is not None
        ):
            raise ValueError("public mission evolution requires a trusted root mission")
        self.mission = mission
        self.mission_evolution_enabled = mission_evolution_enabled
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
            _initialize_research_storage(db)
            initialize_experience_storage(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS public_research_completions("
                "operation TEXT PRIMARY KEY, completed_at TEXT NOT NULL)"
            )
        if mission is not None and self.journal.needs_mission_initialization:
            self.journal.mark_mission_initialized()
        with self.journal.transaction() as db:
            evolution_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='public_mission_state'"
            ).fetchone()
            evolution_state = (
                evolution_table
                and db.execute(
                    "SELECT 1 FROM public_mission_state WHERE id=1"
                ).fetchone()
            )
        if (
            mission_evolution_enabled
            and not free_missions_enabled
            and not evolution_state
            and self.current_research_profile() is None
        ):
            raise ValueError(
                "public mission evolution requires active research profile"
            )
        if not mission_evolution_enabled and evolution_table:
            raise ValueError(
                "persisted public mission evolution requires enabled configuration"
            )
        self.mission_evolution = None
        self.experience_history = PublicExperienceHistory(self)
        from app.integration.intellectual_state import PublicIntellectualState

        self.intellectual = PublicIntellectualState(self)
        self.model.intellectual_context = self.inference_intellectual_context
        self.engine = DeliberationEngine(
            model,
            clock.now,
            parser=lambda value: parse_public_proposal(
                value,
                evolution=mission_evolution_enabled,
                free_missions=self.free_missions_enabled,
            ),
        )
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
        self.mission_evolution = (
            PublicMissionEvolution(self) if mission_evolution_enabled else None
        )
        self.model.mission_evolution_context = (
            self.inference_mission_evolution_context
            if mission_evolution_enabled
            else None
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
        ):
            raise PolicyDeniedError("public runtime controls deny operation")
        if self.operating_window is not None:
            self.operating_window.guard()

    def publication_guard(self):
        self.guard()
        if not self.policy.publication_capability_enabled:
            raise PolicyDeniedError("public publication capability disabled")

    def current_research_profile(self):
        if not self.research_enabled or self.mission is None:
            return None
        try:
            profile = load_research_profile(
                self.research_profile_path, role="application"
            )
        except (OSError, ValueError, TypeError, UnicodeError):
            return None
        if (
            profile.purpose not in PUBLIC_PURPOSES
            or profile.account_ref != self.account
            or profile.application_scope != self.research_scope
            or profile.peer_uid != os.geteuid()
            or not profile.issued_at <= self.clock.now() < profile.expires_at
        ):
            return None
        return profile

    def public_context(self, operation):
        return self._public_context(self.current_research_profile())

    def inference_public_context(self, operation):
        payload = self.journal.read(operation)
        profile = self.current_research_profile()
        if payload["phase"] != "started":
            return (), ()
        if profile is None and payload.get("offered_profile_digest") is None:
            return (), ()
        if profile is None or profile.digest != payload.get("offered_profile_digest"):
            if self.mission_evolution is not None or "intellectual_digest" in payload:
                raise PublicInferenceContextChanged(
                    "public research profile changed during inference"
                )
            return (), ()
        try:
            catalog, evidence, source = self._public_context_with_source(profile)
        except ValueError as error:
            raise PublicInferenceContextChanged(
                "public mission evidence changed during inference"
            ) from error
        if not self.intellectual.auxiliary_matches(payload, catalog, evidence, source):
            raise PublicInferenceContextChanged(
                "public intellectual context changed during inference"
            )
        if self.mission_evolution is not None and (
            source != payload.get("mission_evidence_operation")
            or public_context_digest(evidence) != payload.get("mission_evidence_digest")
        ):
            raise PublicInferenceContextChanged(
                "public mission evidence changed during inference"
            )
        return catalog, evidence

    def _public_context(self, profile):
        catalog, evidence, _ = self._public_context_with_source(profile)
        return catalog, evidence

    def _public_context_with_source(self, profile):
        choices = self.available_topics(profile)
        catalog = () if profile is None else profile.catalog(choices)
        evidence, source = (), None
        if profile is not None:
            pending = (
                None
                if self.mission_evolution is None
                else self.mission_evolution.pending()
            )
            with self.journal.transaction() as db:
                evidence, source = self._selected_public_evidence_in(
                    db, profile, pending
                )
        return catalog, evidence, source

    def _selected_public_evidence_in(self, db, profile, pending):
        if profile is None:
            return (), None
        rows = db.execute(
            "SELECT r.operation,r.evidence,i.payload,c.completed_at,i.rowid AS intent_rowid "
            "FROM public_research_ops r "
            "JOIN intents i ON i.operation=r.operation "
            "LEFT JOIN public_research_completions c ON c.operation=r.operation "
            "WHERE r.phase='completed' AND r.profile_digest=?",
            (profile.digest,),
        ).fetchall()
        for row in sorted(
            rows,
            key=lambda item: (
                datetime.fromisoformat(
                    item["completed_at"] or json.loads(item["payload"])["decided_at"]
                ),
                item["intent_rowid"],
            ),
            reverse=True,
        ):
            if pending is not None and row["completed_at"] is None:
                continue
            if pending is not None and (
                datetime.fromisoformat(row["completed_at"])
                <= pending.proposal.decided_at
            ):
                continue
            payload = json.loads(row["payload"])
            if (
                pending is not None
                and payload.get("mission_invariants")
                != pending.proposal.invariants.to_dict()
            ):
                continue
            try:
                self.require_mission(payload)
                if (
                    self.source_valid(payload)
                    and self.ledger.control_generation() == payload["generation"]
                ):
                    from app.core.intellectual_state import MAX_DEPENDENCIES

                    dependencies = self.intellectual.state.dependencies(
                        db, row["operation"]
                    )
                    # Cached evidence is optional context. Keep room for a current
                    # inbound event and omit the whole report when ancestry cannot fit.
                    if (
                        len(dependencies.keys() | {"research:" + row["operation"]})
                        >= MAX_DEPENDENCIES
                    ):
                        continue
                    return (row["evidence"],), row["operation"]
            except ValueError:
                continue
        return (), None

    def available_topics(self, profile):
        if profile is None:
            return ()
        with self.journal.transaction() as db:
            return self._available_topics_in(db, profile)

    def _available_topics_in(self, db, profile):
        rows = db.execute(
            "SELECT topic,COUNT(*) FROM public_research_ops GROUP BY topic"
        ).fetchall()
        counts = dict(rows)
        if sum(counts.values()) >= profile.aggregate_limit:
            return ()
        if profile.question_policy is not None:
            return (FREE_QUESTION_CHOICE[0],)
        return tuple(
            topic.topic_id
            for topic in profile.topics
            if counts.get(topic.topic_id, 0) < topic.limit
        )

    def inference_public_experience(self, operation):
        payload = self.journal.read(operation)
        try:
            self.guard()
            self.require_mission(payload)
            if not super().source_valid(payload):
                raise ValueError("public trigger changed")
            return self.experience_history.load(operation, payload)
        except ValueError as error:
            raise PublicInferenceContextChanged("public experience changed") from error

    def inference_intellectual_context(self, operation):
        try:
            return self.intellectual.context(operation)
        except ValueError as error:
            raise PublicInferenceContextChanged(
                "public intellectual state changed"
            ) from error

    def source_valid(self, payload):
        if not super().source_valid(payload):
            return False
        try:
            self.experience_history.load(
                payload.get("experience_operation", payload["correlation"]), payload
            )
            return self.intellectual.valid(
                payload.get("experience_operation", payload["correlation"]),
                revisions=False,
            )
        except ValueError:
            return False

    def attest_worker_request(self, request):
        try:
            payload = self.journal.read(request.operation_id)
        except ValueError:
            return False
        try:
            return (
                payload["phase"] == "started"
                and payload.get("runtime_version") == PUBLIC_RUNTIME_VERSION
                and request.operation_id == request.correlation_id
                and payload["trigger"] == request.trigger
                and payload["context_digest"]
                == (
                    public_context_digest(request.context)
                    if request.experience is None
                    else inference_context_digest(request.context, request.experience)
                )
                and self.inference_public_experience(request.operation_id)
                == request.experience
                and self.inference_intellectual_context(request.operation_id)
                == request.intellectual_state
                and tuple(payload["mission_text"]) == request.mission
                and self.inference_public_context(request.operation_id)
                == (request.catalog, request.evidence)
                and self.inference_mission_evolution_context(request.operation_id)
                == request.mission_evolution
                and 0 < request.deadline_seconds <= payload["timeout"]
                and request.deadline_at == self.inference_deadline(request.operation_id)
            )
        except PublicInferenceContextChanged:
            self.journal.finish(
                request.operation_id,
                {**payload, "reason": "public_inference_context_changed"},
                held=True,
            )
            return False

    def inference_mission_evolution_context(self, operation):
        if self.mission_evolution is None:
            return ()
        payload = self.journal.read(operation)
        if payload["phase"] != "started":
            return ()
        value = tuple(payload["mission_evolution"])
        try:
            current = self.mission_evolution.model_context()
        except ValueError as error:
            raise PublicInferenceContextChanged(
                "public mission proposal changed during inference"
            ) from error
        if value != current:
            raise PublicInferenceContextChanged(
                "public mission proposal changed during inference"
            )
        return value

    def reservation_evidence(self, request):
        """Assert only the started operation fields to the UID authenticated worker."""
        self.guard()
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
            **(
                {"intellectual_digest": payload["intellectual_digest"]}
                if "intellectual_digest" in payload
                else {}
            ),
        }

    def inference_deadline(self, operation):
        if self.operating_window is None:
            return None
        return self.parse_time(self.journal.read(operation)["deadline_at"])

    def _decide(self, operation, origin, context, timeout=5):
        self.guard()
        deadline = None
        if self.operating_window is not None:
            deadline = min(
                self.operating_window.spec.expires_at,
                self.clock.now() + timedelta(seconds=timeout),
            )
        try:
            self.journal.read(operation)
            return
        except ValueError:
            pass
        origin = {**origin, "runtime_version": PUBLIC_RUNTIME_VERSION}
        if deadline is not None:
            origin["deadline_at"] = deadline.isoformat()
        if any(
            type(text) is not str or len(text.encode()) > MAX_CONTEXT
            for text in context
        ):
            if self._start_public_operation(operation, origin):
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
        mission_allows_publication = (
            self.mission is None or self.mission.publication_allowed()
        )
        evolution_context, mission_pending_id = (
            ((), None)
            if self.mission_evolution is None
            else self.mission_evolution.decision_context()
        )
        offered_profile = self.current_research_profile()
        mission_evidence_operation = None
        mission_evidence_digest = public_context_digest(())
        offered_catalog, offered_evidence, offered_research_source = (
            self._public_context_with_source(offered_profile)
        )
        if self.mission_evolution is not None:
            mission_evidence_operation = offered_research_source
            mission_evidence_digest = public_context_digest(offered_evidence)
        experience_generation = self.ledger.control_generation()
        selected_experience, experience = self.experience_history.select(
            origin, research_source=offered_research_source
        )
        trigger = "autonomous_wakeup" if origin["kind"] == "wake" else "public_message"
        origin = {
            **origin,
            "offered_profile_digest": None
            if offered_profile is None
            else offered_profile.digest,
            "trigger": trigger,
            "intellectual_research_source": offered_research_source,
            "intellectual_auxiliary_digest": self.intellectual.auxiliary_digest(
                offered_catalog, offered_evidence
            ),
            "context_digest": inference_context_digest(context, experience),
            "experience_digest": experience.digest,
            "experience_mission_root": None
            if self.mission is None
            else self.mission._root_snapshot()[1],
            "experience_generation": experience_generation,
            "experience_operation": operation,
            "mission_text": mission_text,
            "mission_evolution": evolution_context,
            "timeout": timeout,
        }
        if self.mission_evolution is not None:
            origin["mission_invariants"] = self.mission_evolution.invariants().to_dict()
            origin["mission_pending_id"] = mission_pending_id
            origin["mission_evidence_operation"] = mission_evidence_operation
            origin["mission_evidence_digest"] = mission_evidence_digest
        if not self._start_public_operation(operation, origin, selected_experience):
            return
        self.checkpoint("after_started")
        self.guard()
        now = self.clock.now()
        generation = self.ledger.control_generation()
        deliberation = DeliberationInput(
            operation,
            context,
            initial_core_self(),
            mission_text,
            (),
            BehaviorCapabilities(
                "threads-public-v1",
                False,
                False,
                mission_allows_publication
                and not bool(
                    self.policy.publication_blockers(
                        PublicationType.REPLY, live_transport_enabled=True
                    )
                ),
                bool(self.available_topics(self.current_research_profile())),
                mission_allows_publication
                and not bool(
                    self.policy.publication_blockers(
                        PublicationType.POST, live_transport_enabled=True
                    )
                ),
            ),
            TriggerType(trigger),
            DeliberationBudget(1, 1, timeout),
            now,
            now + timedelta(seconds=timeout) if deadline is None else deadline,
            PriorResultCategory.NONE,
            False,
        )
        try:
            result = self.engine.deliberate(deliberation)
        except PublicInferenceContextChanged:
            self.journal.finish(
                operation,
                {**origin, "reason": "public_inference_context_changed"},
                held=True,
            )
            return
        except ValueError:
            if self.journal.read(operation)["phase"] == "held":
                return
            if self.mission_evolution is not None:
                try:
                    self.inference_public_context(operation)
                    self.inference_mission_evolution_context(operation)
                except PublicInferenceContextChanged:
                    self.journal.finish(
                        operation,
                        {**origin, "reason": "public_inference_context_changed"},
                        held=True,
                    )
                    return
            raise
        if self.mission_evolution is not None:
            try:
                self.inference_public_context(operation)
                self.inference_mission_evolution_context(operation)
            except PublicInferenceContextChanged:
                self.journal.finish(
                    operation,
                    {**origin, "reason": "public_inference_context_changed"},
                    held=True,
                )
                return
        try:
            self.inference_public_experience(operation)
            self.inference_public_context(operation)
            self.inference_intellectual_context(operation)
            self.intellectual.commit(operation)
            self.checkpoint("after_intellectual_commit")
        except ValueError:
            self.journal.finish(
                operation,
                {**origin, "reason": "public_inference_context_changed"},
                held=True,
            )
            return
        payload = {
            **origin,
            "generation": generation,
            "decided_at": (
                self.clock.now() if self.free_missions_enabled else now
            ).isoformat(),
            "result": result.outcome.value,
        }
        rejected = result.outcome not in (
            DeliberationOutcome.PROPOSED,
            DeliberationOutcome.NO_ACTION,
        )
        proposal = result.proposal
        if type(proposal) is NoActionProposal:
            payload.update(decision="no_action", reason=proposal.reason_category.value)
        elif type(proposal) is ResearchProposal:
            profile = self.current_research_profile()
            topic = (
                None
                if profile is None
                else profile.topic(proposal.brief)
                if profile.question_policy is not None
                else next(
                    (
                        item
                        for item in profile.topics
                        if item.topic_id == proposal.brief
                    ),
                    None,
                )
            )
            if (
                topic is None
                or profile.digest != origin["offered_profile_digest"]
                or (
                    FREE_QUESTION_CHOICE[0]
                    if profile.question_policy is not None
                    else topic.topic_id
                )
                not in self.available_topics(profile)
            ):
                rejected = True
            else:
                payload.update(
                    decision="research",
                    topic=topic.question
                    if profile.question_policy is not None
                    else topic.topic_id,
                    profile_digest=profile.digest,
                )
        elif (
            type(proposal) is PublicMissionChoice
            and self.mission_evolution is not None
            and evolution_context
        ):
            try:
                pending = self.mission_evolution.pending()
            except ValueError:
                self.journal.finish(operation, payload, held=True)
                return
            if (
                proposal.free != self.free_missions_enabled
                or (
                    proposal.free
                    and any(
                        RussianOutputPolicy(max_regenerations=0)
                        .deliver(text)
                        .used_fallback
                        for text in (
                            (proposal.text,) if proposal.text is not None else ()
                        )
                        + proposal.reasons
                        + proposal.counterevidence
                    )
                )
                or (
                    proposal.action == "propose"
                    and (mission_pending_id is not None or pending is not None)
                )
                or (
                    proposal.action != "propose"
                    and (
                        mission_pending_id is None
                        or pending is None
                        or pending.proposal.proposal_id != mission_pending_id
                    )
                )
                or (
                    proposal.action in ("confirm", "reject")
                    and (
                        (not proposal.free and mission_evidence_operation is None)
                        or (proposal.free and origin["kind"] != "wake")
                        or pending.proposal.invariants.to_dict()
                        != origin["mission_invariants"]
                    )
                )
            ):
                rejected = True
            else:
                payload.update(
                    decision=AgentDecision.REVISE_MISSION.value,
                    mission_action=proposal.action,
                    mission_purpose=None
                    if proposal.purpose is None
                    else proposal.purpose.value,
                )
                if proposal.free:
                    payload.update(
                        mission_free=True,
                        free_mission_text=proposal.text,
                        mission_reasons=proposal.reasons,
                        mission_counterevidence=proposal.counterevidence,
                    )
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
                not mission_allows_publication
                or not route_ok
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
        if (
            self.mission_evolution is not None
            and self.mission_evolution.invariants().to_dict()
            != origin["mission_invariants"]
        ):
            self.journal.finish(operation, payload, held=True)
            return

        def mission_admissible(db):
            try:
                self.guard()
                pending = self.mission_evolution.pending_in(db)
                profile = self.current_research_profile()
                offered_profile_digest = None if profile is None else profile.digest
                evidence, source = self._selected_public_evidence_in(
                    db, profile, pending
                )
                return (
                    self.ledger.control_generation() == generation
                    and self.mission_evolution.invariants().to_dict()
                    == origin["mission_invariants"]
                    and self.source_valid(payload)
                    and (None if pending is None else pending.proposal.proposal_id)
                    == mission_pending_id
                    and offered_profile_digest == origin["offered_profile_digest"]
                    and source == origin["mission_evidence_operation"]
                    and public_context_digest(evidence)
                    == origin["mission_evidence_digest"]
                    and (
                        payload.get("mission_action") != "propose"
                        or (
                            (
                                self.free_missions_enabled
                                or (
                                    profile is not None
                                    and bool(self._available_topics_in(db, profile))
                                )
                            )
                            and self.mission_evolution.proposal_capacity_in(db)
                        )
                    )
                    and (
                        payload.get("mission_action") not in ("confirm", "reject")
                        or self.mission_evolution.offered_evidence_in(db, payload)
                    )
                )
            except (PolicyDeniedError, ValueError):
                return False

        self.journal.finish(
            operation,
            payload,
            rejected=rejected,
            admissible=None if self.mission_evolution is None else mission_admissible,
        )
        self.checkpoint("after_intent")

    def _start_public_operation(self, operation, origin, selected_experience=None):
        intellectual_sources = self.intellectual.sources()

        def admit(db, operation, origin):
            if (
                self.operating_window is not None
                and not self.operating_window.admit_deliberation(db, operation, origin)
            ):
                return False
            if selected_experience is not None:
                db.execute(
                    "INSERT INTO public_experience_snapshots VALUES(?,?,?)",
                    (
                        operation,
                        EXPERIENCE_VERSION,
                        serialized(selected_experience).decode(),
                    ),
                )
            intellectual_projection = self.intellectual.reserve(
                db, operation, origin, selected_experience or [], intellectual_sources
            )
            from app.core.intellectual_state import digest

            origin["intellectual_digest"] = digest(intellectual_projection)
            return True

        started = self.journal.start(operation, origin, admit=admit)
        if not started and self.operating_window is not None:
            raise ValueError("operating window admission unavailable")
        return started

    def _handoff(self, operation, payload):
        if self.mission_evolution is None:
            with self.service.store.conversation_control_effect_fence():
                return self._handoff_with_mission_fence(operation, payload)
        with self.journal.handoff_lock("mission-evolution"):
            if AgentDecision(payload["decision"]) in (
                AgentDecision.REVISE_MISSION,
                AgentDecision.RESEARCH,
            ):
                return self._handoff_with_mission_fence(operation, payload)
            with self.service.store.conversation_control_effect_fence():
                return self._handoff_with_mission_fence(operation, payload)

    def _handoff_with_mission_fence(self, operation, payload):
        self.guard()
        if payload.get("runtime_version") != PUBLIC_RUNTIME_VERSION:
            raise PolicyDeniedError("foreign public intent")
        if self.mission_evolution is not None and self.mission_evolution.hold_if_stale(
            operation, payload
        ):
            return
        try:
            self.require_mission(payload)
        except ValueError as error:
            raise PolicyDeniedError("public mission changed") from error
        if (
            not self.source_valid(payload)
            or not self.intellectual.valid(operation)
            or self.ledger.control_generation() != payload["generation"]
        ):
            raise PolicyDeniedError("public source changed")
        decision = AgentDecision(payload["decision"])
        if decision is AgentDecision.REVISE_MISSION:
            if self.mission_evolution is None:
                raise PolicyDeniedError("public mission evolution disabled")
            try:
                self.mission_evolution.apply(operation, payload)
            except (ValueError, sqlite3.Error) as error:
                phase = self.journal.read(operation)["phase"]
                if phase == "linked":
                    self._record_mission_outcome(operation, payload)
                    return
                if phase == "held":
                    return
                if isinstance(
                    error, ValueError
                ) and self.mission_evolution.hold_if_stale(operation, payload):
                    return
                if isinstance(error, ValueError):
                    raise PolicyDeniedError(
                        "public mission transition unavailable"
                    ) from error
                raise
            self._record_mission_outcome(operation, payload)
            return
        if decision is AgentDecision.RESEARCH:
            self._research(operation, payload)
            self.journal.linked(operation)
            return
        if decision in (AgentDecision.REPLY, AgentDecision.PROPOSE_PUBLICATION):
            self.publication_guard()
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

    def _record_mission_outcome(self, operation, payload):
        if payload[
            "kind"
        ] != "wake" and not self.service.store.conversation_outcome_recorded(operation):
            self.service.store.record_linked_mission_outcome(
                outcome_id=operation,
                correlation_id=payload["correlation"],
                account_ref=self.account,
                stream=InboundKind(payload["kind"]),
                inbound_event_id=payload["event"],
                now=self.parse_time(payload["decided_at"]),
            )

    def recover(self):
        operations = (
            None
            if self.operating_window is None
            else self.operating_window.operations()
        )
        blocked = super().recover(operations=operations)
        if self.mission_evolution is not None:
            with self.journal.transaction() as db:
                rows = db.execute(
                    "SELECT operation,payload FROM intents WHERE phase='linked'"
                ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                if payload.get("decision") == AgentDecision.REVISE_MISSION.value and (
                    operations is None or row["operation"] in operations
                ):
                    self._record_mission_outcome(row["operation"], payload)
        return blocked

    def _research(self, operation, payload):
        with self.journal.transaction() as db:
            existing = db.execute(
                "SELECT phase FROM public_research_ops WHERE operation=?", (operation,)
            ).fetchone()
            if existing is not None:
                if existing[0] == "started":
                    db.execute(
                        "UPDATE public_research_ops SET phase='uncertain' WHERE operation=?",
                        (operation,),
                    )
                return

        def mission_controls_match():
            return (
                self.mission_evolution is None
                or self.mission_evolution.invariants().to_dict()
                == payload["mission_invariants"]
            )

        if not mission_controls_match():
            self.journal.hold_intent(operation)
            return
        profile = self.current_research_profile()
        if profile is None or profile.digest != payload["profile_digest"]:
            raise PolicyDeniedError("public research authority changed")
        if (
            not self.source_valid(payload)
            or self.ledger.control_generation() != payload["generation"]
        ):
            raise PolicyDeniedError("public research source changed")
        self.require_mission(payload)
        research_id = _identity(
            operation, "research", payload["topic"], profile.profile_id
        )
        request = profile.request(research_id, payload["topic"])
        with self.journal.transaction() as db:
            existing = db.execute(
                "SELECT phase FROM public_research_ops WHERE operation=?", (operation,)
            ).fetchone()
            if existing is not None:
                return
            counts = dict(
                db.execute(
                    "SELECT topic,COUNT(*) FROM public_research_ops GROUP BY topic"
                )
            )
            topic = profile.topic(payload["topic"])
            if (
                topic is None
                or sum(counts.values()) >= profile.aggregate_limit
                or counts.get(
                    payload["topic"]
                    if profile.question_policy is not None
                    else topic.topic_id,
                    0,
                )
                >= topic.limit
            ):
                raise PolicyDeniedError("public research capacity exhausted")
            db.execute(
                "INSERT INTO public_research_ops VALUES(?,?,?,?,?,NULL)",
                (operation, research_id, payload["topic"], profile.digest, "started"),
            )
        self.checkpoint("after_research_intent")
        phase, evidence = "uncertain", None
        try:
            self.guard()
            self.require_mission(payload)
            if (
                self.current_research_profile() != profile
                or not self.source_valid(payload)
                or self.ledger.control_generation() != payload["generation"]
                or not mission_controls_match()
            ):
                phase = "held"
            else:
                result = self.research_client.execute(request)
                phase = "uncertain" if result["attempts"] == 1 else "failed"
                if result["outcome"] == "completed":
                    report = result["report"]
                    evidence = json.dumps(
                        {
                            "topic": payload["topic"],
                            "question": request.question,
                            "main_finding": report["main_finding"],
                            "sources": report["sources"],
                            "claim_status": "unverified",
                            "semantic_support": "not_evaluated",
                            **{
                                key: report[key]
                                for key in ("uncertainty", "assessment")
                                if key in report
                            },
                        },
                        ensure_ascii=False,
                    )
                    if len(evidence.encode("utf-8")) > MAX_PUBLIC_EVIDENCE_BYTES:
                        evidence = None
                        phase = "failed"
                    else:
                        phase = "completed"
        except (OSError, ValueError, TimeoutError, PolicyDeniedError):
            phase = "uncertain"
        with (
            self.service.store.conversation_control_fence(),
            self.journal.transaction() as db,
        ):
            try:
                self.guard()
                self.require_mission(payload)
                if (
                    self.current_research_profile() != profile
                    or not self.source_valid(payload)
                    or self.ledger.control_generation() != payload["generation"]
                    or not mission_controls_match()
                ):
                    phase, evidence = "held", None
            except (PolicyDeniedError, ValueError):
                phase, evidence = "held", None
            updated = db.execute(
                "UPDATE public_research_ops SET phase=?, evidence=? WHERE operation=? AND phase='started'",
                (phase, evidence, operation),
            )
            if updated.rowcount == 1 and phase == "completed":
                db.execute(
                    "INSERT OR IGNORE INTO public_research_completions VALUES(?,?)",
                    (operation, self.clock.now().isoformat()),
                )

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
        self.publication_guard()
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
        self.publication_guard()
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
        return self._dispatch_approved(operation, approval)

    def _dispatch_approved(self, operation, approval):
        if self.mission_evolution is None:
            with self.service.store.conversation_control_effect_fence():
                return self.service.dispatch(operation, approval, now=self.clock.now())
        with (
            self.journal.handoff_lock("mission-evolution"),
            self.service.store.conversation_control_effect_fence(),
        ):
            return self.service.dispatch(operation, approval, now=self.clock.now())

    def reconcile(self, operation, correlation_id):
        self.publication_guard()
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
