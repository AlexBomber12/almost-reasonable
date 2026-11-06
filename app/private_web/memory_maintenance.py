"""Delayed auxiliary work over existing source and one-use audit identities.

The reply's prompt marker is the durable intent. The existing IPC audit stores
only its opaque binding and completion; proposal payloads retain source erasure.
No private transcript is copied into a second queue or replayed as a new call.
"""

from contextlib import closing, contextmanager
import fcntl
import os
from threading import Event, Thread

from app.cognition.read_store import retrieve_exact_private_event
from app.core.events import (
    AuthenticatedSourceReference,
    ParticipantId,
    TrustedPrincipalContext,
)
from app.privacy.application import PrivacyReadBlocked
from .extraction_audit import ExtractionAuditStore

MARKER = ":deferred-memory-v1"


class MaintenanceDeferred(RuntimeError):
    """A foreground turn takes priority over the next auxiliary stage."""


class MemoryMaintenance:
    def __init__(self, runtime, principal_enabled):
        self.app = runtime
        self.principal_enabled = principal_enabled
        self.audit = ExtractionAuditStore(runtime._provider, runtime._event_store)
        database = runtime._provider._event_store.database_path
        self.lock_path = database.with_name(database.name + ".maintenance.lock")

    @contextmanager
    def _guard(self):
        with os.fdopen(
            os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a"
        ) as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                try:
                    yield True
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def identity(operation):
        return "maintenance:" + operation.removeprefix("reply:")

    def _pending(self, limit):
        with closing(self.app._provider._event_store._open()) as db:
            finished = {
                operation
                for operation, status in db.execute(
                    "SELECT operation_id,status FROM private_extraction_audit WHERE operation_id LIKE 'maintenance:%'"
                )
                if status not in {"reserved", "received"}
            }
        result = []
        with closing(self.app._event_store._open()) as db:
            self.app._event_store._verify_current_schema(db)
            # Iterate metadata in commit order; completed entries cannot hide a
            # later pending input behind a fixed history/selection limit.
            for row in db.execute(
                "SELECT o.operation_id,o.initiating_event_id,o.audience_principal_id,o.correlation_id "
                "FROM agent_reply_operations o JOIN event_input_sequences s ON s.event_id=o.initiating_event_id "
                "JOIN event_envelopes e ON e.event_id=o.initiating_event_id "
                "JOIN private_payloads p ON p.payload_id=e.payload_id "
                "JOIN agent_reply_resolutions r ON r.operation_id=o.operation_id "
                "WHERE o.prompt_version LIKE ? "
                "AND p.retention_status='active' AND r.outcome IN ('completed','no_action') "
                "ORDER BY s.sequence",
                ("%" + MARKER,),
            ):
                if self.identity(row[0]) not in finished:
                    result.append(row)
                    if len(result) == limit:
                        break
        return result

    def drain(self, limit):
        app = self.app
        processed = 0
        if app._maintenance_should_yield():
            return 0
        with self._guard() as acquired:
            if not acquired:
                return 0
            try:
                rows = app._gate.authorized(lambda: self._pending(limit))
                for operation, event_id, participant, correlation in rows:
                    caps = app._capabilities()
                    if caps.emergency_stop_active or caps.provider_spend_suspended:
                        break
                    principal = TrustedPrincipalContext(
                        ParticipantId(participant),
                        AuthenticatedSourceReference("internal-memory-maintenance"),
                    )
                    with app.authenticated_request(
                        lambda: self.principal_enabled(participant)
                    ):
                        source = app._gate.authorized(
                            lambda: retrieve_exact_private_event(
                                app._event_store, principal, event_id
                            )
                        )
                        if source is None:
                            raise PrivacyReadBlocked("maintenance_source_unavailable")
                        identity = self.identity(operation)
                        audit = app._gate.authorized(
                            lambda: self.audit.reserve(
                                identity, participant, event_id, source.content
                            )
                        )
                        if audit.status not in {"reserved", "received"}:
                            continue

                        def preflight():
                            def read():
                                if app._maintenance_should_yield():
                                    raise MaintenanceDeferred()
                                if (
                                    not app._current_request.get()()
                                    or retrieve_exact_private_event(
                                        app._event_store, principal, event_id
                                    )
                                    is None
                                ):
                                    raise PrivacyReadBlocked(
                                        "maintenance_authority_unavailable"
                                    )
                                return app._capabilities()

                            return app._gate.authorized(read)

                        try:
                            caps = preflight()
                            caps = app._maintain_worker_memory(
                                principal,
                                event_id,
                                source.content,
                                correlation,
                                operation,
                                preflight,
                                caps,
                            )
                        except MaintenanceDeferred:
                            break
                        except PrivacyReadBlocked:
                            self.audit.finish(identity, "unavailable")
                            processed += 1
                            continue
                        if caps.emergency_stop_active or caps.provider_spend_suspended:
                            break
                        # Completion means the existing stage audits were run or
                        # reconciled, not that every model proposed a memory change.
                        self.audit.finish(identity, "accepted")
                        processed += 1
            except PrivacyReadBlocked:
                pass
        return processed


class MemoryMaintenanceRunner:
    """One optional application thread; it never owns provider credentials."""

    def __init__(self, runtime, *, interval=1.0):
        if type(interval) not in {int, float} or not 0 < interval <= 60:
            raise ValueError("invalid maintenance interval")
        self.runtime = runtime
        self.interval = interval
        self.status = "waiting"
        self._stop = Event()
        self._thread = Thread(
            target=self._run, daemon=True, name="private-memory-maintenance"
        )

    def start(self):
        self._thread.start()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=1)

    def _run(self):
        failed = False
        while not self._stop.wait(self.interval):
            self.status = "running"
            try:
                self.runtime.drain_memory_maintenance()
            except Exception:
                # Source/one-use audit identities remain durable for recovery.
                # Never emit exception details, model output or private text.
                if not failed:
                    print(
                        "Private memory maintenance delayed by an internal failure.",
                        flush=True,
                    )
                self.status = "failed"
                failed = True
            else:
                self.status = "waiting"
                failed = False
        self.status = "stopped"
