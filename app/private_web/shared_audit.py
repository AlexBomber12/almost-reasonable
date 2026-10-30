"""Erasable source snapshot and durable one-shot shared proposal receipt."""

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import secrets

from app.cognition.worker_shared import (
    MAX_ITEMS,
    SharedProposalItem,
    SharedProposalRequest,
    SharedSuggestion,
    parse_shared_suggestion,
    suggestion_data,
)
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.core.model_shared_dependencies import validate_dependencies

from .extraction_audit import ExtractionAuditStore


@dataclass(frozen=True, slots=True)
class SharedSnapshot:
    selected: tuple[tuple[str, str, int, str], ...]
    visible: tuple[tuple[str, int, str], ...]
    sources: tuple[str, ...]
    digest: str
    dependencies: dict


@dataclass(frozen=True, slots=True)
class SharedAudit:
    status: str
    snapshot: SharedSnapshot | None
    suggestion: SharedSuggestion | None
    request: SharedProposalRequest | None = None


class SharedProposalAuditStore(ExtractionAuditStore):
    def __init__(
        self, provider: ProviderStateStore, events: EventStore, shared, audience, clock
    ):
        super().__init__(provider, events)
        if (
            type(audience) is not tuple
            or not 1 <= len(audience) <= 16
            or tuple(sorted(set(audience))) != audience
            or any(type(item) is not str for item in audience)
        ):
            raise ValueError("invalid shared proposal audience")
        for item in audience:
            EventStore._require_token("audience", item)
        self._shared = shared
        self._audience = audience
        self._clock = clock

    def _snapshot(self, principal, source_event_id, content, *, selected=None):
        principal_id = principal.principal_id.value
        self._check_input(principal_id, source_event_id, content, required=True)
        with closing(self._events._open()) as db:
            db.execute("BEGIN")
            self._events._verify_current_schema(db)
            heads = db.execute(
                """SELECT o.item_id,o.revision,o.operation_id,o.kind,p.value,p.confidence
                   FROM shared_operations o JOIN shared_payloads p USING(operation_id)
                   WHERE (o.author_principal=? OR (? AND EXISTS (
                       SELECT 1 FROM protected_shared_audiences a WHERE a.operation_id=o.operation_id AND a.principal_id=?)))
                     AND o.status='active'
                     AND o.revision=(SELECT MAX(h.revision) FROM shared_operations h
                                     WHERE h.item_id=o.item_id AND h.status='active')
                   ORDER BY o.item_id""",
                (principal_id, int(self._shared._ordinary_enabled), principal_id),
            ).fetchall()
            visible = tuple(
                (item, revision, operation) for item, revision, operation, *_ in heads
            )
            if self._shared._ordinary_enabled:
                visible = (
                    (
                        "heads:"
                        + hashlib.sha256(json.dumps(visible).encode()).hexdigest(),
                        0,
                        "ordinary-heads-v1",
                    ),
                )
            eligible = []
            for item, revision, operation, kind, value, confidence in heads:
                if revision >= 32:
                    continue
                sources = db.execute(
                    "SELECT event_id,source_principal FROM protected_shared_sources WHERE operation_id=? ORDER BY event_id",
                    (operation,),
                ).fetchall()
                if (
                    len(sources) >= 50
                    or (
                        not self._shared._ordinary_enabled
                        and any(owner != principal_id for _, owner in sources)
                    )
                    or (
                        not self._shared._ordinary_enabled
                        and any(
                            self._shared._gate._ledger.active_shared_consent(
                                principal_id, event_id, audience, self._clock()
                            )
                            is None
                            for event_id, _ in sources
                            for audience in self._audience
                        )
                    )
                ):
                    continue
                if self._shared._ordinary_enabled:
                    manifest = db.execute(
                        "SELECT binding FROM protected_shared_manifests WHERE operation_id=?",
                        (operation,),
                    ).fetchone()
                    from app.cognition.ordinary_abstract import ORDINARY_POLICY_VERSION

                    if (
                        manifest is None
                        or json.loads(manifest[0]).get("ordinary_policy")
                        != ORDINARY_POLICY_VERSION
                    ):
                        continue
                if not all(
                    db.execute(
                        "SELECT 1 FROM protected_shared_audiences WHERE operation_id=? AND principal_id=?",
                        (operation, audience),
                    ).fetchone()
                    for audience in self._audience
                ):
                    continue
                eligible.append(
                    (item, revision, operation, kind, value, confidence, sources)
                )
            if selected is None:
                selected = tuple(
                    ("h" + secrets.token_hex(8), item, revision, operation)
                    for item, revision, operation, *_ in eligible[:1]
                )
            selected_lookup = {
                (item, revision, operation): (handle, sources)
                for (handle, item, revision, operation) in selected
                for row in eligible
                if row[:3] == (item, revision, operation)
                for sources in (row[6],)
            }
            if len(selected_lookup) != len(selected):
                raise ValueError("shared snapshot changed")
            items = []
            bound_sources = {source_event_id}
            binding = [[source_event_id, content]]
            for row in eligible:
                key = row[:3]
                if key not in selected_lookup:
                    continue
                handle, sources = selected_lookup[key]
                _, _, _, kind, value, confidence, _ = row
                items.append(SharedProposalItem(handle, kind, value, confidence))
                for event_id, _ in sources:
                    source = db.execute(
                        """SELECT p.content FROM event_envelopes e JOIN private_payloads p
                           USING(payload_id) WHERE e.event_id=?
                           AND p.retention_status='active'""",
                        (event_id,),
                    ).fetchone()
                    if source is None:
                        raise ValueError("shared source unavailable")
                    bound_sources.add(event_id)
                    binding.append([event_id, source[0]])
                binding.append(list(row[:6]))
            consents = []
            for event_id in sorted(bound_sources):
                for audience in self._audience:
                    consent = self._shared._gate._ledger.active_shared_consent(
                        principal_id, event_id, audience, self._clock()
                    )
                    binding.append([event_id, audience, consent])
                    consents.append([event_id, audience, consent])
            dependencies = dict(
                version="ordinary-shared-model-dependencies-v1"
                if self._shared._ordinary_enabled
                else "shared-model-dependencies-v1",
                principal=principal_id,
                sources=sorted(bound_sources),
                audience=list(self._audience),
                consents=consents,
                reviews=sorted(row[3] for row in selected),
            )
            validate_dependencies(db, dependencies)
            request = SharedProposalRequest(content, tuple(items))
            request.serialize()
            digest = hashlib.sha256(
                json.dumps(
                    [visible, binding, self._audience],
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            return SharedSnapshot(
                selected, visible, tuple(sorted(bound_sources)), digest, dependencies
            ), request

    def valid(self, principal, source_event_id, content, snapshot):
        try:
            fresh, _ = self._snapshot(
                principal, source_event_id, content, selected=snapshot.selected
            )
        except ValueError:
            return False
        return fresh == snapshot

    def _receipt(self, operation_id, principal_id, source_event_id):
        with closing(self._events._open()) as db:
            self._events._verify_current_schema(db)
            row = db.execute(
                "SELECT source_event_id,source_principal_id,proposal FROM private_extraction_receipts WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        if row[:2] != (source_event_id, principal_id):
            raise ValueError("shared receipt conflict")
        try:
            if len(row[2].encode("utf-8")) > 131_072:
                raise ValueError
            data = json.loads(row[2])
            if (
                type(data) is not dict
                or set(data) != {"version", "snapshot", "proposal"}
                or data["version"] != "shared-proposal-receipt-v2"
            ):
                raise ValueError
            raw = data["snapshot"]
            if type(raw) is not dict or set(raw) != {
                "selected",
                "visible",
                "sources",
                "digest",
                "dependencies",
            }:
                raise ValueError
            snapshot = SharedSnapshot(
                tuple(tuple(item) for item in raw["selected"]),
                tuple(tuple(item) for item in raw["visible"]),
                tuple(raw["sources"]),
                raw["digest"],
                raw["dependencies"],
            )
            if (
                len(snapshot.selected) > MAX_ITEMS
                or len(snapshot.visible) > 128
                or len(snapshot.sources) > 50
                or not snapshot.sources
                or tuple(sorted(set(snapshot.sources))) != snapshot.sources
                or type(snapshot.digest) is not str
                or len(snapshot.digest) != 64
                or any(
                    len(item) != 4
                    or any(
                        type(part) is not str for part in (item[0], item[1], item[3])
                    )
                    or type(item[2]) is not int
                    for item in snapshot.selected
                )
                or any(
                    len(item) != 3
                    or type(item[0]) is not str
                    or type(item[1]) is not int
                    or type(item[2]) is not str
                    for item in snapshot.visible
                )
            ):
                raise ValueError
            with closing(self._events._open()) as db:
                validate_dependencies(db, snapshot.dependencies)
            if (
                snapshot.dependencies["sources"] != list(snapshot.sources)
                or snapshot.dependencies["audience"] != list(self._audience)
                or snapshot.dependencies["principal"] != principal_id
                or snapshot.dependencies["reviews"]
                != sorted(row[3] for row in snapshot.selected)
            ):
                raise ValueError
            suggestion = (
                None
                if data["proposal"] is None
                else parse_shared_suggestion(json.dumps(data["proposal"]))
            )
            return snapshot, suggestion
        except (TypeError, ValueError, UnicodeError):
            raise ValueError("invalid shared receipt") from None

    def read(self, operation_id, principal_id, source_event_id, content):
        self._check_input(principal_id, source_event_id, content, required=False)
        with closing(self._provider._event_store._open()) as db:
            self._provider._event_store._verify_current_schema(db)
            row = db.execute(
                "SELECT binding,status FROM private_extraction_audit WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        if row[0] != self._binding(principal_id, source_event_id):
            raise ValueError("shared proposal identity conflict")
        if row[1] not in {"reserved", "received"}:
            self._delete_receipt(operation_id)
            return SharedAudit(row[1], None, None)
        receipt = self._receipt(operation_id, principal_id, source_event_id)
        if receipt is None or (row[1] == "received" and receipt[1] is None):
            self.finish(operation_id, "unavailable")
            return SharedAudit("unavailable", None, None)
        snapshot, suggestion = receipt
        return SharedAudit(
            "received" if suggestion is not None else "reserved", snapshot, suggestion
        )

    def reserve(self, operation_id, principal, source_event_id, content):
        principal_id = principal.principal_id.value
        existing = self.read(operation_id, principal_id, source_event_id, content)
        if existing is not None:
            return existing
        snapshot, request = self._snapshot(principal, source_event_id, content)
        with closing(self._provider._event_store._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._provider._event_store._verify_current_schema(db)
            db.execute(
                "INSERT INTO private_extraction_audit VALUES (?,?,?)",
                (
                    operation_id,
                    self._binding(principal_id, source_event_id),
                    "reserved",
                ),
            )
        receipt = json.dumps(
            {
                "version": "shared-proposal-receipt-v2",
                "snapshot": {
                    "selected": snapshot.selected,
                    "visible": snapshot.visible,
                    "sources": snapshot.sources,
                    "digest": snapshot.digest,
                    "dependencies": snapshot.dependencies,
                },
                "proposal": None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with closing(self._events._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._events._verify_current_schema(db)
            db.execute(
                "INSERT INTO private_extraction_receipts VALUES (?,?,?,?)",
                (operation_id, source_event_id, principal_id, receipt),
            )
        return SharedAudit("reserved", snapshot, None, request)

    def receive(self, operation_id, principal_id, source_event_id, suggestion):
        parsed = parse_shared_suggestion(json.dumps(suggestion_data(suggestion)))
        with closing(self._events._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._events._verify_current_schema(db)
            row = db.execute(
                "SELECT proposal FROM private_extraction_receipts WHERE operation_id=? AND source_event_id=? AND source_principal_id=?",
                (operation_id, source_event_id, principal_id),
            ).fetchone()
            if row is None:
                raise ValueError("shared receipt unavailable")
            data = json.loads(row[0])
            if data["proposal"] is not None:
                raise ValueError("shared receipt conflict")
            data["proposal"] = suggestion_data(parsed)
            db.execute(
                "UPDATE private_extraction_receipts SET proposal=? WHERE operation_id=?",
                (
                    json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                    operation_id,
                ),
            )
        with closing(self._provider._event_store._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute(
                    "UPDATE private_extraction_audit SET status='received' WHERE operation_id=? AND status='reserved'",
                    (operation_id,),
                ).rowcount
                != 1
            ):
                raise ValueError("shared receipt conflict")
