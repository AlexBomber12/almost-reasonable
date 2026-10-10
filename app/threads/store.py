"""Dedicated versioned SQLite outbox and inbound cursor store."""

from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import datetime
import fcntl
from pathlib import Path
import sqlite3
from typing import Iterator

from .domain import (
    ConversationOutcome,
    InboundEvent,
    InboundKind,
    OutboxItem,
    OutboxState,
    PublicationType,
    SourceProvenance,
    content_fingerprint,
    require_aware_time,
    require_reference,
    validate_content,
)


SCHEMA_VERSION = 6
MAX_PENDING_INBOUND_EVENTS = 100
UNRESOLVED_STATES = (OutboxState.UNKNOWN, OutboxState.UNKNOWN_ABANDONED)
_ALLOWED_TRANSITIONS = {
    OutboxState.PROPOSED: {
        OutboxState.AWAITING_APPROVAL,
        OutboxState.CANCELLED,
    },
    OutboxState.AWAITING_APPROVAL: {OutboxState.READY, OutboxState.CANCELLED},
    OutboxState.READY: {
        OutboxState.SENDING,
        OutboxState.CANCELLED,
        OutboxState.FAILED_TERMINAL,
    },
    OutboxState.SENDING: {
        OutboxState.READY,
        OutboxState.CONFIRMED,
        OutboxState.UNKNOWN,
        OutboxState.FAILED_TERMINAL,
    },
    OutboxState.UNKNOWN: {
        OutboxState.CONFIRMED,
        OutboxState.FAILED_TERMINAL,
        OutboxState.UNKNOWN_ABANDONED,
    },
    OutboxState.UNKNOWN_ABANDONED: set(),
    OutboxState.CANCELLED: set(),
    OutboxState.CONFIRMED: set(),
    OutboxState.FAILED_TERMINAL: set(),
}


class ThreadsStoreError(RuntimeError):
    """Sanitized storage or state-machine failure."""


class DuplicateOperationError(ThreadsStoreError):
    """An operation identity was reused for a different immutable proposal."""


class UnknownPublicationRiskError(ThreadsStoreError):
    """A new operation would evade a still-uncertain publication identity."""


class CursorConflictError(ThreadsStoreError):
    """A polling page was based on a stale durable cursor."""


class PollCommitDeadlineError(ThreadsStoreError):
    """A poll page could not commit within its remaining time budget."""


class ActiveEffectWorkerError(ThreadsStoreError):
    """Recovery overlapped a live external-effect worker."""


