"""Application-controlled Threads outbox and inbound polling workflows."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
import time

from .domain import (
    REQUIRED_MENTION_SCOPES,
    REQUIRED_REPLY_READ_SCOPES,
    AccountPolicy,
    Approval,
    InboundKind,
    OutboxItem,
    OutboxState,
    PollResult,
    PublicationType,
    SourceProvenance,
    Stage,
    require_aware_time,
    validate_approval,
)
from .simulator import (
    EffectClass,
    ProviderFailure,
    ReconciliationResult,
    SimulatedCrash,
    ThreadsSimulator,
    simulated_provider_id,
)
from .store import ThreadsStore, ThreadsStoreError


MAX_DISPATCH_ATTEMPTS = 2
MAX_POLL_PAGES = 5
MAX_POLL_EVENTS = 100
MAX_POLL_SECONDS = 30.0


class PolicyDeniedError(RuntimeError):
    """A deterministic capability or authorization check denied an action."""


class PollingError(RuntimeError):
    """A bounded verified-source polling invariant failed."""


class ThreadsService:
    def __init__(
        self,
        store: ThreadsStore,
        simulator: ThreadsSimulator,
        policy: AccountPolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        policy.validate()
        if simulator.account_ref != policy.account_ref:
            raise ValueError("simulator account does not match policy")
        self.store = store
        self.simulator = simulator
        self.policy = policy
        self.clock = clock
        self.utc_now = utc_now

    def propose(
        self,
        *,
        operation_id: str,
        publication_type: PublicationType,
        draft_id: str,
        content: str,
        source_provenance: SourceProvenance,
        privacy_authorized: bool,
        russian_output_authorized: bool,
        correlation_id: str,
        now: datetime,
        parent_message_id: str | None = None,
        replacement_of: str | None = None,
        replacement_approval: Approval | None = None,
    ) -> tuple[OutboxItem, bool]:
        duplicate_ref = None
        if replacement_of is not None:
            original = self.store.get(replacement_of)
            if (
                original.account_ref != self.policy.account_ref
                or original.state
                not in {
                    OutboxState.UNKNOWN,
                    OutboxState.UNKNOWN_ABANDONED,
                }
            ):
                raise PolicyDeniedError("replacement target is not unresolved")
            if replacement_approval is None:
                raise PolicyDeniedError("replacement requires authenticated approval")
            validate_approval(
                replacement_approval,
                account_ref=self.policy.account_ref,
                operation_id=operation_id,
                now=now,
                require_duplicate_ack=True,
            )
            duplicate_ref = replacement_approval.reference
        return self.store.create_proposal(
            operation_id=operation_id,
            account_ref=self.policy.account_ref,
            publication_type=publication_type,
            draft_id=draft_id,
            content=content,
            source_provenance=source_provenance,
            privacy_authorized=privacy_authorized,
            russian_output_authorized=russian_output_authorized,
            correlation_id=correlation_id,
            now=now,
            parent_message_id=parent_message_id,
            replacement_of=replacement_of,
            duplicate_risk_approval_ref=duplicate_ref,
        )

    def submit_for_approval(self, operation_id: str, *, now: datetime) -> OutboxItem:
        return self.store.transition(
            operation_id,
            OutboxState.AWAITING_APPROVAL,
            reason="policy_review_required",
            now=now,
        )

    def authorize(
        self, operation_id: str, approval: Approval, *, now: datetime
    ) -> OutboxItem:
        item = self.store.get(operation_id)
        self._check_ready_policy(item, approval, now=now)
        return self.store.transition(
            operation_id,
            OutboxState.READY,
            reason="operator_and_policy_approved",
            now=now,
            approval_reference=approval.reference,
            approval_status="approved",
        )

    def cancel(self, operation_id: str, *, now: datetime) -> OutboxItem:
        return self.store.transition(
            operation_id,
            OutboxState.CANCELLED,
            reason="cancelled_before_dispatch",
            now=now,
        )

    def dispatch(
        self, operation_id: str, approval: Approval, *, now: datetime
    ) -> OutboxItem:
        item = self.store.get(operation_id)
        if item.state is not OutboxState.READY:
            raise ThreadsStoreError("only a ready operation can be dispatched")
        self._check_ready_policy(item, approval, now=now)
        item = self.store.transition(
            operation_id,
            OutboxState.SENDING,
            reason="durable_dispatch_intent",
            now=now,
            dispatch_evidence="pre_dispatch_intent",
            increment_attempt=True,
        )
        try:
            container_id = item.provider_container_id
            if container_id is None:
                self._recheck_immediately_before_effect(item, approval, self.utc_now())
                self.store.update_sending_evidence(
                    operation_id, "container_dispatch_started", now=now
                )
                container_id = self.simulator.create_container(item)
                self.store.update_sending_evidence(
                    operation_id,
                    "container_created_publish_not_started",
                    now=now,
                    provider_container_id=container_id,
                )
            try:
                self._recheck_immediately_before_effect(item, approval, self.utc_now())
            except (PolicyDeniedError, ValueError):
                self.store.transition(
                    operation_id,
                    OutboxState.READY,
                    reason="pre_publish_policy_denial",
                    now=now,
                    provider_container_id=container_id,
                    dispatch_evidence="publish_not_transmitted",
                    reconciliation_status="not_required",
                )
                raise
            self.store.update_sending_evidence(
                operation_id,
                "publish_dispatch_started",
                now=now,
                provider_container_id=container_id,
            )
            evidence = self.simulator.publish(item, container_id)
        except SimulatedCrash as error:
            marker = (
                "transmission_possible"
                if error.transmission_possible
                else "no_transmission"
            )
            self.store.update_sending_evidence(operation_id, marker, now=now)
            raise
        except ProviderFailure as error:
            return self._record_failure(item, error, now=now)
        return self.store.transition(
            operation_id,
            OutboxState.CONFIRMED,
            reason="provider_confirmed_success",
            now=now,
            provider_container_id=container_id,
            provider_publication_id=evidence.publication_id,
            dispatch_evidence=evidence.evidence,
            reconciliation_status="not_required",
        )

    def recover_sending(self, operation_id: str, *, now: datetime) -> OutboxItem:
        item = self.store.get(operation_id)
        if item.state is not OutboxState.SENDING:
            raise ThreadsStoreError("recovery requires sending state")
        if item.dispatch_evidence in {
            "pre_dispatch_intent",
            "no_transmission",
            "container_created_publish_not_started",
        }:
            state = (
                OutboxState.READY
                if item.attempt_count < MAX_DISPATCH_ATTEMPTS
                else OutboxState.FAILED_TERMINAL
            )
            return self.store.transition(
                operation_id,
                state,
                reason="recovered_proven_pre_dispatch",
                now=now,
                dispatch_evidence="recovery_no_effect",
            )
        return self.store.transition(
            operation_id,
            OutboxState.UNKNOWN,
            reason="recovered_uncertain_dispatch",
            now=now,
            dispatch_evidence="recovery_effect_unknown",
            reconciliation_status="required",
        )

    def reconcile(self, operation_id: str, *, now: datetime) -> OutboxItem:
        item = self.store.get(operation_id)
        if item.state is not OutboxState.UNKNOWN:
            raise ThreadsStoreError("reconciliation requires unknown state")
        try:
            result = self.simulator.reconcile(item)
        except ProviderFailure:
            return self.store.note_reconciliation(
                operation_id, "attempt_failed", now=now
            )
        if result is ReconciliationResult.UNRESOLVED:
            return self.store.note_reconciliation(operation_id, "unresolved", now=now)
        if result is ReconciliationResult.REJECTED:
            return self.store.transition(
                operation_id,
                OutboxState.FAILED_TERMINAL,
                reason="reconciliation_verified_rejection",
                now=now,
                reconciliation_status="verified_rejection",
            )
        return self.store.transition(
            operation_id,
            OutboxState.CONFIRMED,
            reason="reconciliation_verified_success",
            now=now,
            provider_publication_id=simulated_provider_id("reconciled", operation_id),
            reconciliation_status="verified_success",
        )

    def abandon_unknown(self, operation_id: str, *, now: datetime) -> OutboxItem:
        return self.store.transition(
            operation_id,
            OutboxState.UNKNOWN_ABANDONED,
            reason="operator_ended_reconciliation_without_resolution",
            now=now,
            reconciliation_status="abandoned_unresolved",
        )

    def poll(
        self,
        stream: InboundKind,
        *,
        now: datetime,
        max_pages: int = MAX_POLL_PAGES,
        max_events: int = MAX_POLL_EVENTS,
        timeout_seconds: float = MAX_POLL_SECONDS,
        interrupt_after_fetch: bool = False,
    ) -> PollResult:
        self._check_poll_policy(stream)
        require_aware_time("poll time", now)
        if not 1 <= max_pages <= MAX_POLL_PAGES:
            raise ValueError("invalid poll page limit")
        if not 1 <= max_events <= MAX_POLL_EVENTS:
            raise ValueError("invalid poll event limit")
        if not 0 < timeout_seconds <= MAX_POLL_SECONDS:
            raise ValueError("invalid poll timeout")
        started = self.clock()
        cursor = self.store.cursor(self.policy.account_ref, stream)
        seen_cursors = {cursor}
        collected = []
        duplicates = 0
        fetched_count = 0
        pages = 0
        while pages < max_pages:
            if self.clock() - started >= timeout_seconds:
                raise PollingError("poll time limit reached")
            self._check_poll_policy(stream)
            page = self.simulator.poll(stream, cursor)
            if interrupt_after_fetch:
                raise SimulatedCrash(transmission_possible=False)
            if (
                not page.is_verified()
                or page.account_ref != self.policy.account_ref
                or page.source_ref != self.simulator.source_ref
                or page.requested_cursor != cursor
            ):
                raise PollingError("poll page source is not verified")
            if fetched_count + len(page.events) > max_events:
                raise PollingError("poll event limit reached")
            if page.next_cursor is not None and page.next_cursor in seen_cursors:
                raise PollingError("pagination cursor loop detected")
            inserted, duplicate_count = self.store.commit_poll_page(
                account_ref=self.policy.account_ref,
                stream=stream,
                verified_source_ref=page.source_ref,
                expected_cursor=cursor,
                next_cursor=page.next_cursor,
                events=page.events,
                now=now,
            )
            collected.extend(inserted)
            duplicates += duplicate_count
            fetched_count += len(page.events)
            pages += 1
            cursor = page.next_cursor
            if cursor is None:
                cursor = self.store.cursor(self.policy.account_ref, stream)
                break
            seen_cursors.add(cursor)
        return PollResult(tuple(collected), duplicates, pages, cursor)

    def stage_3_readiness(self) -> tuple[str, ...]:
        stage_3_policy = replace(self.policy, stage=Stage.STAGE_3)
        return stage_3_policy.publication_blockers(PublicationType.POST)

    def _check_ready_policy(
        self, item: OutboxItem, approval: Approval, *, now: datetime
    ) -> None:
        if item.account_ref != self.policy.account_ref:
            raise PolicyDeniedError("outbound account mismatch")
        validate_approval(
            approval,
            account_ref=item.account_ref,
            operation_id=item.operation_id,
            now=now,
            require_duplicate_ack=item.replacement_of is not None,
        )
        blockers = self.policy.publication_blockers(item.publication_type)
        if blockers:
            raise PolicyDeniedError("; ".join(blockers))
        unresolved = self.store.unresolved_for_account(item.account_ref)
        unresolved_ids = {candidate.operation_id for candidate in unresolved}
        if item.replacement_of is not None:
            if item.replacement_of not in unresolved_ids:
                raise PolicyDeniedError("replacement target is no longer unresolved")
            if unresolved_ids != {item.replacement_of}:
                raise PolicyDeniedError(
                    "account is paused by an additional unresolved publication"
                )
        elif unresolved_ids:
            raise PolicyDeniedError("account is paused by unresolved publication")

    def _recheck_immediately_before_effect(
        self, item: OutboxItem, approval: Approval, now: datetime
    ) -> None:
        self._check_ready_policy(item, approval, now=now)

    def _record_failure(
        self, item: OutboxItem, error: ProviderFailure, *, now: datetime
    ) -> OutboxItem:
        common = {
            "now": now,
            "provider_container_id": error.container_id,
            "dispatch_evidence": error.evidence,
        }
        if error.effect_class is EffectClass.UNCERTAIN_DISPATCH:
            return self.store.transition(
                item.operation_id,
                OutboxState.UNKNOWN,
                reason="provider_effect_uncertain",
                reconciliation_status="required",
                **common,
            )
        if error.effect_class is EffectClass.CONFIRMED_REJECTION:
            return self.store.transition(
                item.operation_id,
                OutboxState.FAILED_TERMINAL,
                reason="provider_confirmed_rejection",
                reconciliation_status="not_required",
                **common,
            )
        latest = self.store.get(item.operation_id)
        state = (
            OutboxState.READY
            if latest.attempt_count < MAX_DISPATCH_ATTEMPTS
            else OutboxState.FAILED_TERMINAL
        )
        return self.store.transition(
            item.operation_id,
            state,
            reason="proven_pre_dispatch_failure",
            reconciliation_status="not_required",
            **common,
        )

    def _check_poll_policy(self, stream: InboundKind) -> None:
        if type(stream) is not InboundKind:
            raise ValueError("invalid inbound stream")
        self.policy.validate()
        required = (
            REQUIRED_REPLY_READ_SCOPES
            if stream is InboundKind.REPLY
            else REQUIRED_MENTION_SCOPES
        )
        if (
            self.policy.stage is not Stage.SIMULATION
            or not self.policy.simulation_enabled
            or not self.policy.account_authorized
            or not required <= self.policy.scopes
            or self.policy.emergency_stop
        ):
            raise PolicyDeniedError("inbound polling capability is unavailable")
