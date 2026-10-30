"""Explicit offline Stage 0 composition; no SDK, credentials or live effects."""

from collections.abc import Callable
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import hashlib
import os
import re
from threading import Lock

from app.cognition.conversation_context import (
    FictionEpisode,
    MemoryConversationAssembler,
    MemoryModelRequest,
)
from app.cognition.pipeline import CognitionEngine
from app.cognition.contracts import CognitionCapabilities
from app.cognition.contracts import CognitionOutcomeStatus
from app.cognition.contracts import CognitionReason
from app.cognition.contracts import (
    AbstractCandidate,
    AbstractToSharedProposal,
    MemoryKind,
)
from app.cognition.worker_extraction import ExtractionModelRequest
from app.cognition.synthetic import SyntheticMemoryExtractor
from app.cognition.shared import SharedIntelligence, ReviewedSharedCandidate
from app.core.events import VerificationStatus
from app.cognition.shared_context import SharedModelRequest, assemble_shared
from app.core.self_state_store import SelfStateStore
from app.core.openai_worker_client import (
    OpenAIWorkerClient,
    CLIENT_COMMUNICATION_FAILURE,
    CLIENT_OUTCOME_UNCERTAIN,
)
from app.behavior.russian_gate import RussianOutputPolicy
from app.core.action_policy import ActionPolicy, CapabilityState, TrustedActionContext
from app.core.agent_output_store import AgentOutputStore, ReplyOperation, ResultState
from app.core.agent_proposals import (
    NoActionProposal,
    ProposalValidation,
    ReplyProposal,
    ResearchProposal,
)
from app.core.context import (
    ContextLimits,
    PrivateContextAssembler,
    minimum_context_utf8_bytes,
)
from app.core.model_contract import build_model_request
from app.core.provider_contract import DeploymentStage, default_openai_provider_config
from app.core.provider_spend import (
    ProviderStateStore,
    AttemptReservationStatus,
    ProviderPersistenceError,
)
from app.core.store import EventStore
from app.privacy.application import PrivacyReadBlocked, PrivacyReadGate
from app.trusted.openai_gateway import GatewayOutcome, TrustedOpenAIGateway
from app.trusted.openai_transport import FakeOpenAITransport

from .research_bridge import (
    FAILURE_RU,
    SUSPENDED_RU,
    SyntheticResearchBridge,
    intent_id,
    started_id,
    render_report,
)
from .public_research import PublicResearchBridge
from app.research.authorization import SyntheticPublicResearchFixture
from app.research.brief import ResearchScreeningContext, _normalized
from app.trusted.openai_worker_protocol import WorkerProtocolError

from .conversation import (
    ConversationService,
    HistoryItem,
    ModelState,
    MessageStatus,
    SubmittedMessage,
    _utc_now,
)
from .extraction_audit import ExtractionAuditStore
from .evolution_audit import EvolutionAuditStore
from .typed_audit import TypedAuditStore
from .narrative_audit import NarrativeAuditStore
from app.cognition.narrative_store import NarrativeStore, NarrativeConflict
from .shared_audit import SharedProposalAuditStore


_WORKER_FAILURE_RU = "Сервис модели временно недоступен. Попробуйте позже."
_WORKER_STOPPED_RU = "Новые запросы к модели временно остановлены."
_WORKER_SPEND_RU = "Запросы к модели приостановлены из-за лимита расходов."
_WORKER_REJECTED_RU = "Ответ модели отклонён проверкой безопасности."
_NARRATIVE_ACTIVE_LOCK = Lock()
_NARRATIVE_ACTIVE: set[tuple[int, str, str]] = set()
_NARRATIVE_SUBMITTING = ("submit",)
_NARRATIVE_SUBMIT_KEY: ContextVar[tuple | None] = ContextVar(
    "narrative_submit_key", default=None
)


def _narrative_active_key(store, operation_id):
    return (os.getpid(), str(store.database_path.resolve()), operation_id)


def _narrative_requested(content: str) -> bool:
    folded = content.casefold()
    for clause in reversed(
        re.split(
            r"[,;.!?…]+|\b(?:but|instead|however|rather|но|однако|вместо)\b", folded
        )
    ):
        if not (
            re.search(
                r"(?<!\w)(?:вымышлен[а-яё]*|сюжет[а-яё]*|story|stories|storytelling|fiction|fictional|narrative|narratives)(?!\w)",
                clause,
            )
            or re.fullmatch(
                r"\s*(?:расскажи|продолжи|продолжим)\s+истори[а-яё]*\s*",
                clause,
            )
        ):
            continue
        if re.search(
            r"(?<!\w)(?:non[\s-]*(?:fiction(?:al)?|narrative|story)|"
            r"not\s+(?:(?:be|being|a|an|the|write|tell|make)\s+){0,3}"
            r"(?:fiction(?:al)?|narrative|story)|"
            r"(?:don't|do\s+not|doesn't|does\s+not|never)\s+"
            r"(?:want|make|write|tell|include)\s+(?:(?:a|an|any)\s+)?"
            r"(?:fiction(?:al)?|narrative|story)|"
            r"(?:no|without)\s+(?:(?:a|an|any)\s+)?"
            r"(?:fiction(?:al)?|narrative|story)|"
            r"(?:не|без)[\s-]+(?:вымышлен[а-яё]*|сюжет[а-яё]*))(?!\w)",
            clause,
        ):
            return False
        if re.search(
            r"(?<!\w)(?:user|news|true|real|factual|actual)\s+(?:story|stories|narrative)(?!\w)|(?<!\w)(?:story|narrative)\s+of\b|(?<!\w)narrative\s+medicine(?!\w)",
            clause,
        ) and not re.search(r"(?<!\w)(?:fiction(?:al)?|вымышлен[а-яё]*)(?!\w)", clause):
            continue
        if re.search(
            r"(?<!\w)(?:tell|write|create|continue|invent|make|develop|resume|expand|narrate|remember|imagine|compose|draft|generate|build|want|расскажи|продолжи|продолжим|сочини|напиши|создай|измени|добавь)(?!\w)",
            clause,
        ) or re.fullmatch(
            r"\s*(?:(?:a|an)\s+)?fictional\s+(?:episode|story|narrative)\s*", clause
        ):
            return True
    return False


def _without_reviewed_question(text: str, question: str) -> str:
    """Remove only normalized-equivalent canonical spans, retaining private text."""
    needle = _normalized(question)
    if not needle:
        return text
    units: list[str] = []
    spans: list[tuple[int, int]] = []
    for index, character in enumerate(text):
        folded = " " if character.isspace() else _normalized(character)
        for unit in folded:
            if unit == " " and units and units[-1] == " ":
                spans[-1] = (spans[-1][0], index + 1)
            elif unit != " " or units:
                units.append(unit)
                spans.append((index, index + 1))
    if units and units[-1] == " ":
        units.pop()
        spans.pop()
    normalized = "".join(units)
    matches = []
    search_from = 0
    while (hit := normalized.find(needle, search_from)) != -1:
        end = hit + len(needle)
        if (
            (hit == 0 or not normalized[hit - 1].isalnum() or not needle[0].isalnum())
            and (
                end == len(normalized)
                or not normalized[end].isalnum()
                or not needle[-1].isalnum()
            )
            and (hit == 0 or spans[hit - 1][0] != spans[hit][0])
            and (end == len(normalized) or spans[end - 1][1] != spans[end][1])
        ):
            matches.append((spans[hit][0], spans[end - 1][1]))
            search_from = end
        else:
            search_from = hit + 1
    if not matches:
        return text
    pieces = []
    cursor = 0
    for start, end in matches:
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return " ".join(pieces)


class _Snapshot:
    def __init__(self, events):
        self.events = events

    def retrieve_private_events(self, _principal):
        return self.events


