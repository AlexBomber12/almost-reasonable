"""Deterministic, zero-network simulator for documented Threads API stages."""

from dataclasses import dataclass
from enum import StrEnum

from .domain import InboundEvent, InboundKind, OutboxItem


_VERIFIED_SOURCE_AUTHORITY = object()


class EffectClass(StrEnum):
    PROVEN_PRE_DISPATCH = "proven_pre_dispatch"
    UNCERTAIN_DISPATCH = "uncertain_dispatch"
    CONFIRMED_REJECTION = "confirmed_rejection"


class Fault(StrEnum):
    NONE = "none"
    BEFORE_CONTAINER = "before_container"
    DURING_CONTAINER = "during_container"
    AFTER_CONTAINER = "after_container"
    CONTAINER_REJECTED = "container_rejected"
    BEFORE_PUBLISH = "before_publish"
    DURING_PUBLISH = "during_publish"
    AFTER_PUBLISH_SUCCESS = "after_publish_success"
    PUBLISH_REJECTED = "publish_rejected"
    RESPONSE_PARSE = "response_parse"
    CRASH_BEFORE_TRANSMISSION = "crash_before_transmission"
    CRASH_AFTER_TRANSMISSION = "crash_after_transmission"
    STOP_IN_FLIGHT = "stop_in_flight"
    RECONCILIATION = "reconciliation"
    POLL = "poll"


class ReconciliationResult(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"


class ProviderFailure(RuntimeError):
    def __init__(
        self,
        effect_class: EffectClass,
        evidence: str,
        *,
        container_id: str | None = None,
    ):
        super().__init__(evidence)
        self.effect_class = effect_class
        self.evidence = evidence
        self.container_id = container_id


class SimulatedCrash(RuntimeError):
    def __init__(self, *, transmission_possible: bool):
        super().__init__("simulated process interruption")
        self.transmission_possible = transmission_possible


@dataclass(frozen=True, slots=True)
class PublishEvidence:
    publication_id: str
    evidence: str


@dataclass(frozen=True, slots=True)
class VerifiedPollPage:
    account_ref: str
    source_ref: str
    requested_cursor: str | None
    next_cursor: str | None
    events: tuple[InboundEvent, ...]
    _authority: object

    def is_verified(self) -> bool:
        return self._authority is _VERIFIED_SOURCE_AUTHORITY


class ThreadsSimulator:
    """Models container creation, publication, reconciliation, and polling."""

    def __init__(self, *, account_ref: str, source_ref: str = "meta-simulator-v1"):
        self.account_ref = account_ref
        self.source_ref = source_ref
        self.faults: dict[tuple[str, str], Fault] = {}
        self.reconciliation: dict[str, ReconciliationResult] = {}
        self.pages: dict[tuple[InboundKind, str | None], tuple[str | None, tuple]] = {}
        self.trace: list[tuple[str, str]] = []
        self.network_calls = 0
        self.on_in_flight = None

    def set_fault(self, operation_id: str, stage: str, fault: Fault) -> None:
        if type(fault) is not Fault:
            raise ValueError("invalid simulator fault")
        self.faults[(operation_id, stage)] = fault

    def create_container(self, item: OutboxItem) -> str:
        fault = self.faults.get((item.operation_id, "container"), Fault.NONE)
        if fault is Fault.CRASH_BEFORE_TRANSMISSION:
            raise SimulatedCrash(transmission_possible=False)
        if fault is Fault.BEFORE_CONTAINER:
            raise ProviderFailure(
                EffectClass.PROVEN_PRE_DISPATCH, "container_not_transmitted"
            )
        self.trace.append(("create_container", item.operation_id))
        if fault is Fault.CRASH_AFTER_TRANSMISSION:
            raise SimulatedCrash(transmission_possible=True)
        if fault is Fault.DURING_CONTAINER:
            raise ProviderFailure(
                EffectClass.UNCERTAIN_DISPATCH, "container_acceptance_unknown"
            )
        if fault is Fault.CONTAINER_REJECTED:
            raise ProviderFailure(EffectClass.CONFIRMED_REJECTION, "container_rejected")
        container_id = f"container-{item.operation_id}"
        if fault is Fault.AFTER_CONTAINER:
            raise ProviderFailure(
                EffectClass.UNCERTAIN_DISPATCH,
                "container_response_lost",
                container_id=container_id,
            )
        return container_id

    def publish(self, item: OutboxItem, container_id: str) -> PublishEvidence:
        fault = self.faults.get((item.operation_id, "publish"), Fault.NONE)
        if fault is Fault.CRASH_BEFORE_TRANSMISSION:
            raise SimulatedCrash(transmission_possible=False)
        if fault is Fault.BEFORE_PUBLISH:
            raise ProviderFailure(
                EffectClass.PROVEN_PRE_DISPATCH,
                "publish_not_transmitted",
                container_id=container_id,
            )
        self.trace.append(("publish_container", container_id))
        if fault is Fault.CRASH_AFTER_TRANSMISSION:
            raise SimulatedCrash(transmission_possible=True)
        if fault is Fault.DURING_PUBLISH:
            raise ProviderFailure(
                EffectClass.UNCERTAIN_DISPATCH,
                "publish_acceptance_unknown",
                container_id=container_id,
            )
        if fault is Fault.PUBLISH_REJECTED:
            raise ProviderFailure(
                EffectClass.CONFIRMED_REJECTION,
                "publish_rejected",
                container_id=container_id,
            )
        publication_id = f"thread-{item.operation_id}"
        if fault is Fault.STOP_IN_FLIGHT:
            if self.on_in_flight is not None:
                self.on_in_flight()
        if fault in {Fault.AFTER_PUBLISH_SUCCESS, Fault.RESPONSE_PARSE}:
            evidence = (
                "publish_response_lost"
                if fault is Fault.AFTER_PUBLISH_SUCCESS
                else "publish_response_malformed"
            )
            raise ProviderFailure(
                EffectClass.UNCERTAIN_DISPATCH,
                evidence,
                container_id=container_id,
            )
        return PublishEvidence(publication_id, "provider_publish_id_verified")

    def reconcile(self, item: OutboxItem) -> ReconciliationResult:
        self.trace.append(("reconcile", item.operation_id))
        if self.faults.get((item.operation_id, "reconcile")) is Fault.RECONCILIATION:
            raise ProviderFailure(
                EffectClass.UNCERTAIN_DISPATCH, "reconciliation_unavailable"
            )
        return self.reconciliation.get(
            item.operation_id, ReconciliationResult.UNRESOLVED
        )

    def add_poll_page(
        self,
        *,
        stream: InboundKind,
        cursor: str | None,
        next_cursor: str | None,
        events: tuple[InboundEvent, ...],
    ) -> None:
        self.pages[(stream, cursor)] = (next_cursor, events)

    def poll(self, stream: InboundKind, cursor: str | None) -> VerifiedPollPage:
        self.trace.append((f"poll_{stream.value}", cursor or "start"))
        if self.faults.get((stream.value, cursor or "start")) is Fault.POLL:
            raise ProviderFailure(EffectClass.PROVEN_PRE_DISPATCH, "poll_failed")
        next_cursor, events = self.pages.get((stream, cursor), (None, ()))
        return VerifiedPollPage(
            self.account_ref,
            self.source_ref,
            cursor,
            next_cursor,
            tuple(events),
            _VERIFIED_SOURCE_AUTHORITY,
        )
