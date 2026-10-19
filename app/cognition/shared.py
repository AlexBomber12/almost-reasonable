"""Trusted, offline-only exact approvals for screened abstract revisions.

Approvals are constructor capabilities owned by the synthetic application. They
are never parsed from a model, participant, HTTP request, or owner assertion.
The closed Stage 0 vocabulary deliberately excludes arbitrary private facts.
"""

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from types import SimpleNamespace

from app.core.events import TrustedPrincipalContext
from app.core.shared_graph import validate_shared_graph, revoke_shared_authorization
from app.core.store import EventStore
from app.privacy.application import PrivacyReadGate

from .contracts import AbstractToSharedProposal, AbstractReason
from .conversation_context import ModelMemory
from .sharing import (
    AbstractToSharedValidator,
    PRIVACY_SCREEN_VERSION,
    screen_abstract_candidate,
    _contains_protected_marker,
)


SYNTHETIC_ABSTRACT_VALUES = frozenset(
    {
        "Investigate uncertainty in complex systems.",
        "Consider competing explanations of uncertainty.",
        "Explore philosophical accounts of scientific understanding.",
        "Compare evidence before adopting a long-term intellectual direction.",
    }
)


@dataclass(frozen=True, slots=True)
class SyntheticSharedApproval:
    principal: TrustedPrincipalContext
    proposal: AbstractToSharedProposal
    item_id: str
    expected_revision: int
    audience: tuple[str, ...]
    counter_event_ids: tuple[str, ...] = ()
    allow_mission_evidence: bool = False


@dataclass(frozen=True, slots=True)
class ReviewedSharedCandidate:
    principal: TrustedPrincipalContext
    proposal: AbstractToSharedProposal
    item_id: str
    expected_revision: int
    audience: tuple[str, ...]
    counter_event_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SharedRecord:
    """Protected selection identity stays outside its model projection."""

    operation_id: str
    memory: ModelMemory
    revision: int
    kind: str

    def __post_init__(self):
        EventStore._require_token("shared selection identity", self.operation_id)
        if (
            type(self.memory) is not ModelMemory
            or type(self.kind) is not str
            or self.kind
            not in {
                "durable_interest",
                "belief_or_hypothesis",
                "goal_or_intention",
                "unresolved_question",
            }
            or type(self.revision) is not int
            or not 1 <= self.revision <= 32
        ):
            raise ValueError("invalid closed shared record")

    @property
    def verification(self):
        return self.memory.verification

    @property
    def counterevidence_count(self):
        return self.memory.counterevidence_count

    @property
    def text(self):
        return self.memory.text

    def structured(self):
        return {
            **self.memory.structured(),
            "scope": "approved_shared_abstract",
            "revision": self.revision,
            "kind": self.kind,
        }


