"""Credential-free Threads domain with durable simulation and recovery."""

from .domain import (
    AccountPolicy,
    Approval,
    ApprovalPurpose,
    ConversationOutcome,
    CurrentAuthorizationProvider,
    InboundKind,
    OutboxItem,
    OutboxState,
    PublicationType,
    SourceProvenance,
    Stage,
    issue_approval,
)
from .service import ThreadsService
from .simulator import (
    Fault,
    ReconciliationResult,
    SimulationAuthorizationProvider,
    ThreadsSimulator,
)
from .store import ThreadsStore

__all__ = (
    "AccountPolicy",
    "Approval",
    "ApprovalPurpose",
    "ConversationOutcome",
    "CurrentAuthorizationProvider",
    "Fault",
    "InboundKind",
    "OutboxItem",
    "OutboxState",
    "PublicationType",
    "ReconciliationResult",
    "SimulationAuthorizationProvider",
    "SourceProvenance",
    "Stage",
    "ThreadsService",
    "ThreadsSimulator",
    "ThreadsStore",
    "issue_approval",
)
