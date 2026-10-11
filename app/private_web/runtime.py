"""Explicit offline Stage 0 composition; no SDK, credentials or live effects."""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime
import hashlib

from app.cognition.conversation_context import MemoryConversationAssembler
from app.cognition.pipeline import CognitionEngine
from app.cognition.synthetic import SyntheticMemoryExtractor
from app.core.self_state_store import SelfStateStore
from app.behavior.russian_gate import RussianOutputPolicy
from app.core.action_policy import ActionPolicy, CapabilityState, TrustedActionContext
from app.core.agent_output_store import AgentOutputStore, ReplyOperation, ResultState
from app.core.agent_proposals import NoActionProposal, ProposalValidation, ReplyProposal
from app.core.context import ContextLimits, PrivateContextAssembler
from app.core.model_contract import build_model_request
from app.core.provider_contract import DeploymentStage, default_openai_provider_config
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.privacy.application import PrivacyReadBlocked, PrivacyReadGate
from app.trusted.openai_gateway import GatewayOutcome, TrustedOpenAIGateway
from app.trusted.openai_transport import FakeOpenAITransport

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
        context_limits: ContextLimits = ContextLimits(32_000, 99),
    ):
        if type(transport) is not FakeOpenAITransport:
            raise TypeError("Stage 0 requires an exact offline fake transport")
        if privacy_gate._store is not event_store:
            raise ValueError("privacy gate must bind the application database")
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
            None if self._cognition is None else _MemoryFakeBridge(transport)
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
                if self._cognition is not None:
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
                        self._gate.authorized(preflight)
                    result = self._infer(context, correlation, operation_id)
                    self._checkpoint("after_model")
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
                request, sources = MemoryConversationAssembler(
                    self._event_store, self._limits
                ).build(principal, event_id, db)
                operation = ReplyOperation(
                    operation_id,
                    event_id,
                    "conversation:"
                    + hashlib.sha256(principal.principal_id.value.encode()).hexdigest(),
                    correlation,
                    self._config.model,
                    "private-memory-stage0-v2",
                    request.conversation.core_self_version,
                    sources,
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
                    if response.response_content is not None:
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