class _DurableConversationRuntime(ConversationService):
    """Shared privacy, reservation, policy and release lifecycle."""

    def __init__(
        self,
        event_store: EventStore,
        privacy_gate: PrivacyReadGate,
        transport: FakeOpenAITransport | OpenAIWorkerClient,
        *,
        emergency_stop: Callable[[], bool] = lambda: False,
        delivery_enabled: Callable[[], bool] = lambda: True,
        clock: Callable[[], datetime] = _utc_now,
        checkpoint: Callable[[str], None] = lambda _point: None,
        memory_extractor: SyntheticMemoryExtractor | None = None,
        cognition_capabilities: CognitionCapabilities = CognitionCapabilities(),
        shared_intelligence: SharedIntelligence | None = None,
        context_limits: ContextLimits = ContextLimits(32_000, 99),
        research_bridge: SyntheticResearchBridge | PublicResearchBridge | None = None,
        provider_state: ProviderStateStore | None = None,
    ):
        if privacy_gate._store is not event_store:
            raise ValueError("privacy gate must bind the application database")
        if research_bridge is not None and (
            type(research_bridge) not in {SyntheticResearchBridge, PublicResearchBridge}
            or research_bridge._store is not event_store
            or research_bridge._gate is not privacy_gate
        ):
            raise ValueError("research bridge must bind the same synthetic application")
        self._research = research_bridge
        self._current_request = ContextVar(
            "private_request_authorization", default=lambda: True
        )
        self._gate = privacy_gate
        self._outputs = AgentOutputStore(event_store)
        self._intellectual = None
        self._provider = provider_state or ProviderStateStore(event_store)
        self._stop = emergency_stop
        self._delivery = delivery_enabled
        self._checkpoint = checkpoint
        if (
            memory_extractor is not None
            and type(memory_extractor) is not SyntheticMemoryExtractor
        ):
            raise TypeError("memory extraction requires an exact synthetic fixture")
        if type(cognition_capabilities) is not CognitionCapabilities:
            raise TypeError("trusted cognition capabilities required")
        if shared_intelligence is not None and (
            type(shared_intelligence) is not SharedIntelligence
            or shared_intelligence._events is not event_store
            or shared_intelligence._gate is not privacy_gate
        ):
            raise ValueError(
                "shared intelligence must bind the same synthetic application"
            )
        self._shared = shared_intelligence
        self._shared_guard = shared_intelligence or SharedIntelligence(
            event_store, privacy_gate, clock=clock
        )
        self._limits = context_limits
        self._worker = transport if type(transport) is OpenAIWorkerClient else None
        self._transport = transport
        self._clock = clock
        self._cognition = (
            None
            if memory_extractor is None
            else CognitionEngine(
                event_store,
                SelfStateStore(event_store),
                extractor=memory_extractor,
                capabilities=cognition_capabilities,
                clock=clock,
                authorize=privacy_gate.authorized,
                expose_current_candidates=False,
            )
        )
        self._config = replace(
            default_openai_provider_config(),
            deployment_stage=DeploymentStage.STAGE_1,
            paid_calls_enabled=True,
            max_attempts=1,
        )
        self._memory_bridge = (
            None
            if self._worker is not None
            or (self._cognition is None and self._shared is None)
            else _MemoryFakeBridge(transport)
        )
        self._gateway = (
            None
            if self._worker is not None
            else TrustedOpenAIGateway(
                store=self._provider,
                transport=transport
                if self._memory_bridge is None
                else self._memory_bridge,
                config=self._config,
                emergency_stop=emergency_stop,
                utcnow=clock,
            )
        )
        super().__init__(
            event_store, PrivateContextAssembler(event_store, self._limits), clock=clock
        )

    @staticmethod
    def _operation_id(event_id: str) -> str:
        """Bound opaque event identity, never hash private message content."""
        return "reply:" + hashlib.sha256(event_id.encode()).hexdigest()

    @contextmanager
    def authenticated_request(self, current_authorized):
        """WSGI installs an independent current-session check for this request."""
        token = self._current_request.set(current_authorized)
        try:
            yield
        finally:
            self._current_request.reset(token)

    def _capabilities(self):
        return CapabilityState(
            "private-runtime-worker-v1"
            if self._worker is not None
            else "private-runtime-stage0-v1",
            self._stop(),
            self._provider.is_spend_suspended(),
            self._delivery(),
            False,
            True,
        )

    def _skip_outcome(self, caps):
        if self._worker is None:
            return "skipped"
        if caps.emergency_stop_active:
            return "stopped"
        if caps.provider_spend_suspended:
            return "spend_suspended"
        return "unavailable"

    def _input(self, principal, message_id, content):
        # Exact participant/message identity survives a renewed session. The original
        # authenticated source remains in the envelope; never use the latest event.
        db = self._event_store._open()
        try:
            with db:
                db.execute("BEGIN")
                self._event_store._verify_current_schema(db)
                rows = db.execute(
                    """SELECT e.event_id, e.correlation_id, p.content
                       FROM event_envelopes e LEFT JOIN private_payloads p USING(payload_id)
                       WHERE e.audience_principal_id=? AND e.source_message_id=?""",
                    (
                        principal.principal_id.value,
                        message_id,
                    ),
                ).fetchall()
                if not rows:
                    return None
                if len(rows) != 1:
                    raise PrivacyReadBlocked("ambiguous_original_input")
                row = rows[0]
                if row[2] is None:
                    raise PrivacyReadBlocked("original_input_unavailable")
                if row[2] != content:
                    raise ValueError("message identity conflict")
                return row[:2]
        finally:
            db.close()

    def submit(
        self,
        principal,
        message_id,
        content,
        *,
        evidence_event_ids: tuple[str, ...] = (),
    ) -> SubmittedMessage:
        token = _NARRATIVE_SUBMIT_KEY.set(_NARRATIVE_SUBMITTING)
        try:
            return self._submit_once(
                principal, message_id, content, evidence_event_ids=evidence_event_ids
            )
        finally:
            key = _NARRATIVE_SUBMIT_KEY.get()
            if key is not None and key != _NARRATIVE_SUBMITTING:
                with _NARRATIVE_ACTIVE_LOCK:
                    _NARRATIVE_ACTIVE.discard(key)
            _NARRATIVE_SUBMIT_KEY.reset(token)

    def _submit_once(
        self,
        principal,
        message_id,
        content,
        *,
        evidence_event_ids: tuple[str, ...] = (),
    ) -> SubmittedMessage:
        EventStore._validate_context(principal)
        self._validate_message(message_id, content)

        def admit():
            if self._input(principal, message_id, content) is not None:
                return SubmittedMessage(MessageStatus.DUPLICATE, ModelState.UNAVAILABLE)
            return super(_DurableConversationRuntime, self).submit(
                principal, message_id, content
            )

        admission = self._gate.authorized(admit)
        event_id, correlation = self._gate.authorized(
            lambda: self._input(principal, message_id, content)
        )
        operation_id = self._operation_id(event_id)
        route = TrustedActionContext(correlation, principal)
        existing = self._gate.read_reply(principal, operation_id)
        if getattr(self, "_shared_proposal_audit", None) is not None:
            self._reconcile_shared_proposal(principal, event_id, content, operation_id)
        if getattr(self, "_narrative_audit", None) is not None:
            self._reconcile_narrative(principal, event_id, content, operation_id)
        if self._intellectual is not None and existing is not None:
            self._intellectual.commit(principal, operation_id, None)
        if existing is not None and (
            getattr(self, "_extraction_audit", None) is not None
            or getattr(self, "_evolution_audit", None) is not None
            or getattr(self, "_typed_audit", None) is not None
        ):
            if getattr(self, "_evolution_audit", None) is not None:
                self._reconcile_worker_evolution(
                    principal, event_id, content, operation_id
                )
            elif getattr(self, "_extraction_audit", None) is not None:
                self._reconcile_worker_extraction(
                    principal, event_id, content, operation_id
                )
            if getattr(self, "_typed_audit", None) is not None:
                self._reconcile_worker_typed(principal, event_id, content, operation_id)
        if existing is None:
            self._checkpoint("before_reservation")

            def reserve():
                previous = self._outputs.read_reply(principal, operation_id)
                if previous is not None:
                    return previous, None
                if self._memory_bridge is not None or self._worker is not None:
                    return self._reserve_memory(
                        principal,
                        event_id,
                        correlation,
                        operation_id,
                        reserve_narrative_slot=(
                            getattr(self, "_narrative_audit", None) is not None
                            and _narrative_requested(content)
                        ),
                    )
                events = self._event_store.retrieve_private_events_in_commit_order(
                    principal, event_id
                )
                event_ids = tuple(event.event_id for event in events)
                if event_id not in event_ids:
                    raise PrivacyReadBlocked("original_input_outside_context_window")
                context = PrivateContextAssembler(
                    _Snapshot(events), self._limits
                ).build(principal)
                operation = ReplyOperation(
                    operation_id,
                    event_id,
                    "conversation:"
                    + hashlib.sha256(principal.principal_id.value.encode()).hexdigest(),
                    correlation,
                    "worker-owned" if self._worker is not None else self._config.model,
                    "worker-context-v2"
                    if self._worker is not None
                    else self._config.prompt_version,
                    context.core_self.version,
                    tuple(sorted(event.event_id for event in events)),
                )
                return self._outputs.reserve_reply(principal, operation), context

            if getattr(
                self, "_narrative_audit", None
            ) is not None and _narrative_requested(content):
                key = _narrative_active_key(
                    self._event_store,
                    "narrative:" + operation_id.removeprefix("reply:"),
                )
                with _NARRATIVE_ACTIVE_LOCK:
                    reserved, context = self._gate.authorized(reserve)
                    if reserved.reservation_created and key not in _NARRATIVE_ACTIVE:
                        _NARRATIVE_ACTIVE.add(key)
                        _NARRATIVE_SUBMIT_KEY.set(key)
            else:
                reserved, context = self._gate.authorized(reserve)
            if reserved.reservation_created:
                self._checkpoint("after_reservation")

                def preflight():
                    if self._outputs.read_reply(principal, operation_id) is None:
                        raise PrivacyReadBlocked("reply_sources_unavailable")
                    return self._capabilities()

                caps = self._gate.authorized(preflight)
                if (
                    caps.emergency_stop_active
                    or caps.provider_spend_suspended
                    or not caps.private_reply_drafts_enabled
                    or (
                        getattr(self, "_narrative_audit", None) is not None
                        and (
                            not self._narrative_permission()
                            or not self._current_request.get()()
                        )
                    )
                ):
                    self._gate.authorized(
                        lambda: self._outputs.resolve_without_reply(
                            principal, operation_id, self._skip_outcome(caps)
                        )
                    )
                else:
                    if self._cognition is not None:
                        self._checkpoint("before_extraction")
                        self._cognition.process(
                            principal, event_id, evidence_event_ids=evidence_event_ids
                        )
                        self._checkpoint("after_extraction")
                        # Deletion or changed controls during extraction block inference.
                        caps = self._gate.authorized(preflight)
                    if getattr(self, "_evolution_audit", None) is not None:
                        self._evolve_worker_memory(
                            principal, event_id, content, correlation, operation_id
                        )
                        caps = self._gate.authorized(preflight)
                    elif getattr(self, "_extraction_audit", None) is not None:
                        self._extract_worker_memory(
                            principal, event_id, content, correlation, operation_id
                        )
                        caps = self._gate.authorized(preflight)
                    if getattr(self, "_typed_audit", None) is not None:
                        self._evolve_worker_typed(
                            principal, event_id, content, correlation, operation_id
                        )
                        caps = self._gate.authorized(preflight)
                    if getattr(self, "_shared_proposal_audit", None) is not None:
                        self._propose_shared(
                            principal, event_id, content, correlation, operation_id
                        )
                        caps = self._gate.authorized(preflight)
                    narrative_blocked = False
                    if getattr(
                        self, "_narrative_audit", None
                    ) is not None and _narrative_requested(content):
                        narrative_audit = self._evolve_narrative(
                            principal,
                            event_id,
                            content,
                            correlation,
                            operation_id,
                            reserved.operation,
                            context,
                        )
                        if narrative_audit is not None:
                            try:
                                context = self._project_narrative(
                                    context,
                                    narrative_audit,
                                    self._limits.max_utf8_bytes,
                                )
                            except ValueError:
                                narrative_blocked = True
                                self._narrative_audit.finish(
                                    "narrative:" + operation_id.removeprefix("reply:"),
                                    "unavailable",
                                )
                        caps = self._gate.authorized(preflight)
                    if (
                        self._cognition is not None
                        or getattr(self, "_extraction_audit", None) is not None
                        or getattr(self, "_evolution_audit", None) is not None
                        or getattr(self, "_typed_audit", None) is not None
                        or getattr(self, "_shared_proposal_audit", None) is not None
                        or getattr(self, "_narrative_audit", None) is not None
                    ) and (
                        caps.emergency_stop_active
                        or caps.provider_spend_suspended
                        or not caps.private_reply_drafts_enabled
                        or narrative_blocked
                        or (
                            getattr(self, "_narrative_audit", None) is not None
                            and (
                                not self._narrative_permission()
                                or not self._current_request.get()()
                            )
                        )
                    ):
                        if getattr(
                            self, "_narrative_audit", None
                        ) is not None and _narrative_requested(content):
                            narrative_id = "narrative:" + operation_id.removeprefix(
                                "reply:"
                            )
                            audit = self._narrative_audit.read(
                                narrative_id,
                                principal.principal_id.value,
                                event_id,
                                content,
                            )
                            if audit is not None and audit.status == "received":
                                self._narrative_audit.finish(
                                    narrative_id, "unavailable"
                                )
                        self._gate.authorized(
                            lambda: self._outputs.resolve_without_reply(
                                principal, operation_id, self._skip_outcome(caps)
                            )
                        )
                    else:
                        result = self._infer(
                            context, correlation, operation_id, principal
                        )
                        self._checkpoint("after_model")
                        if (
                            self._research is not None
                            and self._research.enabled
                            and result.outcome == GatewayOutcome.COMPLETED
                            and isinstance(result.validation.proposal, ResearchProposal)
                        ):
                            persisted = self._research_turn(
                                route,
                                reserved.operation,
                                result.validation.proposal,
                                context=context,
                                validation=result.validation,
                            )
                        else:
                            persisted = self._gate.authorized(
                                lambda: self._commit(route, reserved.operation, result)
                            )
                        if persisted:
                            self._checkpoint("after_reply_commit")
                        if getattr(
                            self, "_narrative_audit", None
                        ) is not None and _narrative_requested(content):
                            narrative_id = "narrative:" + operation_id.removeprefix(
                                "reply:"
                            )
                            audit = self._narrative_audit.read(
                                narrative_id,
                                principal.principal_id.value,
                                event_id,
                                content,
                            )
                            if audit is not None and audit.status == "received":
                                if persisted:
                                    self._commit_narrative(
                                        principal, event_id, narrative_id, audit
                                    )
                                else:
                                    self._narrative_audit.finish(
                                        narrative_id, "unavailable"
                                    )
        self._checkpoint("before_release")
        return self._gate.authorized(
            lambda: self._response(admission.message_status, route, operation_id)
        )

    def _reserve_memory(
        self,
        principal,
        event_id,
        correlation,
        operation_id,
        *,
        reserve_narrative_slot=False,
    ):
        db = self._event_store._open()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                self._event_store._verify_current_schema(db)
                private_limits = self._limits
                if self._intellectual is not None:
                    private_limits = replace(
                        self._limits,
                        max_utf8_bytes=max(
                            minimum_context_utf8_bytes(),
                            self._limits.max_utf8_bytes
                            - min(8192, self._limits.max_utf8_bytes // 3),
                        ),
                    )
                if self._shared is not None:
                    private_limits = replace(
                        self._limits,
                        max_utf8_bytes=max(
                            minimum_context_utf8_bytes(),
                            self._limits.max_utf8_bytes - 200,
                        ),
                    )
                request, sources = MemoryConversationAssembler(
                    self._event_store, private_limits
                ).build(
                    principal,
                    event_id,
                    db,
                    reply_permitted=lambda reply: self._current_request.get()()
                    and self._shared_consent_live(reply.operation, principal)
                    and self._narrative_reply_permitted(reply.operation)
                    and self._research_evidence(reply.operation.operation_id) is None
                    and self._historical_research_permitted(principal, reply),
                    include_memories=True,
                    include_narrative=getattr(self, "_narrative_audit", None)
                    is not None,
                    reserve_narrative_slot=reserve_narrative_slot,
                )
                generic_operations = request.generic_operation_ids
                shared_operations = request.inherited_shared_operation_ids
                narrative_operations = request.narrative_operation_ids
                if self._shared is not None:
                    request, shared_operations = assemble_shared(
                        request,
                        sources,
                        self._shared,
                        principal,
                        db,
                        self._limits.max_utf8_bytes,
                    )
                if self._intellectual is not None:
                    request, sources = self._intellectual.attach(
                        db,
                        principal,
                        operation_id,
                        request,
                        sources,
                        operation_groups=(
                            shared_operations,
                            generic_operations,
                            narrative_operations,
                        ),
                        reserve_narrative_slot=reserve_narrative_slot,
                    )
                    inherited_shared, inherited_generic, inherited_narrative = (
                        self._intellectual.inherited_operations(
                            db, principal, operation_id
                        )
                    )
                    shared_operations = tuple(
                        sorted(set(shared_operations) | inherited_shared)
                    )
                    generic_operations = tuple(
                        sorted(set(generic_operations) | inherited_generic)
                    )
                    narrative_operations = tuple(
                        sorted(set(narrative_operations) | inherited_narrative)
                    )
                operation = ReplyOperation(
                    operation_id,
                    event_id,
                    "conversation:"
                    + hashlib.sha256(principal.principal_id.value.encode()).hexdigest(),
                    correlation,
                    "worker-owned" if self._worker is not None else self._config.model,
                    "worker-context-v2-transcript-v4"
                    if self._worker is not None
                    else "private-memory-stage0-v2-transcript-v4"
                    if self._shared is None
                    else "shared-stage0-v3-transcript-v4",
                    request.conversation.core_self_version,
                    sources,
                    shared_operations,
                    generic_operations,
                    narrative_operations,
                )
                reserved = self._outputs.reserve_reply(
                    principal, operation, _connection=db
                )
                if self._intellectual is not None:
                    self._intellectual.bind_context(
                        db, principal, operation_id, request
                    )
                return reserved, request
        finally:
            db.close()

    def _infer(self, context, correlation, operation_id, principal):
        if self._worker is not None:
            request = (
                context
                if type(context) in {MemoryModelRequest, SharedModelRequest}
                else build_model_request(context)
            )
            marker = "ipc:" + operation_id

            def admit_worker():
                caps = self._capabilities()
                if caps.emergency_stop_active:
                    return "emergency_stop"
                if caps.provider_spend_suspended:
                    return "spend_suspended"
                if (
                    not caps.private_reply_drafts_enabled
                    or not self._current_request.get()()
                    or (
                        getattr(self, "_narrative_audit", None) is not None
                        and not self._narrative_permission()
                    )
                ):
                    return "authorization_rejected"
                if not self._inference_consent_live(principal, operation_id):
                    return "consent_unavailable"
                if self._intellectual is not None and not self._intellectual.valid(
                    principal, operation_id
                ):
                    return "consent_unavailable"
                admitted = self._provider.reserve_attempt(
                    marker, "worker-ipc", self._clock()
                )
                if (
                    admitted is AttemptReservationStatus.CREATED
                    and self._intellectual is not None
                ):
                    with self._event_store._open() as db, db:
                        db.execute("BEGIN IMMEDIATE")
                        self._intellectual.bind_context(
                            db, principal, operation_id, request
                        )
                return admitted

            self._checkpoint("before_worker_admission")
            reservation = self._gate.authorized(admit_worker)
            if reservation is not AttemptReservationStatus.CREATED:
                return self._worker_result(
                    CLIENT_OUTCOME_UNCERTAIN
                    if reservation is AttemptReservationStatus.DUPLICATE
                    else reservation
                )
            return self._worker.infer(
                request, correlation_id=correlation, operation_id=operation_id
            )
        self._checkpoint("before_stage0_admission")
        if not self._gate.authorized(
            lambda: self._inference_consent_live(principal, operation_id)
        ):
            return self._worker_result("consent_unavailable")
        if self._memory_bridge is None:
            return self._gateway.submit(
                build_model_request(context),
                correlation_id=correlation,
                operation_id=operation_id,
            )
        # Per-execution context prevents concurrent payload substitution while the
        # same gateway retains its fail-closed persistence state across requests.
        token = self._memory_bridge.payload.set(context.serialize())
        try:
            return self._gateway.submit(
                context.conversation,
                correlation_id=correlation,
                operation_id=operation_id,
            )
        finally:
            self._memory_bridge.payload.reset(token)

    def _shared_proposal_controls(self):
        caps = self._capabilities()
        return (
            not caps.emergency_stop_active
            and not caps.provider_spend_suspended
            and caps.private_reply_drafts_enabled
            and self._current_request.get()()
        )

    def _reconcile_shared_proposal(
        self, principal, event_id, content, reply_operation_id
    ):
        operation_id = "sharedgen:" + reply_operation_id.removeprefix("reply:")
        try:
            audit = self._shared_proposal_audit.read(
                operation_id, principal.principal_id.value, event_id, content
            )
        except ValueError:
            return
        if audit is not None and audit.status == "received":
            self._commit_shared_proposal(
                principal, event_id, content, operation_id, audit
            )

    def _commit_shared_proposal(
        self, principal, event_id, content, operation_id, audit
    ):
        def commit():
            if (
                not self._shared_proposal_controls()
                or not self._shared_proposal_audit.valid(
                    principal, event_id, content, audit.snapshot
                )
            ):
                return "unavailable"
            suggestion = audit.suggestion
            if suggestion.decision == "no_change":
                return "no_change"
            selected = {
                handle: (item, revision, parent)
                for handle, item, revision, parent in audit.snapshot.selected
            }
            if suggestion.decision == "revise":
                if suggestion.handle not in selected:
                    return "rejected"
                item_id, revision, _parent = selected[suggestion.handle]
                sources = (event_id,)
            else:
                item_id = "shared-model:" + operation_id.removeprefix("sharedgen:")
                revision = 0
                sources = audit.snapshot.sources
            with self._event_store._open() as db:
                row = db.execute(
                    "SELECT occurred_at FROM event_envelopes WHERE event_id=?",
                    (event_id,),
                ).fetchone()
            if row is None:
                return "unavailable"
            candidate = ReviewedSharedCandidate(
                principal,
                AbstractToSharedProposal(
                    operation_id,
                    AbstractCandidate(
                        MemoryKind(suggestion.kind),
                        suggestion.statement,
                        suggestion.confidence,
                        VerificationStatus.UNVERIFIED,
                    ),
                    sources,
                    datetime.fromisoformat(row[0]),
                ),
                item_id,
                revision,
                self._shared_proposal_audit._audience,
            )
            return candidate

        try:
            result = self._gate.authorized(commit)
            if type(result) is ReviewedSharedCandidate:
                reason = (
                    self._shared.transition
                    if self._shared._ordinary_enabled
                    else self._shared.quarantine
                )(
                    result,
                    persistence_guard=self._shared_proposal_controls,
                    model_dependencies=audit.snapshot.dependencies,
                )
                status = (
                    "accepted"
                    if reason in {"shared_commit_disabled", operation_id}
                    else "rejected"
                )
            else:
                status = result
        except (PrivacyReadBlocked, ValueError):
            status = "unavailable"
        self._shared_proposal_audit.finish(operation_id, status)
        self._checkpoint("after_shared_proposal_commit")

    def _propose_shared(
        self, principal, event_id, content, correlation, reply_operation_id
    ):
        operation_id = "sharedgen:" + reply_operation_id.removeprefix("reply:")

        def admit():
            if not self._shared_proposal_controls():
                return None
            return self._shared_proposal_audit.reserve(
                operation_id, principal, event_id, content
            )

        try:
            audit = self._gate.authorized(admit)
        except (PrivacyReadBlocked, ValueError):
            return
        if audit is None:
            return
        if audit.request is None:
            if audit.status == "received":
                self._commit_shared_proposal(
                    principal, event_id, content, operation_id, audit
                )
            return
        reservation = self._provider.reserve_attempt(
            "ipc:" + operation_id, correlation, self._clock()
        )
        if reservation is not AttemptReservationStatus.CREATED:
            self._shared_proposal_audit.finish(operation_id, "unavailable")
            return
        try:
            allowed = self._gate.authorized(
                lambda: self._shared_proposal_controls()
                and self._shared_proposal_audit.valid(
                    principal, event_id, content, audit.snapshot
                )
            )
        except PrivacyReadBlocked:
            allowed = False
        if not allowed:
            self._shared_proposal_audit.finish(operation_id, "unavailable")
            return
        try:
            result = self._worker.infer(
                audit.request, correlation_id=correlation, operation_id=operation_id
            )
        except (TypeError, ValueError):
            self._shared_proposal_audit.finish(operation_id, "rejected")
            return
        if result.outcome == CLIENT_OUTCOME_UNCERTAIN:
            self._shared_proposal_audit.finish(operation_id, "uncertain")
            return
        if result.outcome != "completed" or result.shared is None:
            self._shared_proposal_audit.finish(
                operation_id,
                "unavailable"
                if result.outcome
                in {
                    CLIENT_COMMUNICATION_FAILURE,
                    "emergency_stop",
                    "spend_suspended",
                    "authorization_rejected",
                }
                else "rejected",
            )
            return
        try:

            def receive():
                if (
                    not self._shared_proposal_controls()
                    or not self._shared_proposal_audit.valid(
                        principal, event_id, content, audit.snapshot
                    )
                ):
                    return False
                self._shared_proposal_audit.receive(
                    operation_id, principal.principal_id.value, event_id, result.shared
                )
                return True

            received = self._gate.authorized(receive)
        except (PrivacyReadBlocked, ValueError):
            received = False
        if not received:
            self._shared_proposal_audit.finish(operation_id, "unavailable")
            return
        self._checkpoint("after_shared_proposal_receipt")
        self._commit_shared_proposal(
            principal,
            event_id,
            content,
            operation_id,
            replace(audit, status="received", suggestion=result.shared, request=None),
        )

    def _reconcile_worker_extraction(
        self, principal, event_id, content, reply_operation_id
    ):
        operation_id = "memory:" + reply_operation_id.removeprefix("reply:")
        audit = self._extraction_audit.read(
            operation_id, principal.principal_id.value, event_id, content
        )
        if audit is not None and audit.status == "received":
            self._commit_worker_memory(
                principal, event_id, operation_id, audit.proposal
            )

    def _reconcile_worker_evolution(
        self, principal, event_id, content, reply_operation_id
    ):
        operation_id = "evolution:" + reply_operation_id.removeprefix("reply:")
        audit = self._evolution_audit.read(
            operation_id, principal.principal_id.value, event_id, content
        )
        if audit is not None and audit.status == "received":
            self._commit_worker_evolution(principal, event_id, operation_id, audit)

    def _reconcile_worker_typed(self, principal, event_id, content, reply_operation_id):
        operation_id = "typed:" + reply_operation_id.removeprefix("reply:")
        audit = self._typed_audit.read(
            operation_id, principal.principal_id.value, event_id, content
        )
        if audit is not None and audit.status == "received":
            self._commit_worker_typed(principal, event_id, operation_id, audit)

    def _reconcile_narrative(self, principal, event_id, content, reply_operation_id):
        operation_id = "narrative:" + reply_operation_id.removeprefix("reply:")
        key = _narrative_active_key(self._event_store, operation_id)
        with _NARRATIVE_ACTIVE_LOCK:
            if key in _NARRATIVE_ACTIVE:
                return
            _NARRATIVE_ACTIVE.add(key)
        try:
            self._reconcile_narrative_once(
                principal, event_id, content, reply_operation_id, operation_id
            )
        finally:
            with _NARRATIVE_ACTIVE_LOCK:
                _NARRATIVE_ACTIVE.remove(key)

    def _reconcile_narrative_once(
        self, principal, event_id, content, reply_operation_id, operation_id
    ):
        try:
            audit = self._narrative_audit.read(
                operation_id, principal.principal_id.value, event_id, content
            )
        except ValueError:
            return
        if audit is None and _narrative_requested(content):

            def resolve_missing_audit():
                reply, resolution = self._outputs.read_reply_and_resolution(
                    principal, reply_operation_id
                )
                if (
                    reply is not None
                    and reply.result_state is ResultState.PENDING
                    and resolution is None
                    and not self._provider.attempt_exists("ipc:" + reply_operation_id)
                ):
                    self._outputs.resolve_without_reply(
                        principal, reply_operation_id, "unavailable"
                    )

            self._gate.authorized(resolve_missing_audit)
            return
        if audit is not None and audit.status == "reserved":
            self._narrative_audit.finish(operation_id, "unavailable")
            audit = self._narrative_audit.read(
                operation_id, principal.principal_id.value, event_id, content
            )
        if audit is not None and audit.status == "received":
            reply = self._gate.authorized(
                lambda: self._outputs.read_reply(principal, reply_operation_id)
            )
            if self._research_evidence(reply_operation_id) is not None:
                # Intent precedes research contact and persistence. A research
                # report never acknowledges the projected fictional episode.
                self._narrative_audit.finish(operation_id, "rejected")
            elif reply is not None and reply.result_state is ResultState.PERSISTED:
                self._commit_narrative(principal, event_id, operation_id, audit)
            elif reply is None or not self._provider.attempt_exists(
                "ipc:" + reply_operation_id
            ):
                self._narrative_audit.finish(operation_id, "unavailable")
            audit = self._narrative_audit.read(
                operation_id, principal.principal_id.value, event_id, content
            )
        if audit is not None and audit.status not in {"reserved", "received"}:

            def resolve_interrupted_reply():
                reply, resolution = self._outputs.read_reply_and_resolution(
                    principal, reply_operation_id
                )
                if (
                    reply is not None
                    and reply.result_state is ResultState.PENDING
                    and resolution is None
                    and not self._provider.attempt_exists("ipc:" + reply_operation_id)
                ):
                    self._outputs.resolve_without_reply(
                        principal, reply_operation_id, "unavailable"
                    )

            self._gate.authorized(resolve_interrupted_reply)

    def _narrative_current(
        self, principal, event_id, reply_operation, audit, *, committed=False
    ):
        caps = self._capabilities()
        if (
            caps.emergency_stop_active
            or caps.provider_spend_suspended
            or not caps.private_reply_drafts_enabled
            or not self._current_request.get()()
            or not self._narrative_permission()
            or not self._inference_consent_live(principal, reply_operation.operation_id)
        ):
            return False
        current_reply = self._outputs.read_reply(
            principal, reply_operation.operation_id
        )
        if current_reply is None or current_reply.operation != reply_operation:
            return False
        if audit.sources != reply_operation.source_event_ids or not {
            operation for _handle, operation in audit.selected
        } <= set(reply_operation.narrative_operation_ids):
            return False
        heads = tuple(
            sorted(
                (episode.item_id, episode.revision)
                for episode in self._narrative_store.current(principal)
            )
        )
        return (heads == audit.heads or committed) and event_id in audit.sources

    def _commit_narrative(self, principal, event_id, operation_id, audit):
        reply_operation_id = "reply:" + operation_id.removeprefix("narrative:")

        def commit():
            reply = self._outputs.read_reply(principal, reply_operation_id)
            if reply is None or reply.result_state is not ResultState.PERSISTED:
                return "unavailable"
            already = self._narrative_store.committed(
                principal,
                operation_id=operation_id,
                source_event_id=event_id,
                proposal=audit.proposal,
                selected=audit.selected,
                heads=audit.heads,
                sources=audit.sources,
            )
            if not self._narrative_current(
                principal, event_id, reply.operation, audit, committed=already
            ):
                return "unavailable"
            if already:
                self._outputs.attach_narrative(
                    principal, reply_operation_id, operation_id
                )
                return "accepted"
            proposal = audit.proposal
            if proposal.decision == "no_change":
                return "no_change"
            selected = dict(audit.selected)
            if proposal.decision == "revise":
                target = selected.get(proposal.handle)
                current = {
                    episode.operation_id: episode
                    for episode in self._narrative_store.current(principal)
                }
                if target not in current:
                    return "rejected"
                item_id = current[target].item_id
                expected_revision = current[target].revision
            else:
                item_id = "episode:" + operation_id.removeprefix("narrative:")
                expected_revision = 0
            self._write_narrative(
                principal, event_id, operation_id, audit, item_id, expected_revision
            )
            self._checkpoint("after_memory_store_commit")
            return "accepted"

        try:
            status = self._gate.authorized(commit)
        except (PrivacyReadBlocked, NarrativeConflict, ValueError):
            status = "unavailable"
        if status not in {"accepted", "no_change"}:
            status = "unavailable"
        self._narrative_audit.finish(operation_id, status)

    def _write_narrative(
        self, principal, event_id, operation_id, audit, item_id, expected_revision
    ):
        db = self._event_store._open()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                self._narrative_store.commit(
                    principal,
                    operation_id=operation_id,
                    source_event_id=event_id,
                    text=audit.proposal.episode,
                    item_id=item_id,
                    expected_revision=expected_revision,
                    expected_heads=audit.heads,
                    exposed_sources=audit.sources,
                    exposed_episodes=tuple(
                        operation for _handle, operation in audit.selected
                    ),
                    created_at=self._clock(),
                    _connection=db,
                )
                self._outputs.attach_narrative(
                    principal,
                    "reply:" + operation_id.removeprefix("narrative:"),
                    operation_id,
                    _connection=db,
                )
        finally:
            db.close()

    @staticmethod
    def _project_narrative(context, audit, max_utf8_bytes):
        proposal = audit.proposal
        if proposal is None or proposal.decision == "no_change":
            return context
        private = context.private if type(context) is SharedModelRequest else context
        episodes = list(private.episodes)
        if proposal.decision == "revise":
            target = dict(audit.selected).get(proposal.handle)
            if target not in private.projected_narrative_operation_ids:
                raise ValueError("narrative projection unavailable")
            index = private.projected_narrative_operation_ids.index(target)
            episodes[index] = FictionEpisode(
                proposal.episode, episodes[index].revision + 1
            )
        else:
            episodes.append(FictionEpisode(proposal.episode, 1))
        projected_private = replace(private, episodes=tuple(episodes))
        projected = (
            replace(context, private=projected_private)
            if type(context) is SharedModelRequest
            else projected_private
        )
        if len(projected.serialize().encode("utf-8")) > max_utf8_bytes:
            raise ValueError("narrative projection exceeds context budget")
        return projected

    def _evolve_narrative(
        self,
        principal,
        event_id,
        content,
        correlation,
        reply_operation_id,
        reply_operation,
        context,
    ):
        operation_id = "narrative:" + reply_operation_id.removeprefix("reply:")
        key = _narrative_active_key(self._event_store, operation_id)
        with _NARRATIVE_ACTIVE_LOCK:
            if key in _NARRATIVE_ACTIVE:
                if _NARRATIVE_SUBMIT_KEY.get() != key:
                    return
            else:
                _NARRATIVE_ACTIVE.add(key)
        try:
            return self._evolve_narrative_once(
                principal,
                event_id,
                content,
                correlation,
                reply_operation_id,
                reply_operation,
                context,
            )
        finally:
            if _NARRATIVE_SUBMIT_KEY.get() is None:
                with _NARRATIVE_ACTIVE_LOCK:
                    _NARRATIVE_ACTIVE.remove(key)
            else:
                _NARRATIVE_SUBMIT_KEY.set(key)

    def _evolve_narrative_once(
        self,
        principal,
        event_id,
        content,
        correlation,
        reply_operation_id,
        reply_operation,
        context,
    ):
        operation_id = "narrative:" + reply_operation_id.removeprefix("reply:")

        def admit():
            caps = self._capabilities()
            if (
                caps.emergency_stop_active
                or caps.provider_spend_suspended
                or not caps.private_reply_drafts_enabled
                or not self._current_request.get()()
                or not self._narrative_permission()
            ):
                return None
            return self._narrative_audit.reserve(
                operation_id, principal, event_id, content, reply_operation, context
            )

        try:
            audit = self._gate.authorized(admit)
        except (PrivacyReadBlocked, ValueError):
            return
        if audit is None:
            return
        if audit.request is None:
            return audit if audit.status == "received" else None
        reservation = self._provider.reserve_attempt(
            "ipc:" + operation_id, correlation, self._clock()
        )
        if reservation is not AttemptReservationStatus.CREATED:
            self._narrative_audit.finish(operation_id, "unavailable")
            return
        try:
            allowed = self._gate.authorized(
                lambda: self._narrative_current(
                    principal, event_id, reply_operation, audit
                )
            )
        except PrivacyReadBlocked:
            allowed = False
        if not allowed:
            self._narrative_audit.finish(operation_id, "unavailable")
            return
        try:
            result = self._worker.infer(
                audit.request, correlation_id=correlation, operation_id=operation_id
            )
        except (TypeError, ValueError):
            self._narrative_audit.finish(operation_id, "rejected")
            return
        if result.outcome == CLIENT_OUTCOME_UNCERTAIN:
            self._narrative_audit.finish(operation_id, "uncertain")
            return
        if result.outcome != "completed" or result.narrative is None:
            self._narrative_audit.finish(
                operation_id,
                "unavailable"
                if result.outcome
                in {
                    CLIENT_COMMUNICATION_FAILURE,
                    "emergency_stop",
                    "spend_suspended",
                    "authorization_rejected",
                }
                else "rejected",
            )
            return
        if (
            result.narrative.decision == "create" and not audit.request.create_allowed
        ) or (
            result.narrative.decision == "revise"
            and result.narrative.handle
            not in {handle for handle, _operation in audit.selected}
        ):
            self._narrative_audit.finish(operation_id, "rejected")
            return
        try:

            def receive():
                if not self._narrative_current(
                    principal, event_id, reply_operation, audit
                ):
                    return False
                self._narrative_audit.receive(
                    operation_id,
                    principal.principal_id.value,
                    event_id,
                    content,
                    result.narrative,
                )
                return True

            received = self._gate.authorized(receive)
        except (PrivacyReadBlocked, ValueError):
            received = False
        if not received:
            self._narrative_audit.finish(operation_id, "unavailable")
            return
        self._checkpoint("after_extraction_receipt")
        return replace(
            audit, status="received", proposal=result.narrative, request=None
        )

    def _commit_worker_typed(self, principal, event_id, operation_id, audit):
        def commit():
            caps = self._capabilities()
            if (
                caps.emergency_stop_active
                or caps.provider_spend_suspended
                or not self._current_request.get()()
            ):
                return "unavailable"
            outcome = self._worker_cognition.commit_worker_typed(
                principal, event_id, operation_id, audit.snapshot, audit.proposal
            )
            if outcome.reason in {
                CognitionReason.SOURCE_NOT_FOUND,
                CognitionReason.STALE_REVISION,
            }:
                return "unavailable"
            if outcome.status is CognitionOutcomeStatus.NO_CHANGE:
                return "no_change"
            return (
                "accepted"
                if outcome.status is CognitionOutcomeStatus.PERSISTED
                else "rejected"
            )

        try:
            status = self._gate.authorized(commit)
        except PrivacyReadBlocked:
            status = "unavailable"
        if status == "accepted":
            self._checkpoint("after_memory_store_commit")
        self._typed_audit.finish(operation_id, status)
        self._checkpoint("after_extraction_commit")

    def _evolve_worker_typed(
        self, principal, event_id, content, correlation, reply_operation_id
    ):
        operation_id = "typed:" + reply_operation_id.removeprefix("reply:")

        def admit():
            caps = self._capabilities()
            if (
                caps.emergency_stop_active
                or caps.provider_spend_suspended
                or not self._current_request.get()()
            ):
                return None
            return self._typed_audit.reserve(operation_id, principal, event_id, content)

        try:
            audit = self._gate.authorized(admit)
        except (PrivacyReadBlocked, ValueError):
            return
        if audit is None:
            return
        if audit.request is None:
            if audit.status == "received":
                self._commit_worker_typed(principal, event_id, operation_id, audit)
            return
        reservation = self._provider.reserve_attempt(
            "ipc:" + operation_id, correlation, self._clock()
        )
        if reservation is not AttemptReservationStatus.CREATED:
            self._typed_audit.finish(operation_id, "unavailable")
            return

        def preflight():
            caps = self._capabilities()
            if (
                caps.emergency_stop_active
                or caps.provider_spend_suspended
                or not self._current_request.get()()
            ):
                return False
            read = self._worker_cognition._read
            if read.retrieve_exact_private_event(principal, event_id) is None:
                return False
            if any(
                read.retrieve_exact_private_event(principal, source) is None
                for source in audit.snapshot.sources
            ):
                return False
            heads = tuple(
                (item.item_id, item.revision)
                for item in read.retrieve_all_private_candidates(principal)
            )
            return heads == audit.snapshot.visible

        try:
            allowed = self._gate.authorized(preflight)
        except PrivacyReadBlocked:
            allowed = False
        if not allowed:
            self._typed_audit.finish(operation_id, "unavailable")
            return
        try:
            result = self._worker.infer(
                audit.request, correlation_id=correlation, operation_id=operation_id
            )
        except (TypeError, ValueError):
            self._typed_audit.finish(operation_id, "rejected")
            return
        if result.outcome == CLIENT_OUTCOME_UNCERTAIN:
            self._typed_audit.finish(operation_id, "uncertain")
        elif result.outcome != "completed" or result.typed is None:
            status = (
                "unavailable"
                if result.outcome
                in {
                    CLIENT_COMMUNICATION_FAILURE,
                    "emergency_stop",
                    "spend_suspended",
                    "authorization_rejected",
                }
                else "rejected"
            )
            self._typed_audit.finish(operation_id, status)
        else:
            try:

                def receive():
                    caps = self._capabilities()
                    if (
                        caps.emergency_stop_active
                        or caps.provider_spend_suspended
                        or not self._current_request.get()()
                    ):
                        return False
                    self._typed_audit.receive(
                        operation_id,
                        principal.principal_id.value,
                        event_id,
                        content,
                        result.typed,
                    )
                    return True

                received = self._gate.authorized(receive)
            except (PrivacyReadBlocked, ValueError):
                received = False
            if not received:
                self._typed_audit.finish(operation_id, "unavailable")
                return
            self._checkpoint("after_extraction_receipt")
            self._commit_worker_typed(
                principal,
                event_id,
                operation_id,
                replace(audit, status="received", proposal=result.typed, request=None),
            )

    def _commit_worker_evolution(self, principal, event_id, operation_id, audit):
        def commit():
            caps = self._capabilities()
            if (
                caps.emergency_stop_active
                or caps.provider_spend_suspended
                or not self._current_request.get()()
            ):
                return "unavailable"
            outcome = self._worker_cognition.commit_worker_evolution(
                principal, event_id, operation_id, audit.snapshot, audit.proposal
            )
            if outcome.reason in {
                CognitionReason.SOURCE_NOT_FOUND,
                CognitionReason.STALE_REVISION,
            }:
                return "unavailable"
            if outcome.status is CognitionOutcomeStatus.NO_CHANGE:
                return "no_change"
            return (
                "accepted"
                if outcome.status is CognitionOutcomeStatus.PERSISTED
                else "rejected"
            )

        try:
            status = self._gate.authorized(commit)
        except PrivacyReadBlocked:
            status = "unavailable"
        if status == "accepted":
            self._checkpoint("after_memory_store_commit")
        self._evolution_audit.finish(operation_id, status)
        self._checkpoint("after_extraction_commit")

    def _evolve_worker_memory(
        self, principal, event_id, content, correlation, reply_operation_id
    ):
        operation_id = "evolution:" + reply_operation_id.removeprefix("reply:")

        def admit():
            caps = self._capabilities()
            if (
                caps.emergency_stop_active
                or caps.provider_spend_suspended
                or not self._current_request.get()()
            ):
                return None
            return self._evolution_audit.reserve(
                operation_id, principal, event_id, content
            )

        try:
            audit = self._gate.authorized(admit)
        except (PrivacyReadBlocked, ValueError):
            return
        if audit is None:
            return
        if audit.request is None:
            if audit.status == "received":
                self._commit_worker_evolution(principal, event_id, operation_id, audit)
            return
        marker = "ipc:" + operation_id
        reservation = self._provider.reserve_attempt(marker, correlation, self._clock())
        if reservation is not AttemptReservationStatus.CREATED:
            self._evolution_audit.finish(operation_id, "unavailable")
            return

        def preflight():
            caps = self._capabilities()
            if (
                caps.emergency_stop_active
                or caps.provider_spend_suspended
                or not self._current_request.get()()
            ):
                return False
            if (
                self._worker_cognition._read.retrieve_exact_private_event(
                    principal, event_id
                )
                is None
            ):
                return False
            visible = tuple(
                (read.view.item_id, read.view.revision)
                for read in self._worker_cognition._generic.current(principal)
            )
            return visible == audit.snapshot.visible

        try:
            allowed = self._gate.authorized(preflight)
        except PrivacyReadBlocked:
            allowed = False
        if not allowed:
            self._evolution_audit.finish(operation_id, "unavailable")
            return
        try:
            result = self._worker.infer(
                audit.request, correlation_id=correlation, operation_id=operation_id
            )
        except (TypeError, ValueError):
            self._evolution_audit.finish(operation_id, "rejected")
            return
        if result.outcome == CLIENT_OUTCOME_UNCERTAIN:
            self._evolution_audit.finish(operation_id, "uncertain")
        elif result.outcome != "completed" or result.evolution is None:
            status = (
                "unavailable"
                if result.outcome
                in {
                    CLIENT_COMMUNICATION_FAILURE,
                    "emergency_stop",
                    "spend_suspended",
                    "authorization_rejected",
                }
                else "rejected"
            )
            self._evolution_audit.finish(operation_id, status)
        else:
            try:

                def receive():
                    caps = self._capabilities()
                    if (
                        caps.emergency_stop_active
                        or caps.provider_spend_suspended
                        or not self._current_request.get()()
                    ):
                        return False
                    self._evolution_audit.receive(
                        operation_id,
                        principal.principal_id.value,
                        event_id,
                        content,
                        result.evolution,
                    )
                    return True

                received = self._gate.authorized(receive)
            except (PrivacyReadBlocked, ValueError):
                received = False
            if not received:
                self._evolution_audit.finish(operation_id, "unavailable")
                return
            self._checkpoint("after_extraction_receipt")
            self._commit_worker_evolution(
                principal,
                event_id,
                operation_id,
                replace(
                    audit, status="received", proposal=result.evolution, request=None
                ),
            )

    def _commit_worker_memory(self, principal, event_id, operation_id, proposal):
        def commit():
            caps = self._capabilities()
            if (
                caps.emergency_stop_active
                or caps.provider_spend_suspended
                or not self._current_request.get()()
            ):
                return "unavailable"
            if proposal.decision == "no_change":
                return "no_change"
            outcome = self._worker_cognition.commit_worker_extraction(
                principal, event_id, operation_id, proposal
            )
            if outcome.reason in {
                CognitionReason.SOURCE_NOT_FOUND,
                CognitionReason.STALE_REVISION,
            }:
                return "unavailable"
            return (
                "accepted"
                if outcome.status is CognitionOutcomeStatus.PERSISTED
                else "rejected"
            )

        try:
            status = self._gate.authorized(commit)
        except PrivacyReadBlocked:
            status = "unavailable"
        if status == "accepted":
            self._checkpoint("after_memory_store_commit")
        self._extraction_audit.finish(operation_id, status)
        self._checkpoint("after_extraction_commit")

    def _extract_worker_memory(
        self, principal, event_id, content, correlation, reply_operation_id
    ):
        operation_id = "memory:" + reply_operation_id.removeprefix("reply:")

        def admit():
            caps = self._capabilities()
            if (
                caps.emergency_stop_active
                or caps.provider_spend_suspended
                or not self._current_request.get()()
            ):
                return None
            if (
                self._worker_cognition._read.retrieve_exact_private_event(
                    principal, event_id
                )
                is None
            ):
                return None
            return self._extraction_audit.reserve(
                operation_id, principal.principal_id.value, event_id, content
            )

        audit = self._gate.authorized(admit)
        if audit is None:
            return
        if not audit.created:
            if audit.status == "received":
                self._commit_worker_memory(
                    principal, event_id, operation_id, audit.proposal
                )
            return
        request = ExtractionModelRequest(content)
        try:
            request.serialize()
        except ValueError:
            self._extraction_audit.finish(operation_id, "rejected")
            return
        marker = "ipc:" + operation_id
        reservation = self._provider.reserve_attempt(marker, correlation, self._clock())
        if reservation is not AttemptReservationStatus.CREATED:
            self._extraction_audit.finish(operation_id, "unavailable")
            return
        try:
            result = self._worker.infer(
                request,
                correlation_id=correlation,
                operation_id=operation_id,
            )
        except (TypeError, ValueError):
            self._extraction_audit.finish(operation_id, "rejected")
            return
        if result.outcome == CLIENT_OUTCOME_UNCERTAIN:
            self._extraction_audit.finish(operation_id, "uncertain")
        elif result.outcome != "completed" or result.extraction is None:
            self._extraction_audit.finish(
                operation_id,
                "unavailable"
                if result.outcome
                in {
                    CLIENT_COMMUNICATION_FAILURE,
                    "emergency_stop",
                    "spend_suspended",
                    "authorization_rejected",
                }
                else "rejected",
            )
        else:
            try:

                def receive():
                    caps = self._capabilities()
                    if (
                        caps.emergency_stop_active
                        or caps.provider_spend_suspended
                        or not self._current_request.get()()
                    ):
                        return False
                    self._extraction_audit.receive(
                        operation_id,
                        principal.principal_id.value,
                        event_id,
                        content,
                        result.extraction,
                    )
                    return True

                received = self._gate.authorized(receive)
            except (PrivacyReadBlocked, ValueError):
                received = False
            if not received:
                self._extraction_audit.finish(operation_id, "unavailable")
                return
            self._checkpoint("after_extraction_receipt")
            self._commit_worker_memory(
                principal, event_id, operation_id, result.extraction
            )

    @staticmethod
    def _worker_result(outcome):
        from app.core.openai_worker_client import ClientResult

        return ClientResult("", "", outcome, 0, None)

    def _research_screening(
        self, route, operation, canonical_question=None, context=None
    ):
        """Capture material dependencies solely for local output rejection.

        A conversation audience authorizes shared abstractions, never foreign raw
        inputs. This internal snapshot uses the already validated protected graph
        for privacy comparisons; no value is returned to a participant or worker.
        """
        db = self._event_store._open()
        try:
            with db:
                db.execute("BEGIN")
                self._event_store._verify_current_schema(db)
                reply = self._outputs._read(db, route.principal, operation.operation_id)
                if reply is None or reply.operation != operation:
                    raise PrivacyReadBlocked("reply_sources_unavailable")
                source_ids = set(operation.source_event_ids)
                shared_texts = []
                for shared in operation.shared_operation_ids:
                    source_ids.update(
                        row[0]
                        for row in db.execute(
                            "SELECT event_id FROM protected_shared_sources WHERE operation_id=?",
                            (shared,),
                        )
                    )
                    # Include exactly the selected revision's value and the
                    # authorized prior alternatives exposed in SharedModelRequest.
                    shared_texts.extend(
                        row[0]
                        for row in db.execute(
                            """SELECT DISTINCT p.value FROM shared_operations selected
                           JOIN shared_operations history ON history.item_id=selected.item_id
                             AND history.revision<=selected.revision
                           JOIN shared_payloads p ON p.operation_id=history.operation_id
                           JOIN protected_shared_audiences a ON a.operation_id=history.operation_id
                           WHERE selected.operation_id=? AND history.status='active'
                             AND a.principal_id=? AND a.purpose='conversation'""",
                            (shared, route.principal.principal_id.value),
                        )
                    )
                placeholders = ",".join("?" for _source in source_ids)
                rows = db.execute(
                    f"""SELECT e.event_id, p.content, e.audience_principal_id,
                               e.authenticated_source_ref
                        FROM event_envelopes e JOIN private_payloads p USING(payload_id)
                        WHERE e.event_id IN ({placeholders}) ORDER BY e.event_id""",
                    tuple(sorted(source_ids)),
                ).fetchall()
                context_texts = []
                private = (
                    context.private if type(context) is SharedModelRequest else context
                )
                if type(private) is MemoryModelRequest:
                    context_texts.extend(
                        segment.content for segment in private.conversation.conversation
                    )
                    context_texts.extend(episode.text for episode in private.episodes)
                    for memory in private.memories:
                        context_texts.extend(
                            value
                            for value in (
                                memory.text,
                                memory.revision_reason,
                                memory.reason_for_interest,
                                memory.rationale,
                                memory.review_condition,
                                *memory.alternatives,
                                *memory.constraints,
                            )
                            if value is not None
                        )
                base = self._research._screening
                protected_texts = tuple(
                    remaining
                    for text in [
                        *(row[1] for row in rows),
                        *shared_texts,
                        *context_texts,
                    ]
                    if _normalized(
                        remaining := (
                            _without_reviewed_question(text, canonical_question)
                            if canonical_question is not None
                            else text
                        )
                    )
                )
                private_words = {
                    word
                    for text in protected_texts
                    for word in re.findall(r"\w{16,}", text)
                }
                profile = getattr(self._research, "_profile", None)
                if profile is not None and profile.question_policy is not None:
                    # New free questions have no independent catalog review.
                    # Conservatively withhold name-shaped private tokens too.
                    functional = set(
                        "a an the this that these those what how why when where which could would should please thanks and but not for from with without you your our their его её это этот эта эти как что почему когда где который которая пожалуйста спасибо привет обсудим".split()
                    )
                    private_words.update(
                        word
                        for text in protected_texts
                        for word in re.findall(
                            r"\b(?:[A-ZА-ЯЁ][a-zа-яё]{2,}|[A-ZА-ЯЁ]{3,})\b", text
                        )
                        if word.casefold() not in functional
                    )
                return ResearchScreeningContext(
                    private_canaries=base.private_canaries
                    + tuple(sorted(source_ids))
                    + operation.shared_operation_ids
                    + operation.generic_operation_ids
                    + operation.narrative_operation_ids
                    + tuple(sorted(private_words)),
                    participant_identifiers=base.participant_identifiers
                    + tuple(
                        sorted({identifier for row in rows for identifier in row[2:]})
                    )
                    + (
                        route.principal.principal_id.value,
                        route.principal.authenticated_source.value,
                    ),
                    # Exact quotations protect even one-word sources and survive
                    # the existing percent/base64 decoding screens. Overlap still
                    # rejects recognizable excerpts of longer protected texts.
                    private_quotations=base.private_quotations
                    + base.private_texts
                    + protected_texts,
                    sensitive_details=base.sensitive_details,
                    private_texts=base.private_texts + protected_texts,
                )
        finally:
            db.close()

    def _research_turn(
        self, route, operation, proposal, *, context=None, validation=None
    ):
        public = type(self._research) is PublicResearchBridge
        if public:
            subject = proposal.brief
            worker_id = operation.operation_id
        else:
            try:
                subject = SyntheticPublicResearchFixture(proposal.brief)
            except ValueError:
                subject = None
            worker_id = "research-stage0:" + (subject.value if subject else "denied")

        # A short fence captures current authority and sources and commits intent.
        # No privacy fence or SQLite transaction remains held across IPC.
        def admission():
            if self._intellectual is not None:
                self._intellectual.commit(
                    route.principal, operation.operation_id, validation
                )
            canonical = None
            if (
                public
                and self._research._profile is not None
                and self._research._profile.question_policy is None
            ):
                topic = self._research._profile.topic(subject)
                canonical = topic.question if topic is not None else None
            screening = self._research_screening(route, operation, canonical, context)
            if (
                public
                and self._research._profile is not None
                and self._research._profile.question_policy is not None
                and self._research._profile.topic(subject) is not None
            ):
                from app.research.brief import (
                    build_safe_research_brief,
                    ResearchRecordSet,
                    ResearchQuestion,
                )
                from .research_questions import bind

                built = build_safe_research_brief(
                    ResearchRecordSet((ResearchQuestion(subject),)),
                    result_budget=3,
                    trusted_config_version="public-research-brief-v1",
                    screening=screening,
                )
                if built.brief is not None and built.brief.question == subject:
                    with self._event_store._open() as db, db:
                        db.execute("BEGIN IMMEDIATE")
                        bind(
                            db,
                            operation.operation_id,
                            subject,
                            self._research._profile.digest,
                        )
            return self._provider.reserve_attempt(
                intent_id(operation.operation_id), worker_id, self._clock()
            ), screening

        reservation, screening = self._gate.authorized(admission)
        if reservation is AttemptReservationStatus.SPEND_SUSPENDED:

            def suspended():
                self._research.record(
                    self._provider,
                    operation,
                    subject,
                    "research_suspended",
                    self._clock(),
                    require_reservation=False,
                )
                self._outputs.resolve_without_reply(
                    route.principal, operation.operation_id, "failed"
                )

            self._gate.authorized(suspended)
            return False
        if reservation is not AttemptReservationStatus.CREATED:
            return False
        if getattr(self, "_narrative_audit", None) is not None:
            narrative_id = "narrative:" + operation.operation_id.removeprefix("reply:")
            if (
                self._narrative_audit.status(
                    narrative_id,
                    route.principal.principal_id.value,
                    operation.initiating_event_id,
                )
                == "received"
            ):
                self._narrative_audit.finish(narrative_id, "rejected")
        self._checkpoint("after_research_intent")

        def current():
            if (
                self._outputs.read_reply(route.principal, operation.operation_id)
                is None
            ):
                raise PrivacyReadBlocked("reply_sources_unavailable")
            if not self._shared_consent_live(operation, route.principal):
                return "research_unavailable"
            caps = self._capabilities()
            if caps.emergency_stop_active:
                return "research_stopped"
            if caps.provider_spend_suspended or (
                not public and self._research._client._worker_state.is_spend_suspended()
            ):
                return "research_suspended"
            if (
                not caps.private_reply_drafts_enabled
                or not self._current_request.get()()
                or (
                    getattr(self, "_narrative_audit", None) is not None
                    and not self._narrative_permission()
                )
            ):
                return "research_unavailable"
            if self._intellectual is not None and not self._intellectual.valid(
                route.principal, operation.operation_id
            ):
                return "research_unavailable"
            if not self._research.permits(route.principal, operation, subject):
                return "research_denied"
            return None

        outcome = self._gate.authorized(current)
        text = None
        if outcome is None:
            self._checkpoint("before_research_ipc")

            def start():
                blocked = current()
                if blocked is not None:
                    return blocked
                reserved = self._provider.reserve_attempt(
                    started_id(operation.operation_id), worker_id, self._clock()
                )
                return (
                    None
                    if reserved is AttemptReservationStatus.CREATED
                    else "research_uncertain"
                )

            outcome = self._gate.authorized(start)
        if outcome is None:
            self._checkpoint("after_research_started")
            # The started marker is conservative: crashes from this point may
            # have consumed the independent worker. Never infer completion.
            outcome = self._gate.authorized(current)
        if outcome is None:
            try:
                response = (
                    self._research.execute(operation.operation_id, subject)
                    if public
                    else self._research._client.execute(subject)
                )
            except (OSError, WorkerProtocolError):
                # Contact may have consumed the worker identity. Never resend.
                response = None
            self._checkpoint("after_research_ipc")
            if response is None or response["outcome"] in {
                "duplicate_operation",
                "persistence_failure",
            }:
                outcome = "research_uncertain"
            elif response["outcome"] == "completed":
                text = render_report(response["report"], screening)
                outcome = "completed" if text is not None else "research_rejected"
            else:
                outcome = {
                    "authorization_denied": "research_denied",
                    "disabled": "research_unavailable",
                    "emergency_stop": "research_stopped",
                    "spend_suspended": "research_suspended",
                    "spend_limit_confirmed": "research_suspended",
                    "invalid_response": "research_rejected",
                    "output_gate_rejected": "research_rejected",
                }.get(response["outcome"], "research_failed")

        def record():
            nonlocal outcome
            blocked = current()
            if blocked is not None:
                outcome = blocked
            return self._research.record(
                self._provider, operation, subject, outcome, self._clock()
            )

        if not self._gate.authorized(record):
            return False
        self._checkpoint("after_research_result_record")

        def persist():
            # Result evidence and reply payload have separate transactions. A
            # crash/control change in between retains an honest held result.
            if outcome == "completed":
                if current() is not None:
                    return False
                validation = ProposalValidation(ReplyProposal(text), None)
                caps = self._capabilities()
                grant = ActionPolicy().evaluate(validation, route, caps)
                self._outputs.persist_reply(route, operation, grant, caps)
                return True
            if outcome != "research_uncertain":
                self._outputs.resolve_without_reply(
                    route.principal, operation.operation_id, "failed"
                )
            return False

        return self._gate.authorized(persist)

    def _research_evidence(self, operation_id):
        identifier = intent_id(operation_id)
        if not self._provider.attempt_exists(identifier):
            return None
        try:
            return self._provider.usage_attempt(identifier)
        except ProviderPersistenceError:
            # Reservation without an outcome, or unreadable evidence: fail closed.
            return (
                "research_uncertain"
                if self._provider.attempt_exists(started_id(operation_id))
                else "research_pending"
            )

    def _commit(self, route, operation, result):
        caps = self._capabilities()
        if self._intellectual is not None and not self._intellectual.valid(
            route.principal, operation.operation_id
        ):
            self._outputs.resolve_without_reply(
                route.principal, operation.operation_id, "unavailable"
            )
            return
        if (
            self._intellectual is not None
            and result.outcome == GatewayOutcome.COMPLETED
            and self._current_request.get()()
            and not caps.emergency_stop_active
            and not caps.provider_spend_suspended
        ):
            self._intellectual.commit(
                route.principal, operation.operation_id, result.validation
            )
            self._checkpoint("after_intellectual_commit")
        if caps.emergency_stop_active or caps.provider_spend_suspended:
            outcome = (
                ("stopped" if caps.emergency_stop_active else "spend_suspended")
                if self._worker is not None
                else "skipped"
            )
        elif result.outcome == "consent_unavailable":
            outcome = "unavailable"
        elif not self._shared_consent_live(operation, route.principal):
            outcome = "unavailable"
        elif (
            getattr(self, "_narrative_audit", None) is not None
            and not self._narrative_permission()
        ):
            outcome = "unavailable"
        elif self._worker is not None and not self._current_request.get()():
            return
        elif self._worker is not None and result.outcome == CLIENT_OUTCOME_UNCERTAIN:
            return
        elif (
            self._worker is not None and result.outcome == CLIENT_COMMUNICATION_FAILURE
        ):
            outcome = "unavailable"
        elif result.outcome == GatewayOutcome.COMPLETED:
            decision = ActionPolicy().evaluate(result.validation, route, caps)
            if isinstance(decision.proposal, NoActionProposal):
                outcome = "no_action"
            elif not decision.dispatchable:
                outcome = "unsupported"
            else:
                gated = RussianOutputPolicy(max_regenerations=0).deliver(
                    decision.proposal.content
                )
                validation = ProposalValidation(ReplyProposal(gated.text), None)
                grant = ActionPolicy().evaluate(validation, route, self._capabilities())
                self._outputs.persist_reply(
                    route, operation, grant, self._capabilities()
                )
                return True
        elif result.outcome == GatewayOutcome.INVALID_PROPOSAL:
            outcome = "rejected" if self._worker is not None else "invalid"
        elif result.outcome in {
            GatewayOutcome.DUPLICATE_OPERATION,
            GatewayOutcome.PERSISTENCE_FAILURE,
        }:
            # Unknown provider outcome is never converted into permission to retry.
            return
        elif result.outcome in {
            GatewayOutcome.EMERGENCY_STOP,
            GatewayOutcome.SPEND_SUSPENDED,
        }:
            outcome = (
                (
                    "stopped"
                    if result.outcome == GatewayOutcome.EMERGENCY_STOP
                    else "spend_suspended"
                )
                if self._worker is not None
                else "skipped"
            )
        elif self._worker is not None and result.outcome in {
            GatewayOutcome.DISABLED,
            "authorization_rejected",
            "invalid_ipc_request",
            GatewayOutcome.INVALID_RESPONSE,
            GatewayOutcome.INVALID_REQUEST,
            GatewayOutcome.REFUSED,
            GatewayOutcome.INCOMPLETE,
            GatewayOutcome.UNSUPPORTED_OUTPUT,
        }:
            outcome = (
                "unavailable"
                if result.outcome == GatewayOutcome.DISABLED
                else "rejected"
            )
        else:
            outcome = "failed"
        self._outputs.resolve_without_reply(
            route.principal, operation.operation_id, outcome
        )

    def _response(self, message_status, route, operation_id):
        reply, resolution = self._outputs.read_reply_and_resolution(
            route.principal, operation_id
        )
        if reply is None:
            raise PrivacyReadBlocked("reply_sources_unavailable")
        if not self._shared_consent_live(reply.operation, route.principal):
            return SubmittedMessage(message_status, ModelState.UNAVAILABLE)
        if not self._narrative_reply_permitted(reply.operation):
            return SubmittedMessage(message_status, ModelState.UNAVAILABLE)
        if (
            getattr(self, "_narrative_audit", None) is not None
            and not self._narrative_permission()
        ):
            return SubmittedMessage(message_status, ModelState.UNAVAILABLE)
        if self._intellectual is not None and not self._intellectual.valid(
            route.principal,
            operation_id,
            revisions=resolution != ("completed", f"release:{operation_id}"),
        ):
            return SubmittedMessage(message_status, ModelState.UNAVAILABLE)
        evidence = self._research_evidence(operation_id)
        if evidence is not None:
            if isinstance(evidence, str) or reply.result_state is ResultState.PENDING:
                state = (
                    evidence
                    if isinstance(evidence, str)
                    else "research_uncertain"
                    if evidence.outcome == "completed"
                    else evidence.outcome
                )
                message = {
                    "research_unavailable": FAILURE_RU,
                    "research_suspended": SUSPENDED_RU,
                    "research_stopped": SUSPENDED_RU,
                    "research_failed": FAILURE_RU,
                    "research_rejected": FAILURE_RU,
                }.get(state)
                return SubmittedMessage(message_status, ModelState(state), message)
            if self._research is None:
                return SubmittedMessage(message_status, ModelState.RESEARCH_DENIED)
            public = type(self._research) is PublicResearchBridge
            if public:
                grant = self._research._grants.get(operation_id)
                subject = grant.request.question if grant is not None else None
                if self._research._profile is not None:
                    subject = self._research.evidence_choice(
                        evidence.model_identifier, operation_id=operation_id
                    )
            else:
                try:
                    subject = SyntheticPublicResearchFixture(evidence.model_identifier)
                except ValueError:
                    return SubmittedMessage(message_status, ModelState.RESEARCH_DENIED)
            if (
                self._research is not None
                and not public
                and self._research._client._worker_state.is_spend_suspended()
            ):
                return SubmittedMessage(
                    message_status, ModelState.RESEARCH_SUSPENDED, SUSPENDED_RU
                )
            if not self._current_request.get()():
                return SubmittedMessage(message_status, ModelState.RESEARCH_UNAVAILABLE)
            if self._research is None or not self._research.permits(
                route.principal, reply.operation, subject
            ):
                return SubmittedMessage(message_status, ModelState.RESEARCH_DENIED)
        if reply.result_state is ResultState.PERSISTED:
            if getattr(self, "_narrative_audit", None) is not None:
                active_key = _narrative_active_key(
                    self._event_store,
                    "narrative:" + operation_id.removeprefix("reply:"),
                )
                with _NARRATIVE_ACTIVE_LOCK:
                    if (
                        active_key in _NARRATIVE_ACTIVE
                        and _NARRATIVE_SUBMIT_KEY.get() != active_key
                    ):
                        return SubmittedMessage(message_status, ModelState.PENDING)
            if self._worker is not None and not self._current_request.get()():
                return SubmittedMessage(message_status, ModelState.UNAVAILABLE)
            caps = self._capabilities()
            if (
                caps.emergency_stop_active
                or caps.provider_spend_suspended
                or not caps.private_reply_drafts_enabled
            ):
                if self._worker is not None:
                    return SubmittedMessage(
                        message_status,
                        ModelState(self._skip_outcome(caps)),
                    )
                return SubmittedMessage(message_status, ModelState.SKIPPED)
            narrative_audit = getattr(self, "_narrative_audit", None)
            if narrative_audit is None and self._worker is not None:
                narrative_audit = NarrativeAuditStore(self._provider, self._event_store)
            if narrative_audit is not None:
                narrative_id = "narrative:" + operation_id.removeprefix("reply:")
                status = narrative_audit.status(
                    narrative_id,
                    route.principal.principal_id.value,
                    reply.operation.initiating_event_id,
                )
                if status in {"received", "withheld"} or (
                    status == "accepted"
                    and narrative_id not in reply.operation.narrative_operation_ids
                ):
                    return SubmittedMessage(message_status, ModelState.UNAVAILABLE)
            validation = ProposalValidation(ReplyProposal(reply.content), None)
            grant = ActionPolicy().evaluate(validation, route, caps)
            self._outputs.release_reply(
                route,
                reply.operation,
                grant,
                self._capabilities(),
                f"release:{operation_id}",
            )
            return SubmittedMessage(message_status, ModelState.COMPLETED, reply.content)
        if resolution is not None:
            state = ModelState(resolution[0])
            message = (
                {
                    ModelState.UNAVAILABLE: _WORKER_FAILURE_RU,
                    ModelState.STOPPED: _WORKER_STOPPED_RU,
                    ModelState.SPEND_SUSPENDED: _WORKER_SPEND_RU,
                    ModelState.REJECTED: _WORKER_REJECTED_RU,
                    ModelState.FAILED: _WORKER_FAILURE_RU,
                }.get(state)
                if self._worker is not None
                else None
            )
            return SubmittedMessage(message_status, state, message)
        if self._worker is not None and self._provider.attempt_exists(
            "ipc:" + operation_id
        ):
            return SubmittedMessage(message_status, ModelState.UNCERTAIN)
        return SubmittedMessage(message_status, ModelState.PENDING)

    def history(self, principal) -> tuple[HistoryItem, ...]:
        def read():
            items = []
            for event in self._event_store.retrieve_private_events_in_commit_order(
                principal
            ):
                items.append(
                    HistoryItem(event.event_id, event.occurred_at, event.content)
                )
                operation_id = self._operation_id(event.event_id)
                reply, resolution = self._outputs.read_reply_and_resolution(
                    principal, operation_id
                )
                # A held payload is never evidence of release. The scoped storage
                # read also validates every live private/shared/generic dependency.
                if (
                    reply is not None
                    and reply.result_state is ResultState.PERSISTED
                    and resolution == ("completed", f"release:{operation_id}")
                    and self._current_request.get()()
                    and (
                        getattr(self, "_narrative_audit", None) is None
                        or self._narrative_permission()
                    )
                    and self._shared_consent_live(reply.operation, principal)
                    and self._narrative_reply_permitted(reply.operation)
                    and self._historical_research_permitted(principal, reply)
                ):
                    items.append(
                        HistoryItem(
                            operation_id, event.occurred_at, reply.content, "agent"
                        )
                    )
            return tuple(items)

        return self._gate.authorized(read)

    def _shared_consent_live(self, operation, principal):
        return (
            not operation.shared_operation_ids
            or self._shared_guard.operations_consent_live(
                operation.shared_operation_ids, principal
            )
        )

    def _narrative_reply_permitted(self, operation):
        return not operation.narrative_operation_ids or (
            getattr(self, "_narrative_audit", None) is not None
            and self._narrative_permission()
        )

    def _inference_consent_live(self, principal, operation_id):
        """Call only within the privacy fence, immediately before inference admission."""
        reply = self._outputs.read_reply(principal, operation_id)
        if reply is None:
            raise PrivacyReadBlocked("reply_sources_unavailable")
        return self._shared_consent_live(reply.operation, principal)

    def _historical_research_permitted(self, principal, reply):
        evidence = self._research_evidence(reply.operation.operation_id)
        if evidence is None:
            return True
        if isinstance(evidence, str) or self._research is None:
            return False
        if type(self._research) is PublicResearchBridge:
            grant = self._research._grants.get(reply.operation.operation_id)
            subject = grant.request.question if grant is not None else None
            if self._research._profile is not None:
                subject = self._research.evidence_choice(
                    evidence.model_identifier, operation_id=reply.operation.operation_id
                )
        else:
            try:
                subject = SyntheticPublicResearchFixture(evidence.model_identifier)
            except ValueError:
                return False
        return self._research.permits(principal, reply.operation, subject)


class _MemoryFakeBridge:
    """Private Stage 0 adapter; never installed in the provider or worker protocol."""

    def __init__(self, transport):
        self._transport = transport
        self.payload = ContextVar("private_memory_fake_payload")

    def call(self, request, *, timeout_seconds):
        return self._transport.call(
            replace(request, input_text=self.payload.get()),
            timeout_seconds=timeout_seconds,
        )


class Stage0ConversationRuntime(_DurableConversationRuntime):
    """Exact fake-only offline fixture; never accepts an arbitrary adapter."""

    def __init__(self, event_store, privacy_gate, transport, **kwargs):
        if type(transport) is not FakeOpenAITransport:
            raise TypeError("Stage 0 requires an exact offline fake transport")
        if "provider_state" in kwargs:
            raise ValueError("Stage 0 provider state is bound to its application store")
        if type(kwargs.get("research_bridge")) is PublicResearchBridge:
            raise ValueError("Stage 0 cannot admit public research")
        super().__init__(event_store, privacy_gate, transport, **kwargs)


class WorkerConversationRuntime(_DurableConversationRuntime):
    """Explicit application-only worker connection using durable reply policy."""

    def __init__(
        self,
        event_store,
        privacy_gate,
        client,
        *,
        provider_state,
        memory_extraction_enabled=False,
        memory_evolution_enabled=False,
        typed_memory_enabled=False,
        narrative_enabled=False,
        narrative_permission_current=None,
        shared_proposals_enabled=False,
        shared_proposal_audience=(),
        **kwargs,
    ):
        if type(client) is not OpenAIWorkerClient:
            raise TypeError("worker runtime requires an exact worker client")
        if client._expected_worker_uid is None:
            raise ValueError("worker runtime requires an authenticated worker UID")
        if type(provider_state) is not ProviderStateStore:
            raise TypeError("worker runtime requires an exact provider state store")
        if type(memory_extraction_enabled) is not bool:
            raise TypeError("memory_extraction_enabled must be bool")
        if type(memory_evolution_enabled) is not bool:
            raise TypeError("memory_evolution_enabled must be bool")
        if type(typed_memory_enabled) is not bool:
            raise TypeError("typed_memory_enabled must be bool")
        if type(narrative_enabled) is not bool:
            raise TypeError("narrative_enabled must be bool")
        if narrative_enabled and not callable(narrative_permission_current):
            raise TypeError("narrative permission check required")
        if type(shared_proposals_enabled) is not bool:
            raise TypeError("shared_proposals_enabled must be bool")
        if shared_proposals_enabled and kwargs.get("shared_intelligence") is None:
            raise ValueError("shared proposals require shared intelligence")
        if os.path.samefile(
            provider_state._event_store.database_path, event_store.database_path
        ):
            raise ValueError("worker provider state must use a separate database")
        if (
            kwargs.get("research_bridge") is not None
            and type(kwargs["research_bridge"]) is not PublicResearchBridge
        ) or kwargs.get("memory_extractor") is not None:
            raise ValueError("worker conversation cannot run synthetic effects")
        super().__init__(
            event_store, privacy_gate, client, provider_state=provider_state, **kwargs
        )
        from .intellectual import PrivateIntellectualState

        self._intellectual = PrivateIntellectualState(self)
        self._extraction_audit = (
            ExtractionAuditStore(provider_state, event_store)
            if memory_extraction_enabled and not memory_evolution_enabled
            else None
        )
        self._evolution_audit = (
            EvolutionAuditStore(provider_state, event_store)
            if memory_evolution_enabled
            else None
        )
        self._typed_audit = (
            TypedAuditStore(provider_state, event_store)
            if typed_memory_enabled
            else None
        )
        self._narrative_audit = (
            NarrativeAuditStore(provider_state, event_store)
            if narrative_enabled
            else None
        )
        self._narrative_store = (
            NarrativeStore(event_store) if narrative_enabled else None
        )
        self._narrative_permission = narrative_permission_current
        self._shared_proposal_audit = (
            SharedProposalAuditStore(
                provider_state,
                event_store,
                self._shared,
                shared_proposal_audience,
                self._clock,
            )
            if shared_proposals_enabled
            else None
        )
        self._worker_cognition = (
            CognitionEngine(
                event_store,
                SelfStateStore(event_store),
                capabilities=CognitionCapabilities(
                    private_state_write=typed_memory_enabled,
                    shared_abstraction_screening=False,
                    generic_memory_write=(
                        memory_extraction_enabled or memory_evolution_enabled
                    ),
                    private_consolidation=memory_evolution_enabled,
                ),
                clock=self._clock,
                authorize=privacy_gate.authorized,
                expose_current_candidates=False,
            )
            if memory_extraction_enabled
            or memory_evolution_enabled
            or typed_memory_enabled
            else None
        )