class ThreadsStore:
    def __init__(self, database: str | Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _open(self, *, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        return connection

    def _initialize(self) -> None:
        with self._open() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='threads_schema_metadata'"
            ).fetchone()
            if existing is None:
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(
                    "INSERT INTO threads_schema_metadata(singleton, version) "
                    "VALUES (1, ?)",
                    (SCHEMA_VERSION,),
                )
            else:
                metadata = connection.execute(
                    "SELECT version FROM threads_schema_metadata WHERE singleton=1"
                ).fetchone()
                if metadata is None:
                    raise ThreadsStoreError("Threads schema metadata is missing")
                version = metadata[0]
                if version == 1:
                    for statement in _MIGRATION_1_TO_2.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        "UPDATE threads_schema_metadata SET version=? WHERE singleton=1",
                        (2,),
                    )
                    version = 2
                if version == 2:
                    for statement in _MIGRATION_2_TO_3.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        "UPDATE threads_schema_metadata SET version=? WHERE singleton=1",
                        (3,),
                    )
                    version = 3
                if version == 3:
                    for statement in _MIGRATION_3_TO_4.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        "UPDATE threads_schema_metadata SET version=? WHERE singleton=1",
                        (4,),
                    )
                    version = 4
                if version == 4:
                    for statement in _MIGRATION_4_TO_5.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        "UPDATE threads_schema_metadata SET version=? WHERE singleton=1",
                        (5,),
                    )
                    version = 5
                if version == 5:
                    for statement in _MIGRATION_5_TO_6.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        "UPDATE threads_schema_metadata SET version=? WHERE singleton=1",
                        (SCHEMA_VERSION,),
                    )
                    version = SCHEMA_VERSION
                if version != SCHEMA_VERSION:
                    raise ThreadsStoreError(
                        f"unsupported Threads schema version: {version}"
                    )
            connection.commit()

    def create_proposal(
        self,
        *,
        operation_id: str,
        account_ref: str,
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
        duplicate_risk_approval_ref: str | None = None,
        precreate_check: Callable[[], None] | None = None,
    ) -> tuple[OutboxItem, bool]:
        for field, value in (
            ("operation ID", operation_id),
            ("account reference", account_ref),
            ("draft ID", draft_id),
            ("correlation ID", correlation_id),
        ):
            require_reference(field, value)
        if type(publication_type) is not PublicationType:
            raise ValueError("invalid publication type")
        if type(source_provenance) is not SourceProvenance:
            raise ValueError("invalid source provenance")
        if type(privacy_authorized) is not bool or not privacy_authorized:
            raise ValueError("privacy authorization is required")
        if type(russian_output_authorized) is not bool or not russian_output_authorized:
            raise ValueError("Russian output authorization is required")
        if source_provenance is SourceProvenance.PRIVATE_UNAUTHORIZED:
            raise ValueError("unauthorized private provenance cannot be published")
        content = validate_content(content)
        require_aware_time("proposal time", now)
        if publication_type is PublicationType.REPLY:
            require_reference("parent message ID", parent_message_id)
        elif parent_message_id is not None:
            raise ValueError("a post cannot have a reply parent")
        if replacement_of is not None:
            require_reference("replacement operation ID", replacement_of)
            require_reference(
                "duplicate-risk approval reference", duplicate_risk_approval_ref
            )
        elif duplicate_risk_approval_ref is not None:
            raise ValueError("duplicate-risk approval requires a replacement")
        if precreate_check is not None and not callable(precreate_check):
            raise ValueError("invalid proposal pre-create check")
        fingerprint = content_fingerprint(content)
        immutable = (
            account_ref,
            publication_type.value,
            parent_message_id,
            draft_id,
            content,
            fingerprint,
            source_provenance.value,
            int(privacy_authorized),
            int(russian_output_authorized),
            correlation_id,
            replacement_of,
            duplicate_risk_approval_ref,
        )
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM threads_outbox WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if existing is not None:
                if _immutable_values(existing) != immutable:
                    raise DuplicateOperationError("operation identity conflict")
                connection.commit()
                return _outbox_item(existing), False
            if replacement_of is not None and precreate_check is None:
                raise ValueError("replacement requires an authorization fence")
            if precreate_check is not None:
                precreate_check()
            if replacement_of is not None:
                target = connection.execute(
                    "SELECT account_ref, state FROM threads_outbox "
                    "WHERE operation_id=?",
                    (replacement_of,),
                ).fetchone()
                if (
                    target is None
                    or target["account_ref"] != account_ref
                    or OutboxState(target["state"]) not in UNRESOLVED_STATES
                ):
                    raise UnknownPublicationRiskError(
                        "replacement target is no longer unresolved"
                    )
            unresolved = connection.execute(
                "SELECT operation_id FROM threads_outbox WHERE account_ref=? "
                "AND content_fingerprint=? AND state IN (?, ?)",
                (
                    account_ref,
                    fingerprint,
                    OutboxState.UNKNOWN.value,
                    OutboxState.UNKNOWN_ABANDONED.value,
                ),
            ).fetchone()
            if unresolved is not None and replacement_of != unresolved[0]:
                raise UnknownPublicationRiskError(
                    "exact content matches an unresolved publication"
                )
            timestamp = now.isoformat()
            connection.execute(
                """INSERT INTO threads_outbox(
                    operation_id, account_ref, publication_type, parent_message_id,
                    approval_reference, approval_status, draft_id, content,
                    content_fingerprint, source_provenance, privacy_authorized,
                    russian_output_authorized, state, revision, correlation_id,
                    attempt_count,
                    provider_container_id, provider_publication_id,
                    dispatch_evidence, reconciliation_status, replacement_of,
                    duplicate_risk_approval_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, 'pending', ?, ?, ?, ?, ?, ?, ?, 0, ?, 0,
                          NULL, NULL, NULL, 'not_started', ?, ?, ?, ?)""",
                (
                    operation_id,
                    account_ref,
                    publication_type.value,
                    parent_message_id,
                    draft_id,
                    content,
                    fingerprint,
                    source_provenance.value,
                    int(privacy_authorized),
                    int(russian_output_authorized),
                    OutboxState.PROPOSED.value,
                    correlation_id,
                    replacement_of,
                    duplicate_risk_approval_ref,
                    timestamp,
                    timestamp,
                ),
            )
            self._record_transition(
                connection,
                operation_id,
                None,
                OutboxState.PROPOSED,
                0,
                "proposal_created",
                timestamp,
            )
            row = connection.execute(
                "SELECT * FROM threads_outbox WHERE operation_id=?", (operation_id,)
            ).fetchone()
            connection.commit()
            return _outbox_item(row), True

    def get(self, operation_id: str) -> OutboxItem:
        require_reference("operation ID", operation_id)
        with self._open() as connection:
            row = connection.execute(
                "SELECT * FROM threads_outbox WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None:
            raise ThreadsStoreError("unknown operation")
        return _outbox_item(row)

    def record_policy_denial(
        self,
        operation_id: str,
        *,
        expected_state: OutboxState,
        reason: str,
        now: datetime,
    ) -> OutboxItem:
        require_reference("operation ID", operation_id)
        if type(expected_state) is not OutboxState:
            raise ValueError("invalid expected outbox state")
        require_reference("policy denial reason", reason)
        require_aware_time("policy denial time", now)
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM threads_outbox WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None or OutboxState(row["state"]) is not expected_state:
                raise ThreadsStoreError("policy denial state changed before audit")
            revision = row["revision"] + 1
            connection.execute(
                "UPDATE threads_outbox SET revision=?, updated_at=? "
                "WHERE operation_id=?",
                (revision, now.isoformat(), operation_id),
            )
            self._record_transition(
                connection,
                operation_id,
                expected_state,
                expected_state,
                revision,
                reason,
                now.isoformat(),
            )
            result = connection.execute(
                "SELECT * FROM threads_outbox WHERE operation_id=?", (operation_id,)
            ).fetchone()
            connection.commit()
        return _outbox_item(result)

    @contextmanager
    def account_dispatch_guard(self, account_ref: str) -> Iterator[None]:
        require_reference("account reference", account_ref)
        with self._effect_worker_guard("dispatch", account_ref):
            yield

    @contextmanager
    def reconciliation_worker_guard(self, operation_id: str) -> Iterator[None]:
        require_reference("operation ID", operation_id)
        with self._effect_worker_guard("reconciliation", operation_id):
            yield

    @contextmanager
    def _effect_worker_guard(self, namespace: str, reference: str) -> Iterator[None]:
        lock_name = (
            f".{self.database.name}."
            f"{content_fingerprint(f'{namespace}:{reference}')[:16]}.effect.lock"
        )
        lock_path = self.database.with_name(lock_name)
        with lock_path.open("a+b") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ActiveEffectWorkerError(
                    f"an active {namespace} worker exists"
                ) from error
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def transition(
        self,
        operation_id: str,
        new_state: OutboxState,
        *,
        reason: str,
        now: datetime,
        approval_reference: str | None = None,
        approval_status: str | None = None,
        provider_container_id: str | None = None,
        provider_publication_id: str | None = None,
        dispatch_evidence: str | None = None,
        reconciliation_status: str | None = None,
        increment_attempt: bool = False,
        operator_decision: str | None = None,
        operator_approval_reference: str | None = None,
        release_lock_target_operation_id: str | None = None,
        release_lock_kind: str | None = None,
        release_lock_required: bool = False,
        release_account_effect_lock: bool = False,
        account_effect_lock_required: bool = False,
        precommit_check: Callable[[], None] | None = None,
    ) -> OutboxItem:
        require_reference("operation ID", operation_id)
        if type(new_state) is not OutboxState:
            raise ValueError("invalid outbox state")
        require_reference("transition reason", reason)
        require_aware_time("transition time", now)
        for field, value in (
            ("approval reference", approval_reference),
            ("provider container ID", provider_container_id),
            ("provider publication ID", provider_publication_id),
            ("dispatch evidence", dispatch_evidence),
            ("reconciliation status", reconciliation_status),
            ("operator decision", operator_decision),
            ("operator approval reference", operator_approval_reference),
            ("release-lock target operation ID", release_lock_target_operation_id),
        ):
            if value is not None:
                require_reference(field, value)
        if (operator_decision is None) != (operator_approval_reference is None):
            raise ValueError("operator decision and approval must be supplied together")
        if operator_decision is not None and operator_decision != "abandon_unknown":
            raise ValueError("invalid operator decision")
        if (release_lock_target_operation_id is None) != (release_lock_kind is None):
            raise ValueError("release-lock target and kind must be supplied together")
        if release_lock_kind not in {None, "replacement_publication"}:
            raise ValueError("invalid transition release-lock kind")
        if type(release_lock_required) is not bool:
            raise ValueError("invalid transition release-lock requirement")
        if release_lock_required and release_lock_target_operation_id is None:
            raise ValueError("required release lock is missing its target")
        if type(release_account_effect_lock) is not bool:
            raise ValueError("invalid account-effect release requirement")
        if type(account_effect_lock_required) is not bool:
            raise ValueError("invalid account-effect lock requirement")
        if account_effect_lock_required and not release_account_effect_lock:
            raise ValueError("required account-effect release is not enabled")
        if precommit_check is not None and not callable(precommit_check):
            raise ValueError("invalid transition pre-commit check")
        if operator_decision is not None and precommit_check is None:
            raise ValueError("operator decision requires an authorization fence")
        if approval_status is not None and approval_status not in {
            "pending",
            "approved",
            "revoked",
            "expired",
        }:
            raise ValueError("invalid approval status")
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM threads_outbox WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise ThreadsStoreError("unknown operation")
            old_state = OutboxState(row["state"])
            if new_state not in _ALLOWED_TRANSITIONS[old_state]:
                raise ThreadsStoreError(
                    f"invalid outbox transition: {old_state.value} -> {new_state.value}"
                )
            if operator_decision is not None:
                reservation = connection.execute(
                    "SELECT 1 FROM threads_unknown_effect_locks "
                    "WHERE target_operation_id=?",
                    (operation_id,),
                ).fetchone()
                if reservation is not None:
                    raise ThreadsStoreError(
                        "unknown effect is reserved by an active operation"
                    )
            if precommit_check is not None:
                precommit_check()
            revision = row["revision"] + 1
            values = {
                "approval_reference": approval_reference
                if approval_reference is not None
                else row["approval_reference"],
                "approval_status": approval_status
                if approval_status is not None
                else row["approval_status"],
                "provider_container_id": provider_container_id
                if provider_container_id is not None
                else row["provider_container_id"],
                "provider_publication_id": provider_publication_id
                if provider_publication_id is not None
                else row["provider_publication_id"],
                "dispatch_evidence": dispatch_evidence
                if dispatch_evidence is not None
                else row["dispatch_evidence"],
                "reconciliation_status": reconciliation_status
                if reconciliation_status is not None
                else row["reconciliation_status"],
            }
            connection.execute(
                """UPDATE threads_outbox SET state=?, revision=?, updated_at=?,
                   approval_reference=?, approval_status=?, provider_container_id=?,
                   provider_publication_id=?, dispatch_evidence=?,
                   reconciliation_status=?, attempt_count=attempt_count+?
                   WHERE operation_id=?""",
                (
                    new_state.value,
                    revision,
                    now.isoformat(),
                    values["approval_reference"],
                    values["approval_status"],
                    values["provider_container_id"],
                    values["provider_publication_id"],
                    values["dispatch_evidence"],
                    values["reconciliation_status"],
                    int(increment_attempt),
                    operation_id,
                ),
            )
            self._record_transition(
                connection,
                operation_id,
                old_state,
                new_state,
                revision,
                reason,
                now.isoformat(),
            )
            if operator_decision is not None:
                connection.execute(
                    "INSERT INTO threads_operator_decisions "
                    "(operation_id, decision, approval_reference, occurred_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        operation_id,
                        operator_decision,
                        operator_approval_reference,
                        now.isoformat(),
                    ),
                )
            if release_lock_target_operation_id is not None:
                changed = connection.execute(
                    "DELETE FROM threads_unknown_effect_locks "
                    "WHERE target_operation_id=? AND holder_operation_id=? "
                    "AND lock_kind=?",
                    (
                        release_lock_target_operation_id,
                        operation_id,
                        release_lock_kind,
                    ),
                ).rowcount
                if release_lock_required and changed != 1:
                    raise ThreadsStoreError(
                        "required unknown-effect reservation is missing"
                    )
            if release_account_effect_lock:
                changed = connection.execute(
                    "DELETE FROM threads_account_effect_locks "
                    "WHERE account_ref=? AND holder_operation_id=?",
                    (row["account_ref"], operation_id),
                ).rowcount
                if account_effect_lock_required and changed != 1:
                    raise ThreadsStoreError(
                        "required account-effect reservation is missing"
                    )
            result = connection.execute(
                "SELECT * FROM threads_outbox WHERE operation_id=?", (operation_id,)
            ).fetchone()
            connection.commit()
        return _outbox_item(result)

    def reserve_publication_effect(
        self,
        *,
        operation_id: str,
        account_ref: str,
        replacement_target_operation_id: str | None,
        now: datetime,
    ) -> None:
        require_reference("operation ID", operation_id)
        require_reference("account reference", account_ref)
        if replacement_target_operation_id is not None:
            require_reference(
                "replacement target operation ID", replacement_target_operation_id
            )
        require_aware_time("reservation time", now)
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if replacement_target_operation_id is None:
                reservable = connection.execute(
                    "SELECT 1 FROM threads_outbox WHERE operation_id=? "
                    "AND account_ref=? AND state=? AND replacement_of IS NULL "
                    "AND NOT EXISTS (SELECT 1 FROM threads_outbox "
                    "WHERE account_ref=? AND state IN (?, ?))",
                    (
                        operation_id,
                        account_ref,
                        OutboxState.SENDING.value,
                        account_ref,
                        OutboxState.UNKNOWN.value,
                        OutboxState.UNKNOWN_ABANDONED.value,
                    ),
                ).fetchone()
            else:
                reservable = connection.execute(
                    "SELECT 1 FROM threads_outbox AS replacement "
                    "JOIN threads_outbox AS target ON target.operation_id=? "
                    "WHERE replacement.operation_id=? "
                    "AND replacement.account_ref=? AND target.account_ref=? "
                    "AND replacement.state=? AND replacement.replacement_of=? "
                    "AND target.state IN (?, ?) "
                    "AND NOT EXISTS (SELECT 1 FROM threads_outbox AS other "
                    "WHERE other.account_ref=? AND other.operation_id<>? "
                    "AND other.state IN (?, ?))",
                    (
                        replacement_target_operation_id,
                        operation_id,
                        account_ref,
                        account_ref,
                        OutboxState.SENDING.value,
                        replacement_target_operation_id,
                        OutboxState.UNKNOWN.value,
                        OutboxState.UNKNOWN_ABANDONED.value,
                        account_ref,
                        replacement_target_operation_id,
                        OutboxState.UNKNOWN.value,
                        OutboxState.UNKNOWN_ABANDONED.value,
                    ),
                ).fetchone()
            if reservable is None:
                raise ThreadsStoreError("publication effect is no longer reservable")
            try:
                if replacement_target_operation_id is not None:
                    connection.execute(
                        "INSERT INTO threads_unknown_effect_locks "
                        "(target_operation_id, holder_operation_id, lock_kind, "
                        "created_at) VALUES (?, ?, 'replacement_publication', ?)",
                        (
                            replacement_target_operation_id,
                            operation_id,
                            now.isoformat(),
                        ),
                    )
                connection.execute(
                    "INSERT INTO threads_account_effect_locks "
                    "(account_ref, holder_operation_id, created_at) VALUES (?, ?, ?)",
                    (account_ref, operation_id, now.isoformat()),
                )
            except sqlite3.IntegrityError as error:
                raise ThreadsStoreError(
                    "publication effect is already reserved"
                ) from error
            connection.commit()

    def claim_reconciliation(
        self, *, operation_id: str, account_ref: str, now: datetime
    ) -> None:
        require_reference("operation ID", operation_id)
        require_reference("account reference", account_ref)
        require_aware_time("reconciliation claim time", now)
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT 1 FROM threads_outbox WHERE operation_id=? "
                "AND account_ref=? AND state=?",
                (operation_id, account_ref, OutboxState.UNKNOWN.value),
            ).fetchone()
            if item is None:
                raise ThreadsStoreError("reconciliation target is unavailable")
            try:
                connection.execute(
                    "INSERT INTO threads_unknown_effect_locks "
                    "(target_operation_id, holder_operation_id, lock_kind, created_at) "
                    "VALUES (?, ?, 'reconciliation', ?)",
                    (operation_id, operation_id, now.isoformat()),
                )
            except sqlite3.IntegrityError as error:
                raise ThreadsStoreError("unknown effect is already reserved") from error
            connection.commit()

    def release_unknown_effect_lock(
        self,
        *,
        target_operation_id: str,
        holder_operation_id: str,
        lock_kind: str,
        required: bool,
    ) -> None:
        require_reference("target operation ID", target_operation_id)
        require_reference("holder operation ID", holder_operation_id)
        if lock_kind not in {"reconciliation", "replacement_publication"}:
            raise ValueError("invalid unknown-effect lock kind")
        if type(required) is not bool:
            raise ValueError("invalid unknown-effect release requirement")
        with self._open() as connection:
            changed = connection.execute(
                "DELETE FROM threads_unknown_effect_locks "
                "WHERE target_operation_id=? AND holder_operation_id=? "
                "AND lock_kind=?",
                (target_operation_id, holder_operation_id, lock_kind),
            ).rowcount
        if required and changed != 1:
            raise ThreadsStoreError("unknown-effect reservation is missing")

    def recover_reconciliation_claim(
        self,
        *,
        operation_id: str,
        account_ref: str,
        approval_reference: str,
        now: datetime,
        precommit_check: Callable[[], None],
    ) -> None:
        require_reference("operation ID", operation_id)
        require_reference("account reference", account_ref)
        require_reference("approval reference", approval_reference)
        require_aware_time("reconciliation recovery time", now)
        if not callable(precommit_check):
            raise ValueError("invalid reconciliation recovery pre-commit check")
        decision_prefix = (
            f"recover_reconciliation-{content_fingerprint(approval_reference)[:16]}"
        )
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            precommit_check()
            changed = connection.execute(
                "DELETE FROM threads_unknown_effect_locks "
                "WHERE target_operation_id=? AND holder_operation_id=? "
                "AND lock_kind='reconciliation' AND EXISTS ("
                "SELECT 1 FROM threads_outbox WHERE operation_id=? "
                "AND account_ref=? AND state=?)",
                (
                    operation_id,
                    operation_id,
                    operation_id,
                    account_ref,
                    OutboxState.UNKNOWN.value,
                ),
            ).rowcount
            if changed != 1:
                raise ThreadsStoreError("recoverable reconciliation claim is missing")
            recovery_count = connection.execute(
                "SELECT COUNT(*) FROM threads_operator_decisions "
                "WHERE operation_id=? AND decision GLOB 'recover_reconciliation-*'",
                (operation_id,),
            ).fetchone()[0]
            decision = f"{decision_prefix}-{recovery_count + 1}"
            connection.execute(
                "INSERT INTO threads_operator_decisions "
                "(operation_id, decision, approval_reference, occurred_at) "
                "VALUES (?, ?, ?, ?)",
                (operation_id, decision, approval_reference, now.isoformat()),
            )
            connection.commit()

    def operator_decision_reference(
        self, operation_id: str, decision: str
    ) -> str | None:
        require_reference("operation ID", operation_id)
        require_reference("operator decision", decision)
        with self._open() as connection:
            row = connection.execute(
                "SELECT approval_reference FROM threads_operator_decisions "
                "WHERE operation_id=? AND decision=?",
                (operation_id, decision),
            ).fetchone()
        return None if row is None else row["approval_reference"]

    def update_sending_evidence(
        self,
        operation_id: str,
        evidence: str,
        *,
        now: datetime,
        provider_container_id: str | None = None,
    ) -> None:
        require_reference("operation ID", operation_id)
        require_reference("dispatch evidence", evidence)
        if provider_container_id is not None:
            require_reference("provider container ID", provider_container_id)
        require_aware_time("evidence time", now)
        with self._open() as connection:
            changed = connection.execute(
                "UPDATE threads_outbox SET dispatch_evidence=?, "
                "provider_container_id=COALESCE(?, provider_container_id), updated_at=? "
                "WHERE operation_id=? AND state=?",
                (
                    evidence,
                    provider_container_id,
                    now.isoformat(),
                    operation_id,
                    OutboxState.SENDING.value,
                ),
            ).rowcount
        if changed != 1:
            raise ThreadsStoreError("dispatch evidence requires sending state")

    def note_reconciliation(
        self, operation_id: str, status: str, *, now: datetime
    ) -> OutboxItem:
        require_reference("operation ID", operation_id)
        require_reference("reconciliation status", status)
        require_aware_time("reconciliation time", now)
        with self._open() as connection:
            changed = connection.execute(
                "UPDATE threads_outbox SET reconciliation_status=?, updated_at=? "
                "WHERE operation_id=? AND state=?",
                (status, now.isoformat(), operation_id, OutboxState.UNKNOWN.value),
            ).rowcount
        if changed != 1:
            raise ThreadsStoreError("reconciliation requires unknown state")
        return self.get(operation_id)

    def unresolved_for_account(self, account_ref: str) -> tuple[OutboxItem, ...]:
        require_reference("account reference", account_ref)
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM threads_outbox WHERE account_ref=? AND state IN (?, ?) "
                "ORDER BY created_at, operation_id",
                (
                    account_ref,
                    OutboxState.UNKNOWN.value,
                    OutboxState.UNKNOWN_ABANDONED.value,
                ),
            ).fetchall()
        return tuple(_outbox_item(row) for row in rows)

    def likely_duplicates(self, operation_id: str) -> tuple[str, ...]:
        item = self.get(operation_id)
        with self._open() as connection:
            rows = connection.execute(
                "SELECT operation_id FROM threads_outbox WHERE account_ref=? "
                "AND content_fingerprint=? AND operation_id<>? AND state<>? "
                "ORDER BY created_at, operation_id LIMIT 10",
                (
                    item.account_ref,
                    item.content_fingerprint,
                    operation_id,
                    OutboxState.CANCELLED.value,
                ),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def cursor(self, account_ref: str, stream: InboundKind) -> str | None:
        require_reference("account reference", account_ref)
        if type(stream) is not InboundKind:
            raise ValueError("invalid inbound stream")
        with self._open() as connection:
            row = connection.execute(
                "SELECT cursor FROM threads_poll_cursors WHERE account_ref=? AND stream=?",
                (account_ref, stream.value),
            ).fetchone()
        return None if row is None else row[0]

    def record_poll_policy_denial(
        self,
        *,
        correlation_id: str,
        account_ref: str,
        stream: InboundKind,
        actor_reference: str,
        reason: str,
        now: datetime,
    ) -> None:
        for field, value in (
            ("correlation ID", correlation_id),
            ("account reference", account_ref),
            ("actor reference", actor_reference),
            ("poll denial reason", reason),
        ):
            require_reference(field, value)
        if type(stream) is not InboundKind:
            raise ValueError("invalid inbound stream")
        require_aware_time("poll denial time", now)
        with self._open() as connection:
            changed = connection.execute(
                "INSERT OR IGNORE INTO threads_poll_policy_denials "
                "(correlation_id, account_ref, stream, actor_reference, reason, "
                "occurred_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    correlation_id,
                    account_ref,
                    stream.value,
                    actor_reference,
                    reason,
                    now.isoformat(),
                ),
            ).rowcount
            if changed == 1:
                return
            existing = connection.execute(
                "SELECT account_ref, stream, actor_reference, reason "
                "FROM threads_poll_policy_denials WHERE correlation_id=?",
                (correlation_id,),
            ).fetchone()
            expected = (account_ref, stream.value, actor_reference, reason)
            if tuple(existing) != expected:
                raise ThreadsStoreError("poll denial correlation conflict")

    def commit_poll_page(
        self,
        *,
        correlation_id: str,
        account_ref: str,
        stream: InboundKind,
        verified_source_ref: str,
        expected_cursor: str | None,
        next_cursor: str | None,
        events: Iterable[InboundEvent],
        now: datetime,
        lock_wait_seconds: float = 5.0,
        deadline_check: Callable[[], bool] | None = None,
        precommit_check: Callable[[], None],
    ) -> tuple[tuple[InboundEvent, ...], int]:
        require_reference("poll correlation ID", correlation_id)
        require_reference("account reference", account_ref)
        if type(stream) is not InboundKind:
            raise ValueError("invalid inbound stream")
        require_reference("verified source reference", verified_source_ref)
        for cursor in (expected_cursor, next_cursor):
            if cursor is not None:
                require_reference("pagination cursor", cursor)
        require_aware_time("poll commit time", now)
        if type(lock_wait_seconds) not in {int, float} or not (
            0 < lock_wait_seconds <= 5.0
        ):
            raise ValueError("invalid poll commit lock wait")
        if deadline_check is not None and not callable(deadline_check):
            raise ValueError("invalid poll commit deadline check")
        if not callable(precommit_check):
            raise ValueError("invalid poll commit authorization fence")
        page = tuple(events)
        busy_timeout_ms = max(1, int(lock_wait_seconds * 1000))
        with self._open(busy_timeout_ms=busy_timeout_ms) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if "locked" in str(error).casefold():
                    raise PollCommitDeadlineError(
                        "poll page commit exceeded its lock-wait budget"
                    ) from error
                raise
            if deadline_check is not None and not deadline_check():
                raise PollCommitDeadlineError("poll page commit deadline reached")
            precommit_check()
            row = connection.execute(
                "SELECT cursor FROM threads_poll_cursors WHERE account_ref=? AND stream=?",
                (account_ref, stream.value),
            ).fetchone()
            current = None if row is None else row[0]
            if current != expected_cursor:
                raise CursorConflictError("poll cursor changed before page commit")
            inserted = []
            duplicates = 0
            for event in page:
                _validate_inbound_event(event, account_ref, stream, verified_source_ref)
                changed = connection.execute(
                    """INSERT OR IGNORE INTO threads_inbound_events(
                       event_id, correlation_id, account_ref, source_ref, kind, conversation_id,
                       parent_message_id, occurred_at, content, received_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_id,
                        event.correlation_id,
                        event.account_ref,
                        event.source_ref,
                        event.kind.value,
                        event.conversation_id,
                        event.parent_message_id,
                        event.occurred_at.isoformat(),
                        event.content,
                        now.isoformat(),
                    ),
                ).rowcount
                if changed:
                    inserted.append(event)
                else:
                    existing = connection.execute(
                        "SELECT correlation_id, source_ref, conversation_id, "
                        "parent_message_id, occurred_at, content "
                        "FROM threads_inbound_events WHERE account_ref=? "
                        "AND kind=? AND event_id=?",
                        (event.account_ref, event.kind.value, event.event_id),
                    ).fetchone()
                    expected_event = (
                        event.correlation_id,
                        event.source_ref,
                        event.conversation_id,
                        event.parent_message_id,
                        event.occurred_at.isoformat(),
                        event.content,
                    )
                    if existing is None or tuple(existing) != expected_event:
                        raise ThreadsStoreError("inbound event identity conflict")
                    duplicates += 1
            if deadline_check is not None and not deadline_check():
                raise PollCommitDeadlineError("poll page commit deadline reached")
            connection.execute(
                """INSERT INTO threads_poll_cursors(account_ref, stream, cursor, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(account_ref, stream) DO UPDATE SET
                       cursor=excluded.cursor, updated_at=excluded.updated_at""",
                (
                    account_ref,
                    stream.value,
                    next_cursor if next_cursor is not None else expected_cursor,
                    now.isoformat(),
                ),
            )
            actor_reference = f"account-{content_fingerprint(account_ref)[:16]}"
            changed = connection.execute(
                "INSERT OR IGNORE INTO threads_poll_actions "
                "(correlation_id, account_ref, stream, actor_reference, action, "
                "occurred_at) VALUES (?, ?, ?, ?, 'poll_committed', ?)",
                (
                    correlation_id,
                    account_ref,
                    stream.value,
                    actor_reference,
                    now.isoformat(),
                ),
            ).rowcount
            if changed == 0:
                existing = connection.execute(
                    "SELECT account_ref, stream, actor_reference, action "
                    "FROM threads_poll_actions WHERE correlation_id=?",
                    (correlation_id,),
                ).fetchone()
                expected = (
                    account_ref,
                    stream.value,
                    actor_reference,
                    "poll_committed",
                )
                if existing is None or tuple(existing) != expected:
                    raise ThreadsStoreError("poll action correlation conflict")
            connection.commit()
        return tuple(inserted), duplicates

    def record_conversation_outcome(
        self,
        *,
        outcome_id: str,
        correlation_id: str,
        account_ref: str,
        stream: InboundKind,
        inbound_event_id: str,
        outcome: ConversationOutcome,
        reason: str,
        resume_condition: str | None,
        now: datetime,
    ) -> None:
        require_reference("outcome ID", outcome_id)
        require_reference("correlation ID", correlation_id)
        require_reference("account reference", account_ref)
        if type(stream) is not InboundKind:
            raise ValueError("invalid inbound stream")
        require_reference("inbound event ID", inbound_event_id)
        require_reference("outcome reason", reason)
        if type(outcome) is not ConversationOutcome:
            raise ValueError("invalid conversation outcome")
        if outcome is ConversationOutcome.DEFER:
            require_reference("resume condition", resume_condition)
        elif resume_condition is not None:
            raise ValueError("resume condition is only valid for defer")
        require_aware_time("outcome time", now)
        with self._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                changed = connection.execute(
                    "INSERT INTO threads_conversation_outcomes "
                    "(outcome_id, correlation_id, inbound_account_ref, inbound_kind, "
                    "inbound_event_id, outcome, reason, resume_condition, occurred_at) "
                    "SELECT ?, ?, account_ref, kind, event_id, ?, ?, ?, ? "
                    "FROM threads_inbound_events WHERE account_ref=? AND kind=? "
                    "AND event_id=? AND correlation_id=?",
                    (
                        outcome_id,
                        correlation_id,
                        outcome.value,
                        reason,
                        resume_condition,
                        now.isoformat(),
                        account_ref,
                        stream.value,
                        inbound_event_id,
                        correlation_id,
                    ),
                ).rowcount
            except sqlite3.IntegrityError as error:
                raise ThreadsStoreError(
                    "invalid or duplicate conversation outcome"
                ) from error
            if changed != 1:
                raise ThreadsStoreError(
                    "conversation outcome correlation does not match inbound event"
                )
            acknowledged = connection.execute(
                "UPDATE threads_inbound_events SET processed_at=? "
                "WHERE account_ref=? AND kind=? AND event_id=? "
                "AND correlation_id=? AND processed_at IS NULL",
                (
                    now.isoformat(),
                    account_ref,
                    stream.value,
                    inbound_event_id,
                    correlation_id,
                ),
            ).rowcount
            if acknowledged != 1:
                raise ThreadsStoreError("inbound event is already processed")
            connection.commit()

    def pending_inbound_events(
        self, account_ref: str, stream: InboundKind, *, limit: int = 100
    ) -> tuple[InboundEvent, ...]:
        require_reference("account reference", account_ref)
        if type(stream) is not InboundKind:
            raise ValueError("invalid inbound stream")
        if type(limit) is not int or not 1 <= limit <= MAX_PENDING_INBOUND_EVENTS:
            raise ValueError("invalid pending inbound limit")
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM threads_inbound_events WHERE account_ref=? AND kind=? "
                "AND processed_at IS NULL ORDER BY received_at, event_id LIMIT ?",
                (account_ref, stream.value, limit),
            ).fetchall()
        return tuple(_inbound_event(row) for row in rows)

    @staticmethod
    def _record_transition(
        connection: sqlite3.Connection,
        operation_id: str,
        old_state: OutboxState | None,
        new_state: OutboxState,
        revision: int,
        reason: str,
        timestamp: str,
    ) -> None:
        connection.execute(
            "INSERT INTO threads_outbox_transitions "
            "(operation_id, revision, old_state, new_state, reason, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                operation_id,
                revision,
                None if old_state is None else old_state.value,
                new_state.value,
                reason,
                timestamp,
            ),
        )


def _immutable_values(row: sqlite3.Row) -> tuple[object, ...]:
    return tuple(
        row[field]
        for field in (
            "account_ref",
            "publication_type",
            "parent_message_id",
            "draft_id",
            "content",
            "content_fingerprint",
            "source_provenance",
            "privacy_authorized",
            "russian_output_authorized",
            "correlation_id",
            "replacement_of",
            "duplicate_risk_approval_ref",
        )
    )


def _outbox_item(row: sqlite3.Row) -> OutboxItem:
    return OutboxItem(
        operation_id=row["operation_id"],
        account_ref=row["account_ref"],
        publication_type=PublicationType(row["publication_type"]),
        parent_message_id=row["parent_message_id"],
        approval_reference=row["approval_reference"],
        approval_status=row["approval_status"],
        draft_id=row["draft_id"],
        content=row["content"],
        content_fingerprint=row["content_fingerprint"],
        source_provenance=SourceProvenance(row["source_provenance"]),
        privacy_authorized=bool(row["privacy_authorized"]),
        russian_output_authorized=bool(row["russian_output_authorized"]),
        state=OutboxState(row["state"]),
        revision=row["revision"],
        correlation_id=row["correlation_id"],
        attempt_count=row["attempt_count"],
        provider_container_id=row["provider_container_id"],
        provider_publication_id=row["provider_publication_id"],
        dispatch_evidence=row["dispatch_evidence"],
        reconciliation_status=row["reconciliation_status"],
        replacement_of=row["replacement_of"],
        duplicate_risk_approval_ref=row["duplicate_risk_approval_ref"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _validate_inbound_event(
    event: InboundEvent,
    account_ref: str,
    stream: InboundKind,
    verified_source_ref: str,
) -> None:
    if type(event) is not InboundEvent:
        raise ValueError("invalid inbound event")
    for field, value in (
        ("event ID", event.event_id),
        ("correlation ID", event.correlation_id),
        ("source reference", event.source_ref),
        ("conversation ID", event.conversation_id),
    ):
        require_reference(field, value)
    if event.account_ref != account_ref or event.kind is not stream:
        raise ValueError("inbound event account or stream mismatch")
    if event.source_ref != verified_source_ref:
        raise ValueError("inbound event source does not match verified page")
    if event.parent_message_id is not None:
        require_reference("parent message ID", event.parent_message_id)
    require_aware_time("event time", event.occurred_at)
    validate_content(event.content, inbound=True)


def _inbound_event(row: sqlite3.Row) -> InboundEvent:
    return InboundEvent(
        event_id=row["event_id"],
        correlation_id=row["correlation_id"],
        account_ref=row["account_ref"],
        source_ref=row["source_ref"],
        kind=InboundKind(row["kind"]),
        conversation_id=row["conversation_id"],
        parent_message_id=row["parent_message_id"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        content=row["content"],
    )


_SCHEMA = """
CREATE TABLE threads_schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL
);
CREATE TABLE threads_outbox (
    operation_id TEXT PRIMARY KEY,
    account_ref TEXT NOT NULL,
    publication_type TEXT NOT NULL CHECK(publication_type IN ('post','reply')),
    parent_message_id TEXT,
    approval_reference TEXT,
    approval_status TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    content TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL,
    source_provenance TEXT NOT NULL,
    privacy_authorized INTEGER NOT NULL CHECK(privacy_authorized=1),
    russian_output_authorized INTEGER NOT NULL CHECK(russian_output_authorized=1),
    state TEXT NOT NULL,
    revision INTEGER NOT NULL,
    correlation_id TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    provider_container_id TEXT UNIQUE,
    provider_publication_id TEXT UNIQUE,
    dispatch_evidence TEXT,
    reconciliation_status TEXT NOT NULL,
    replacement_of TEXT REFERENCES threads_outbox(operation_id),
    duplicate_risk_approval_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX threads_outbox_account_state
    ON threads_outbox(account_ref, state);
CREATE INDEX threads_outbox_fingerprint
    ON threads_outbox(account_ref, content_fingerprint);
CREATE TABLE threads_outbox_transitions (
    operation_id TEXT NOT NULL REFERENCES threads_outbox(operation_id),
    revision INTEGER NOT NULL,
    old_state TEXT,
    new_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    PRIMARY KEY(operation_id, revision)
);
CREATE TABLE threads_inbound_events (
    event_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    account_ref TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('reply','mention')),
    conversation_id TEXT NOT NULL,
    parent_message_id TEXT,
    occurred_at TEXT NOT NULL,
    content TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    PRIMARY KEY(account_ref, kind, event_id)
);
CREATE TABLE threads_poll_cursors (
    account_ref TEXT NOT NULL,
    stream TEXT NOT NULL CHECK(stream IN ('reply','mention')),
    cursor TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_ref, stream)
);
CREATE TABLE threads_poll_policy_denials (
    correlation_id TEXT PRIMARY KEY,
    account_ref TEXT NOT NULL,
    stream TEXT NOT NULL CHECK(stream IN ('reply','mention')),
    actor_reference TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE threads_poll_actions (
    correlation_id TEXT PRIMARY KEY,
    account_ref TEXT NOT NULL,
    stream TEXT NOT NULL CHECK(stream IN ('reply','mention')),
    actor_reference TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action='poll_committed'),
    occurred_at TEXT NOT NULL
);
CREATE TABLE threads_conversation_outcomes (
    outcome_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    inbound_account_ref TEXT NOT NULL,
    inbound_kind TEXT NOT NULL CHECK(inbound_kind IN ('reply','mention')),
    inbound_event_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    resume_condition TEXT,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(inbound_account_ref, inbound_kind, inbound_event_id)
        REFERENCES threads_inbound_events(account_ref, kind, event_id)
);
CREATE TABLE threads_operator_decisions (
    operation_id TEXT NOT NULL REFERENCES threads_outbox(operation_id),
    decision TEXT NOT NULL,
    approval_reference TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    PRIMARY KEY(operation_id, decision)
);
CREATE TABLE threads_unknown_effect_locks (
    target_operation_id TEXT PRIMARY KEY REFERENCES threads_outbox(operation_id),
    holder_operation_id TEXT NOT NULL REFERENCES threads_outbox(operation_id),
    lock_kind TEXT NOT NULL CHECK(
        lock_kind IN ('reconciliation','replacement_publication')
    ),
    created_at TEXT NOT NULL
);
CREATE TABLE threads_account_effect_locks (
    account_ref TEXT PRIMARY KEY,
    holder_operation_id TEXT NOT NULL REFERENCES threads_outbox(operation_id),
    created_at TEXT NOT NULL
);
"""


_MIGRATION_1_TO_2 = """
ALTER TABLE threads_inbound_events ADD COLUMN correlation_id TEXT;
ALTER TABLE threads_conversation_outcomes ADD COLUMN correlation_id TEXT;
UPDATE threads_inbound_events
SET correlation_id='legacy-' || substr(event_id, 1, 121);
UPDATE threads_conversation_outcomes
SET correlation_id=(
    SELECT correlation_id FROM threads_inbound_events
    WHERE threads_inbound_events.event_id=threads_conversation_outcomes.inbound_event_id
);
CREATE TABLE threads_operator_decisions (
    operation_id TEXT NOT NULL REFERENCES threads_outbox(operation_id),
    decision TEXT NOT NULL,
    approval_reference TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    PRIMARY KEY(operation_id, decision)
);
CREATE TABLE threads_unknown_effect_locks (
    target_operation_id TEXT PRIMARY KEY REFERENCES threads_outbox(operation_id),
    holder_operation_id TEXT NOT NULL REFERENCES threads_outbox(operation_id),
    lock_kind TEXT NOT NULL CHECK(
        lock_kind IN ('reconciliation','replacement_publication')
    ),
    created_at TEXT NOT NULL
);
"""


_MIGRATION_2_TO_3 = """
CREATE TABLE threads_inbound_events_v3 (
    event_id TEXT NOT NULL,
    correlation_id TEXT,
    account_ref TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('reply','mention')),
    conversation_id TEXT NOT NULL,
    parent_message_id TEXT,
    occurred_at TEXT NOT NULL,
    content TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY(account_ref, kind, event_id)
);
INSERT INTO threads_inbound_events_v3
SELECT event_id, correlation_id, account_ref, source_ref, kind, conversation_id,
       parent_message_id, occurred_at, content, received_at
