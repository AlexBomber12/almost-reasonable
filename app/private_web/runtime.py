"""Explicit offline Stage 0 composition; no SDK, credentials or live effects."""

from collections.abc import Callable
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import hashlib
import os

from app.cognition.conversation_context import (
    MemoryConversationAssembler,
    MemoryModelRequest,
)
from app.cognition.pipeline import CognitionEngine
from app.cognition.contracts import CognitionCapabilities
from app.cognition.synthetic import SyntheticMemoryExtractor
from app.cognition.shared import SharedIntelligence
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


_WORKER_FAILURE_RU = "Сервис модели временно недоступен. Попробуйте позже."
_WORKER_STOPPED_RU = "Новые запросы к модели временно остановлены."
_WORKER_SPEND_RU = "Запросы к модели приостановлены из-за лимита расходов."
_WORKER_REJECTED_RU = "Ответ модели отклонён проверкой безопасности."


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
        if existing is None:
            self._checkpoint("before_reservation")

            def reserve():
                previous = self._outputs.read_reply(principal, operation_id)
                if previous is not None:
                    return previous, None
                if self._memory_bridge is not None or self._worker is not None:
                    return self._reserve_memory(
                        principal, event_id, correlation, operation_id
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
                    if self._cognition is not None and (
                        caps.emergency_stop_active
                        or caps.provider_spend_suspended
                        or not caps.private_reply_drafts_enabled
                    ):
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
                                route, reserved.operation, result.validation.proposal
                            )
                        else:
                            persisted = self._gate.authorized(
                                lambda: self._commit(route, reserved.operation, result)
                            )
                        if persisted:
                            self._checkpoint("after_reply_commit")
        self._checkpoint("before_release")
        return self._gate.authorized(
            lambda: self._response(admission.message_status, route, operation_id)
        )

    def _reserve_memory(self, principal, event_id, correlation, operation_id):
        db = self._event_store._open()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                self._event_store._verify_current_schema(db)
                private_limits = self._limits
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
                ).build(principal, event_id, db)
                generic_operations = request.generic_operation_ids
                shared_operations = ()
                if self._shared is not None:
                    request, shared_operations = assemble_shared(
                        request,
                        sources,
                        self._shared,
                        principal,
                        db,
                        self._limits.max_utf8_bytes,
                    )
                operation = ReplyOperation(
                    operation_id,
                    event_id,
                    "conversation:"
                    + hashlib.sha256(principal.principal_id.value.encode()).hexdigest(),
                    correlation,
                    "worker-owned" if self._worker is not None else self._config.model,
                    "worker-context-v2"
                    if self._worker is not None
                    else "private-memory-stage0-v2"
                    if self._shared is None
                    else "shared-stage0-v3",
                    request.conversation.core_self_version,
                    sources,
                    shared_operations,
                    generic_operations,
                )
                return self._outputs.reserve_reply(
                    principal, operation, _connection=db
                ), request
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
                ):
                    return "authorization_rejected"
                if self._outputs.read_reply(principal, operation_id) is None:
                    raise PrivacyReadBlocked("reply_sources_unavailable")
                return self._provider.reserve_attempt(
                    marker, "worker-ipc", self._clock()
                )

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

    @staticmethod
    def _worker_result(outcome):
        from app.core.openai_worker_client import ClientResult

        return ClientResult("", "", outcome, 0, None)

    def _research_screening(self, route, operation):
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
                base = self._research._screening
                protected_texts = tuple(
                    text
                    for text in [*(row[1] for row in rows), *shared_texts]
                    if _normalized(text)
                )
                return ResearchScreeningContext(
                    private_canaries=base.private_canaries
                    + tuple(sorted(source_ids))
                    + operation.shared_operation_ids,
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

    def _research_turn(self, route, operation, proposal):
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
            screening = self._research_screening(route, operation)
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
        self._checkpoint("after_research_intent")

        def current():
            if (
                self._outputs.read_reply(route.principal, operation.operation_id)
                is None
            ):
                raise PrivacyReadBlocked("reply_sources_unavailable")
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
                    self._research.execute(operation.operation_id)
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
        if caps.emergency_stop_active or caps.provider_spend_suspended:
            outcome = (
                ("stopped" if caps.emergency_stop_active else "spend_suspended")
                if self._worker is not None
                else "skipped"
            )
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
                    and self._historical_research_permitted(principal, reply)
                ):
                    items.append(
                        HistoryItem(
                            operation_id, event.occurred_at, reply.content, "agent"
                        )
                    )
            return tuple(items)

        return self._gate.authorized(read)

    def _historical_research_permitted(self, principal, reply):
        evidence = self._research_evidence(reply.operation.operation_id)
        if evidence is None:
            return True
        if isinstance(evidence, str) or self._research is None:
            return False
        if type(self._research) is PublicResearchBridge:
            grant = self._research._grants.get(reply.operation.operation_id)
            subject = grant.request.question if grant is not None else None
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

    def __init__(self, event_store, privacy_gate, client, *, provider_state, **kwargs):
        if type(client) is not OpenAIWorkerClient:
            raise TypeError("worker runtime requires an exact worker client")
        if client._expected_worker_uid is None:
            raise ValueError("worker runtime requires an authenticated worker UID")
        if type(provider_state) is not ProviderStateStore:
            raise TypeError("worker runtime requires an exact provider state store")
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
