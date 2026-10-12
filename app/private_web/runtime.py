"""Explicit offline Stage 0 composition; no SDK, credentials or live effects."""

from collections.abc import Callable
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import hashlib

from app.cognition.conversation_context import MemoryConversationAssembler
from app.cognition.pipeline import CognitionEngine
from app.cognition.synthetic import SyntheticMemoryExtractor
from app.cognition.shared import SharedIntelligence
from app.cognition.shared_context import assemble_shared
from app.core.self_state_store import SelfStateStore
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
from app.research.authorization import SyntheticPublicResearchFixture
from app.research.brief import ResearchScreeningContext
from app.trusted.openai_worker_protocol import WorkerProtocolError

from .conversation import (
    ConversationService,
    HistoryItem,
    ModelState,
    MessageStatus,
    SubmittedMessage,
    _utc_now,
)


class _Snapshot:
    def __init__(self, events):
        self.events = events

    def retrieve_private_events(self, _principal):
        return self.events


class Stage0ConversationRuntime(ConversationService):
    """Synthetic-only runtime using existing privacy, provider and policy stores.

    Construction requires an exact fake transport, never an arbitrary adapter.
    The gateway's Stage 1 execution switch is used only inside this closed offline
    fixture boundary; the normal provider configuration remains inert Stage 0.
    """

    def __init__(
        self,
        event_store: EventStore,
        privacy_gate: PrivacyReadGate,
        transport: FakeOpenAITransport,
        *,
        emergency_stop: Callable[[], bool] = lambda: False,
        delivery_enabled: Callable[[], bool] = lambda: True,
        clock: Callable[[], datetime] = _utc_now,
        checkpoint: Callable[[str], None] = lambda _point: None,
        memory_extractor: SyntheticMemoryExtractor | None = None,
        shared_intelligence: SharedIntelligence | None = None,
        context_limits: ContextLimits = ContextLimits(32_000, 99),
        research_bridge: SyntheticResearchBridge | None = None,
    ):
        if type(transport) is not FakeOpenAITransport:
            raise TypeError("Stage 0 requires an exact offline fake transport")
        if privacy_gate._store is not event_store:
            raise ValueError("privacy gate must bind the application database")
        if research_bridge is not None and (
            type(research_bridge) is not SyntheticResearchBridge
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
        self._provider = ProviderStateStore(event_store)
        self._stop = emergency_stop
        self._delivery = delivery_enabled
        self._checkpoint = checkpoint
        if (
            memory_extractor is not None
            and type(memory_extractor) is not SyntheticMemoryExtractor
        ):
            raise TypeError("memory extraction requires an exact synthetic fixture")
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
        self._transport = transport
        self._clock = clock
        self._cognition = (
            None
            if memory_extractor is None
            else CognitionEngine(
                event_store,
                SelfStateStore(event_store),
                extractor=memory_extractor,
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
            if self._cognition is None and self._shared is None
            else _MemoryFakeBridge(transport)
        )
        self._gateway = TrustedOpenAIGateway(
            store=self._provider,
            transport=transport if self._memory_bridge is None else self._memory_bridge,
            config=self._config,
            emergency_stop=emergency_stop,
            utcnow=clock,
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
            "private-runtime-stage0-v1",
            self._stop(),
            self._provider.is_spend_suspended(),
            self._delivery(),
            False,
            True,
        )

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
            return super(Stage0ConversationRuntime, self).submit(
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
                if self._memory_bridge is not None:
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
                    self._config.model,
                    self._config.prompt_version,
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
                            principal, operation_id, "skipped"
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
                                principal, operation_id, "skipped"
                            )
                        )
                    else:
                        result = self._infer(context, correlation, operation_id)
                        self._checkpoint("after_model")
                        if (
                            self._research is not None
                            and self._research.enabled
                            and result.outcome is GatewayOutcome.COMPLETED
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
                    self._config.model,
                    "private-memory-stage0-v2"
                    if self._shared is None
                    else "shared-stage0-v3",
                    request.conversation.core_self_version,
                    sources,
                    shared_operations,
                )
                return self._outputs.reserve_reply(
                    principal, operation, _connection=db
                ), request
        finally:
            db.close()

    def _infer(self, context, correlation, operation_id):
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

    def _research_turn(self, route, operation, proposal):
        try:
            fixture = SyntheticPublicResearchFixture(proposal.brief)
        except ValueError:
            fixture = None
        worker_id = "research-stage0:" + (fixture.value if fixture else "denied")

        # A short fence captures current authority and sources and commits intent.
        # No privacy fence or SQLite transaction remains held across IPC.
        def admission():
            if (
                self._outputs.read_reply(route.principal, operation.operation_id)
                is None
            ):
                raise PrivacyReadBlocked("reply_sources_unavailable")
            events = self._event_store.retrieve_private_events_in_commit_order(
                route.principal, operation.initiating_event_id
            )
            base = self._research._screening
            screening = ResearchScreeningContext(
                private_canaries=base.private_canaries
                + operation.source_event_ids
                + operation.shared_operation_ids,
                participant_identifiers=base.participant_identifiers
                + (
                    route.principal.principal_id.value,
                    route.principal.authenticated_source.value,
                ),
                private_quotations=base.private_quotations,
                sensitive_details=base.sensitive_details,
                private_texts=base.private_texts
                + tuple(event.content for event in events),
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
                    fixture,
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
            if (
                caps.provider_spend_suspended
                or self._research._client._worker_state.is_spend_suspended()
            ):
                return "research_suspended"
            if (
                not caps.private_reply_drafts_enabled
                or not self._current_request.get()()
            ):
                return "research_unavailable"
            if not self._research.permits(route.principal, operation, fixture):
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
                response = self._research._client.execute(fixture)
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
                self._provider, operation, fixture, outcome, self._clock()
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
            outcome = "skipped"
        elif result.outcome is GatewayOutcome.COMPLETED:
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
        elif result.outcome is GatewayOutcome.INVALID_PROPOSAL:
            outcome = "invalid"
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
            outcome = "skipped"
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
            fixture = SyntheticPublicResearchFixture(evidence.model_identifier)
            if (
                self._research is not None
                and self._research._client._worker_state.is_spend_suspended()
            ):
                return SubmittedMessage(
                    message_status, ModelState.RESEARCH_SUSPENDED, SUSPENDED_RU
                )
            if not self._current_request.get()():
                return SubmittedMessage(message_status, ModelState.RESEARCH_UNAVAILABLE)
            if self._research is None or not self._research.permits(
                route.principal, reply.operation, fixture
            ):
                return SubmittedMessage(message_status, ModelState.RESEARCH_DENIED)
        if reply.result_state is ResultState.PERSISTED:
            caps = self._capabilities()
            if (
                caps.emergency_stop_active
                or caps.provider_spend_suspended
                or not caps.private_reply_drafts_enabled
            ):
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
            return SubmittedMessage(message_status, ModelState(resolution[0]))
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
                resolution = self._outputs.resolution(principal, operation_id)
                if resolution == ("completed", f"release:{operation_id}"):
                    route = TrustedActionContext(event.correlation_id, principal)
                    response = self._response(None, route, operation_id)
                    # The same ledger fence and source checks cover the whole rendering read.
                    if response.model_state is ModelState.COMPLETED:
                        items.append(
                            HistoryItem(
                                operation_id,
                                event.occurred_at,
                                response.response_content,
                                "agent",
                            )
                        )
            return tuple(items)

        return self._gate.authorized(read)


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