FROM threads_inbound_events;
CREATE TABLE threads_conversation_outcomes_v3 (
    outcome_id TEXT PRIMARY KEY,
    correlation_id TEXT,
    inbound_account_ref TEXT NOT NULL,
    inbound_kind TEXT NOT NULL CHECK(inbound_kind IN ('reply','mention')),
    inbound_event_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    resume_condition TEXT,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(inbound_account_ref, inbound_kind, inbound_event_id)
        REFERENCES threads_inbound_events_v3(account_ref, kind, event_id)
);
INSERT INTO threads_conversation_outcomes_v3
SELECT outcome.outcome_id, outcome.correlation_id, event.account_ref, event.kind,
       outcome.inbound_event_id, outcome.outcome, outcome.reason,
       outcome.resume_condition, outcome.occurred_at
FROM threads_conversation_outcomes AS outcome
JOIN threads_inbound_events AS event
  ON event.event_id=outcome.inbound_event_id;
DROP TABLE threads_conversation_outcomes;
DROP TABLE threads_inbound_events;
ALTER TABLE threads_inbound_events_v3 RENAME TO threads_inbound_events;
ALTER TABLE threads_conversation_outcomes_v3 RENAME TO threads_conversation_outcomes;
CREATE TABLE threads_account_effect_locks (
    account_ref TEXT PRIMARY KEY,
    holder_operation_id TEXT NOT NULL REFERENCES threads_outbox(operation_id),
    created_at TEXT NOT NULL
);
"""


_MIGRATION_3_TO_4 = """
CREATE TABLE threads_poll_policy_denials (
    correlation_id TEXT PRIMARY KEY,
    account_ref TEXT NOT NULL,
    stream TEXT NOT NULL CHECK(stream IN ('reply','mention')),
    actor_reference TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
"""


_MIGRATION_4_TO_5 = """
ALTER TABLE threads_inbound_events ADD COLUMN processed_at TEXT;
UPDATE threads_inbound_events
SET processed_at=(
    SELECT MIN(outcome.occurred_at)
    FROM threads_conversation_outcomes AS outcome
    WHERE outcome.inbound_account_ref=threads_inbound_events.account_ref
      AND outcome.inbound_kind=threads_inbound_events.kind
      AND outcome.inbound_event_id=threads_inbound_events.event_id
)
WHERE EXISTS (
    SELECT 1 FROM threads_conversation_outcomes AS outcome
    WHERE outcome.inbound_account_ref=threads_inbound_events.account_ref
      AND outcome.inbound_kind=threads_inbound_events.kind
      AND outcome.inbound_event_id=threads_inbound_events.event_id
);
"""


_MIGRATION_5_TO_6 = """
CREATE TABLE threads_poll_actions (
    correlation_id TEXT PRIMARY KEY,
    account_ref TEXT NOT NULL,
    stream TEXT NOT NULL CHECK(stream IN ('reply','mention')),
    actor_reference TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action='poll_committed'),
    occurred_at TEXT NOT NULL
);
"""