class SharedIntelligence:
    @staticmethod
    def _review_identity() -> str:
        return f"os-uid:{os.geteuid()}"

    def __init__(
        self,
        events: EventStore,
        gate: PrivacyReadGate,
        *,
        approvals: tuple[SyntheticSharedApproval, ...] = (),
        clock: Callable[[], datetime],
        privacy_screen=screen_abstract_candidate,
        checkpoint=lambda _point: None,
    ):
        if type(gate) is not PrivacyReadGate or gate._store is not events:
            raise ValueError("shared gate must bind application database")
        if type(approvals) is not tuple or any(
            type(a) is not SyntheticSharedApproval for a in approvals
        ):
            raise ValueError("exact synthetic approvals required")
        self._events = events
        self._gate = gate
        self._approvals = approvals
        self._clock = clock
        self._privacy_screen = privacy_screen
        self._checkpoint = checkpoint

    @staticmethod
    def _shape(a):
        if type(a) is not SyntheticSharedApproval:
            raise ValueError("exact synthetic approval required")
        if type(a.allow_mission_evidence) is not bool:
            raise ValueError("invalid mission evidence purpose grant")
        EventStore._validate_context(a.principal)
        EventStore._require_token("shared item", a.item_id)
        if type(a.expected_revision) is not int or not 0 <= a.expected_revision < 32:
            raise ValueError("invalid shared revision")
        if (
            type(a.audience) is not tuple
            or not 1 <= len(a.audience) <= 16
            or any(type(p) is not str for p in a.audience)
            or tuple(sorted(set(a.audience))) != a.audience
        ):
            raise ValueError("invalid shared audience")
        for p in a.audience:
            EventStore._require_token("audience", p)
        if (
            type(a.proposal) is not AbstractToSharedProposal
            or type(a.counter_event_ids) is not tuple
            or any(type(e) is not str for e in a.counter_event_ids)
            or tuple(sorted(set(a.counter_event_ids))) != a.counter_event_ids
            or not set(a.counter_event_ids)
            <= set(a.proposal.protected_source_event_ids)
        ):
            raise ValueError("invalid shared evidence classification")

    def screen(self, approval: SyntheticSharedApproval) -> str:
        return self._screen(approval, reviewed=False)

    def quarantine(self, candidate: ReviewedSharedCandidate) -> str:
        if type(candidate) is not ReviewedSharedCandidate:
            raise ValueError("reviewed candidate required")
        approval = SyntheticSharedApproval(
            candidate.principal,
            candidate.proposal,
            candidate.item_id,
            candidate.expected_revision,
            candidate.audience,
            candidate.counter_event_ids,
        )
        return self._screen(approval, reviewed=True)

    def _screen(self, approval: SyntheticSharedApproval, *, reviewed: bool) -> str:
        self._shape(approval)
        # Screening is a short deterministic offline read fence. Persistence uses
        # a separate transaction and rechecks source availability after screening.
        result = self._gate.authorized(
            lambda: AbstractToSharedValidator(
                self._events, privacy_screen=self._privacy_screen
            ).validate(approval.principal, approval.proposal)
        )
        if result.audit is None or result.audit.screen_version is None:
            raise ValueError("shared screening evidence unavailable")

        def persist():
            with closing(self._events._open()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self._events._verify_current_schema(db)
                validate_shared_graph(db)
                parent, sources = self._binding_sources(db, approval)
                payload = approval.proposal.candidate
                complete_sources = tuple(
                    SimpleNamespace(
                        content=db.execute(
                            "SELECT p.content FROM event_envelopes e JOIN private_payloads p USING(payload_id) WHERE e.event_id=?",
                            (source,),
                        ).fetchone()[0]
                    )
                    for source, _principal, _role, _classification in sources
                )
                complete_screen = screen_abstract_candidate(payload, complete_sources)
                complete_allowed = (
                    complete_screen.decision.value == "allow"
                    and not any(
                        _contains_protected_marker(payload.value, marker)
                        for source, principal, _role, _classification in sources
                        for marker in (source, principal)
                    )
                )
                screened_reason = (
                    result.reason.value if complete_allowed else "privacy_rejected"
                )
                retained_value = (
                    payload.value
                    if reviewed
                    or (complete_allowed and payload.value in SYNTHETIC_ABSTRACT_VALUES)
                    else "Screened abstract candidate withheld."
                )
                binding = json.dumps(
                    dict(
                        item_id=approval.item_id,
                        revision=approval.expected_revision,
                        author=approval.principal.principal_id.value,
                        mission_evidence=approval.allow_mission_evidence,
                        audience=approval.audience,
                        sources=sources,
                        proposed_at=EventStore._serialize_time(
                            approval.proposal.proposed_at
                        ),
                        candidate_digest=hashlib.sha256(
                            payload.value.encode()
                        ).hexdigest(),
                        confidence=payload.confidence,
                        kind=payload.memory_kind.value,
                        **({"reviewed": True} if reviewed else {}),
                    ),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                existing = db.execute(
                    "SELECT item_id, expected_revision, parent_operation, author_principal, kind, status, reason, screen_version FROM shared_operations WHERE operation_id=?",
                    (approval.proposal.operation_id,),
                ).fetchone()
                expected = (
                    approval.item_id,
                    approval.expected_revision,
                    parent,
                    approval.principal.principal_id.value,
                    payload.memory_kind.value,
                )
                if existing is not None:
                    stored = db.execute(
                        "SELECT value, confidence, verification FROM shared_payloads WHERE operation_id=?",
                        (approval.proposal.operation_id,),
                    ).fetchone()
                    stored_sources = db.execute(
                        "SELECT event_id, source_principal, role, classification FROM protected_shared_sources WHERE operation_id=? ORDER BY event_id",
                        (approval.proposal.operation_id,),
                    ).fetchall()
                    audiences = db.execute(
                        "SELECT principal_id FROM protected_shared_audiences WHERE operation_id=? ORDER BY principal_id",
                        (approval.proposal.operation_id,),
                    ).fetchall()
                    manifest = db.execute(
                        "SELECT binding FROM protected_shared_manifests WHERE operation_id=?",
                        (approval.proposal.operation_id,),
                    ).fetchone()
                    if (
                        manifest != (binding,)
                        or existing[:5] != expected
                        or stored
                        != (
                            retained_value,
                            payload.confidence,
                            payload.verification.value,
                        )
                        or stored_sources != sources
                        or tuple(p[0] for p in audiences) != approval.audience
                    ):
                        raise ValueError("shared operation identity conflict")
                    return existing[6]
                db.execute(
                    """INSERT INTO shared_operations VALUES
                    (?, ?, ?, ?, ?, ?, 'conversation', ?, ?, 'quarantined', ?, 1, ?, ?, ?, ?, ?)""",
                    (
                        approval.proposal.operation_id,
                        approval.item_id,
                        approval.expected_revision,
                        approval.expected_revision + 1,
                        parent,
                        approval.principal.principal_id.value,
                        int(approval.allow_mission_evidence),
                        payload.memory_kind.value,
                        screened_reason,
                        result.audit.screen_version,
                        screened_reason,
                        len(sources),
                        len(approval.audience),
                        EventStore._serialize_time(self._clock()),
                    ),
                )
                db.execute(
                    "INSERT INTO shared_payloads VALUES (?, ?, ?, ?)",
                    (
                        approval.proposal.operation_id,
                        retained_value,
                        payload.confidence,
                        payload.verification.value,
                    ),
                )
                db.execute(
                    "INSERT INTO protected_shared_manifests VALUES (?, ?)",
                    (approval.proposal.operation_id, binding),
                )
                db.executemany(
                    "INSERT INTO protected_shared_sources VALUES (?, ?, ?, ?, ?, 1)",
                    ((approval.proposal.operation_id, *s) for s in sources),
                )
                db.executemany(
                    "INSERT INTO protected_shared_audiences VALUES (?, ?, 'conversation')",
                    ((approval.proposal.operation_id, p) for p in approval.audience),
                )
                validate_shared_graph(db)
                return screened_reason

        return self._gate.authorized(persist)

    def review(
        self,
        candidate: ReviewedSharedCandidate,
        *,
        decision_id: str,
        review_version: str,
        approve: bool,
    ) -> str:
        """An explicit local operator decision over one durable candidate identity."""
        EventStore._require_token("review decision", decision_id)
        EventStore._require_token("review version", review_version)
        if type(approve) is not bool:
            raise ValueError("invalid review decision")
        reason = self.quarantine(candidate)
        self._checkpoint("after_shared_screen")
        if approve and reason not in {
            AbstractReason.SHARED_COMMIT_DISABLED.value,
            "approved",
        }:
            raise ValueError("shared privacy screen did not allow promotion")

        def decide():
            with closing(self._events._open()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self._events._verify_current_schema(db)
                validate_shared_graph(db)
                operation_id = candidate.proposal.operation_id
                review_identity = self._review_identity()
                prior = db.execute(
                    "SELECT operation_id,decision_id,review_identity,review_version,decision FROM reviewed_shared_decisions "
                    "WHERE operation_id=? OR decision_id=?",
                    (operation_id, decision_id),
                ).fetchone()
                decision = "approved" if approve else "rejected"
                if prior is not None:
                    if prior != (
                        operation_id,
                        decision_id,
                        review_identity,
                        review_version,
                        decision,
                    ):
                        raise ValueError("review decision identity conflict")
                    return operation_id if approve else "rejected"
                decision_time = self._clock()
                decision_stamp = EventStore._serialize_time(decision_time)
                row = db.execute(
                    "SELECT status,screen_version,screen_result,mission_evidence "
                    "FROM shared_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                manifest = db.execute(
                    "SELECT binding FROM protected_shared_manifests WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if (
                    row is None
                    or row[0] != "quarantined"
                    or row[1] != PRIVACY_SCREEN_VERSION
                    or row[3] != 0
                    or manifest is None
                    or json.loads(manifest[0]).get("reviewed") is not True
                ):
                    raise ValueError("reviewed quarantine unavailable")
                db.execute(
                    "INSERT INTO reviewed_shared_decisions VALUES (?,?,?,?,?,?)",
                    (
                        operation_id,
                        decision_id,
                        review_identity,
                        review_version,
                        decision,
                        decision_stamp,
                    ),
                )
                if approve:
                    if row[2] != AbstractReason.SHARED_COMMIT_DISABLED.value:
                        raise ValueError(
                            "shared privacy screen did not allow promotion"
                        )
                    head = db.execute(
                        "SELECT COALESCE(MAX(revision),0) FROM shared_operations "
                        "WHERE item_id=? AND status='active'",
                        (candidate.item_id,),
                    ).fetchone()[0]
                    if head != candidate.expected_revision:
                        raise ValueError("shared revision conflict")
                    sources = db.execute(
                        "SELECT event_id,source_principal FROM protected_shared_sources "
                        "WHERE operation_id=? ORDER BY event_id",
                        (operation_id,),
                    ).fetchall()
                    consents = []
                    for event_id, source_principal in sources:
                        for audience in candidate.audience:
                            consent_id = self._gate._ledger.active_shared_consent(
                                source_principal, event_id, audience, decision_time
                            )
                            if consent_id is None:
                                raise ValueError("source consent unavailable")
                            consents.append(
                                (operation_id, consent_id, event_id, audience)
                            )
                    db.execute(
                        "UPDATE shared_operations SET status='active',reason='approved' "
                        "WHERE operation_id=?",
                        (operation_id,),
                    )
                    db.executemany(
                        "INSERT INTO protected_reviewed_consents VALUES (?,?,?,?)",
                        consents,
                    )
                validate_shared_graph(db)
                self._checkpoint("before_shared_commit")
                return operation_id if approve else "rejected"

        result = self._gate.authorized(decide)
        self._checkpoint("after_shared_commit")
        return result

    def revoke_review(self, operation_id: str, revocation_id: str) -> str:
        EventStore._require_token("shared operation", operation_id)
        EventStore._require_token("review revocation", revocation_id)

        def revoke():
            with closing(self._events._open()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self._events._verify_current_schema(db)
                validate_shared_graph(db)
                existing = db.execute(
                    "SELECT operation_id,revocation_id FROM reviewed_shared_revocations "
                    "WHERE operation_id=? OR revocation_id=?",
                    (operation_id, revocation_id),
                ).fetchone()
                if existing is not None:
                    if existing != (operation_id, revocation_id):
                        raise ValueError("review revocation identity conflict")
                    return "revoked"
                row = db.execute(
                    "SELECT o.status,d.decision FROM shared_operations o "
                    "JOIN reviewed_shared_decisions d USING(operation_id) "
                    "WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if row != ("active", "approved"):
                    raise ValueError("active reviewed authorization required")
                db.execute(
                    "INSERT INTO reviewed_shared_revocations VALUES (?,?,?)",
                    (
                        operation_id,
                        revocation_id,
                        EventStore._serialize_time(self._clock()),
                    ),
                )
                revoke_shared_authorization(db, operation_id)
                validate_shared_graph(db)
                return "revoked"

        return self._gate.authorized(revoke)

    def _binding_sources(self, db, a):
        if (
            db.execute(
                "SELECT 1 FROM shared_operations WHERE item_id=? AND status='invalidated'",
                (a.item_id,),
            ).fetchone()
            is not None
        ):
            raise ValueError("invalidated shared item requires a new identity")
        parent = None
        sources = {}
        if a.expected_revision:
            row = db.execute(
                "SELECT operation_id FROM shared_operations WHERE item_id=? AND revision=? AND status='active'",
                (a.item_id, a.expected_revision),
            ).fetchone()
            if row is None:
                raise ValueError("shared parent revision unavailable")
            parent = row[0]
            for e, p, classification in db.execute(
                "SELECT event_id, source_principal, classification FROM protected_shared_sources WHERE operation_id=?",
                (parent,),
            ):
                sources[e] = (e, p, "inherited", classification)
        for e in a.proposal.protected_source_event_ids:
            if e in sources:
                raise ValueError("shared revision cannot relist inherited source")
            row = db.execute(
                "SELECT audience_principal_id, payload_id FROM event_envelopes WHERE event_id=?",
                (e,),
            ).fetchone()
            if (
                row is None
                or row[0] != a.principal.principal_id.value
                or row[1] is None
            ):
                raise ValueError("shared source unavailable")
            sources[e] = (
                e,
                row[0],
                "counter" if e in a.counter_event_ids else "supporting",
                "counter" if e in a.counter_event_ids else "supporting",
            )
        if not 1 <= len(sources) <= 50:
            raise ValueError("shared dependency bound exceeded")
        return parent, sorted(sources.values())

    def commit(self, approval: SyntheticSharedApproval) -> str:
        self._shape(approval)
        if approval not in self._approvals:
            raise ValueError("trusted exact application approval required")
        reason = self.screen(approval)
        self._checkpoint("after_shared_screen")
        if reason not in {AbstractReason.SHARED_COMMIT_DISABLED.value, "approved"}:
            raise ValueError("shared privacy screen did not allow promotion")
        if approval.proposal.candidate.value not in SYNTHETIC_ABSTRACT_VALUES:
            raise ValueError("unreviewed synthetic abstract content")

        def promote():
            with closing(self._events._open()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self._events._verify_current_schema(db)
                validate_shared_graph(db)
                row = db.execute(
                    "SELECT status, screen_version FROM shared_operations WHERE operation_id=?",
                    (approval.proposal.operation_id,),
                ).fetchone()
                if row != ("active", PRIVACY_SCREEN_VERSION):
                    if row != ("quarantined", PRIVACY_SCREEN_VERSION):
                        raise ValueError("outdated shared screen evidence")
                    head = db.execute(
                        "SELECT COALESCE(MAX(revision),0) FROM shared_operations WHERE item_id=? AND status='active'",
                        (approval.item_id,),
                    ).fetchone()[0]
                    if head != approval.expected_revision:
                        raise ValueError("shared revision conflict")
                    db.execute(
                        "UPDATE shared_operations SET status='active', reason='approved' WHERE operation_id=?",
                        (approval.proposal.operation_id,),
                    )
                validate_shared_graph(db)
                self._checkpoint("before_shared_commit")
                return approval.proposal.operation_id

        result = self._gate.authorized(promote)
        self._checkpoint("after_shared_commit")
        return result

    def context(self, principal):
        EventStore._validate_context(principal)

        def read():
            with closing(self._events._open()) as db, db:
                db.execute("BEGIN")
                self._events._verify_current_schema(db)
                return self.select(db, principal)

        return self._gate.authorized(read)

    def select(self, db, principal):
        EventStore._validate_context(principal)
        validate_shared_graph(db)
        rows = db.execute(
            """SELECT o.operation_id, o.kind, p.value, p.confidence,
            (SELECT COUNT(*) FROM protected_shared_sources s WHERE s.operation_id=o.operation_id AND classification='counter'),
            o.source_count, o.revision,
            (SELECT json_group_array(past.value) FROM shared_operations h
                JOIN shared_payloads past ON past.operation_id=h.operation_id
                WHERE h.item_id=o.item_id AND h.status='active' AND h.revision<o.revision
                    AND past.value<>p.value AND EXISTS (SELECT 1 FROM protected_shared_audiences ha
                        WHERE ha.operation_id=h.operation_id AND ha.principal_id=a.principal_id))
            FROM shared_operations o JOIN shared_payloads p USING(operation_id)
            JOIN protected_shared_audiences a USING(operation_id)
            WHERE a.principal_id=? AND a.purpose='conversation' AND o.status='active'
            AND o.revision=(SELECT MAX(h.revision) FROM shared_operations h WHERE h.item_id=o.item_id AND h.status='active')
            ORDER BY o.item_id, o.revision""",
            (principal.principal_id.value,),
        ).fetchall()
        return tuple(
            SharedRecord(
                op,
                ModelMemory(
                    "belief_or_hypothesis" if kind == "unresolved_question" else kind,
                    value,
                    "active",
                    confidence,
                    "uncertain",
                    None,
                    "Compare competing evidence; retain uncertainty.",
                    count - counter,
                    counter,
                    None,
                    tuple(json.loads(alternatives)),
                    None,
                    (),
                    None,
                    "unverified",
                    "model_inference",
                ),
                revision,
                kind,
            )
            for op, kind, value, confidence, counter, count, revision, alternatives in rows
            if self._reviewed_consent_live(db, op, principal.principal_id.value)
        )

    def _reviewed_consent_live(self, db, operation_id: str, audience: str) -> bool:
        if (
            db.execute(
                "SELECT 1 FROM reviewed_shared_decisions WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            is None
        ):
            return True
        for event_id, source_principal, consent_id in db.execute(
            """SELECT c.event_id,s.source_principal,c.consent_id
            FROM protected_reviewed_consents c
            JOIN protected_shared_sources s ON s.operation_id=c.operation_id
                AND s.event_id=c.event_id
            WHERE c.operation_id=? AND c.audience=?""",
            (operation_id, audience),
        ):
            if (
                self._gate._ledger.active_shared_consent(
                    source_principal, event_id, audience, self._clock()
                )
                != consent_id
            ):
                return False
        return True
