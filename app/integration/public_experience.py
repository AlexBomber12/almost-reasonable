"""Local public history selection and immutable per-operation source bindings.

The journal and Threads database commit independently. Revalidation detects local
withdrawal/change; it does not assert present availability on the provider.
"""

from datetime import UTC, datetime
import hashlib
import json
import sqlite3

from app.core.public_experience import (
    EXPERIENCE_VERSION,
    MAX_RECORDS,
    PublicExperience,
    PublicExperienceRecord,
    serialized,
    inference_context_digest,
)
from app.integration.threads_stage0 import ThreadsStage0
from app.threads.domain import InboundKind, OutboxState, SourceProvenance
from app.threads.store import ThreadsStoreError


def initialize_experience_storage(db):
    """Additive v1 migration: never rewrite existing operations or authorities."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS public_experience_schema("
        "singleton INTEGER PRIMARY KEY CHECK(singleton=1), version TEXT NOT NULL)"
    )
    db.execute(
        "INSERT OR IGNORE INTO public_experience_schema VALUES(1,?)",
        (EXPERIENCE_VERSION,),
    )
    if (
        db.execute("SELECT version FROM public_experience_schema").fetchone()[0]
        != EXPERIENCE_VERSION
    ):
        raise ValueError("unsupported public experience schema")
    db.execute(
        "CREATE TABLE IF NOT EXISTS public_experience_snapshots("
        "operation TEXT PRIMARY KEY, version TEXT NOT NULL, "
        "sources TEXT NOT NULL CHECK(length(CAST(sources AS BLOB))<=16384))"
    )


def _digest(value):
    return hashlib.sha256(serialized(value)).hexdigest()


class PublicExperienceHistory:
    def __init__(self, app):
        self.app = app

    def _event_allowed(self, event):
        app = self.app
        return (
            event.source_ref == app.simulator.source_ref
            and app.service.store.conversation_control(
                app.account, event.kind, event.conversation_id, now=app.clock.now()
            )
            is None
        )

    def candidates(self):
        return self._eligible()[0]

    def _eligible(self):
        candidates = self._candidates()
        eligible = self.app.intellectual.sources(_candidates=candidates)
        return {
            key: value for key, value in candidates.items() if key in eligible
        }, eligible

    def _candidates(self):
        """Authorize every source before relevance ranking, count or byte limits."""
        app = self.app
        candidates = {}
        for event in app.service.store.public_experience_events(app.account):
            if self._event_allowed(event):
                record = PublicExperienceRecord(
                    "external_assertion",
                    event.occurred_at.astimezone(UTC).isoformat(),
                    event.content,
                    event.event_id,
                    event.parent_message_id,
                    event.conversation_id,
                )
                candidates[f"event:{event.kind.value}:{event.event_id}"] = (
                    record,
                    _digest(app._origin(event)),
                )
        # Read-only access avoids nested writer transactions at the existing
        # authorization/handoff checks. No transaction spans independent stores.
        with sqlite3.connect(app.journal.path) as db:
            rows = db.execute(
                "SELECT operation,payload FROM intents WHERE phase='linked'"
            ).fetchall()
            sources = dict(db.execute("SELECT draft,active FROM sources"))
        for operation, raw in rows:
            payload = json.loads(raw)
            if (
                payload.get("runtime_version") != "threads-public-runtime-v1"
                or payload.get("generation") != app.ledger.control_generation()
                or not ThreadsStage0.source_valid(app, payload)
            ):
                continue
            try:
                root = None if app.mission is None else app.mission._root_snapshot()[1]
                if (
                    payload.get("experience_mission_root", payload.get("mission"))
                    != root
                ):
                    continue
            except ValueError:
                continue
            if payload["kind"] != "wake":
                event = app.service.store.inbound_event(
                    app.account, InboundKind(payload["kind"]), payload["event"]
                )
                if not self._event_allowed(event):
                    continue
            if payload.get("decision") == "no_action":
                record = PublicExperienceRecord(
                    "no_action",
                    payload["decided_at"],
                    payload["reason"],
                    None,
                    payload.get("parent"),
                    payload.get("conversation"),
                )
                candidates[f"decision:{operation}"] = (record, _digest(payload))
            elif sources.get(payload.get("draft")) == 1:
                try:
                    item = app.service.store.get(operation)
                except ThreadsStoreError:
                    continue
                if (
                    item.account_ref == app.account
                    and item.state is OutboxState.CONFIRMED
                    and item.provider_publication_id is not None
                    and item.dispatch_evidence
                    == "provider_publication_details_verified"
                    and item.source_provenance is SourceProvenance.PUBLIC
                    and item.content == payload.get("content")
                    and item.draft_id == payload.get("draft")
                ):
                    record = PublicExperienceRecord(
                        "confirmed_publication",
                        item.updated_at.astimezone(UTC).isoformat(),
                        item.content,
                        item.provider_publication_id,
                        item.parent_message_id,
                        payload.get("conversation"),
                    )
                    candidates[f"publication:{operation}"] = (
                        record,
                        _digest([payload, item.revision, item.dispatch_evidence]),
                    )
        return candidates

    def select(self, origin, *, research_source=None):
        from app.core.intellectual_state import MAX_DEPENDENCIES

        candidates, sources = self._eligible()
        dependencies = self.app.intellectual.context_dependencies(
            origin, sources, research_source=research_source
        )
        conversation = origin.get("conversation")
        current = origin.get("event") if origin["kind"] != "wake" else None
        ranked = []
        for key, (record, binding) in candidates.items():
            if record.message_id == current and current is not None:
                continue
            if conversation is not None and not (
                record.conversation_id == conversation
                or record.message_id in (conversation, origin.get("parent"))
                or record.parent_message_id == conversation
            ):
                continue
            ranked.append((key, record, binding))
        # Newest first; public identity breaks timestamp ties, independent of SQL order.
        ranked.sort(
            key=lambda row: (datetime.fromisoformat(row[1].occurred_at), row[0]),
            reverse=True,
        )
        selected, records = [], []
        for key, record, binding in ranked:
            additions = {**sources[key]["dependencies"], key: binding}
            if len(dependencies.keys() | additions.keys()) > MAX_DEPENDENCIES:
                continue
            try:
                PublicExperience(tuple([*records, record])).value()
            except ValueError:
                continue  # Skip an oversized record without starving smaller records.
            selected.append({"key": key, "binding": binding, "record": record.value()})
            records.append(record)
            dependencies.update(additions)
            if len(records) == MAX_RECORDS:
                break
        return selected, PublicExperience(tuple(records))

    def load(self, operation, payload):
        # Legacy operations never acquire a new snapshot, including started work.
        if "experience_digest" not in payload:
            return None
        app = self.app
        with sqlite3.connect(app.journal.path) as db:
            row = db.execute(
                "SELECT version,sources FROM public_experience_snapshots WHERE operation=?",
                (operation,),
            ).fetchone()
        if row is None or row[0] != EXPERIENCE_VERSION:
            raise ValueError("public experience snapshot missing")
        selected = json.loads(row[1])
        current = self.candidates()
        records = []
        for source in selected:
            candidate = current.get(source["key"])
            if (
                candidate is None
                or candidate[1] != source["binding"]
                or candidate[0].value() != source["record"]
            ):
                raise ValueError("public experience source changed")
            records.append(candidate[0])
        experience = PublicExperience(tuple(records))
        if experience.digest != payload["experience_digest"]:
            raise ValueError("public experience binding changed")
        if app.ledger.control_generation() != payload["experience_generation"]:
            raise ValueError("public experience controls changed")
        if payload["kind"] != "wake":
            event = app.service.store.inbound_event(
                app.account, InboundKind(payload["kind"]), payload["event"]
            )
            if (
                event is None
                or not self._event_allowed(event)
                or inference_context_digest((event.content,), experience)
                != payload["context_digest"]
            ):
                raise ValueError("public trigger context changed")
        return experience
