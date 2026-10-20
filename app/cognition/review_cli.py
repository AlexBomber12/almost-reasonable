"""Local OS-authorized inspection and decision interface for reviewed sharing."""

import argparse
from contextlib import closing
from datetime import UTC, datetime
import json

from app.core.events import (
    AuthenticatedSourceReference,
    ParticipantId,
    TrustedPrincipalContext,
    VerificationStatus,
)
from app.core.shared_graph import validate_shared_graph
from app.core.store import EventStore
from app.privacy.application import PrivacyReadGate
from app.privacy.ledger import PrivacyLedger

from .contracts import AbstractCandidate, AbstractToSharedProposal, MemoryKind
from .shared import ReviewedSharedCandidate, SharedIntelligence


def _inspect(events: EventStore, gate: PrivacyReadGate, operation_id: str):
    def read():
        with closing(events._open()) as db, db:
            db.execute("BEGIN")
            events._verify_current_schema(db)
            validate_shared_graph(db)
            row = db.execute(
                """SELECT o.status,o.item_id,o.expected_revision,o.author_principal,
                    o.kind,p.value,p.confidence,p.verification,m.binding
                FROM shared_operations o JOIN shared_payloads p USING(operation_id)
                JOIN protected_shared_manifests m USING(operation_id)
                WHERE o.operation_id=?""",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise ValueError("review candidate unavailable")
            (
                status,
                item_id,
                revision,
                author,
                kind,
                value,
                confidence,
                verification,
                binding,
            ) = row
            manifest = json.loads(binding)
            if manifest.get("reviewed") is not True:
                raise ValueError("reviewed candidate required")
            sources = db.execute(
                """SELECT s.event_id,s.source_principal,s.role,s.classification,p.content
                FROM protected_shared_sources s JOIN event_envelopes e USING(event_id)
                JOIN private_payloads p ON p.payload_id=e.payload_id
                WHERE s.operation_id=? ORDER BY s.event_id""",
                (operation_id,),
            ).fetchall()
            candidate = ReviewedSharedCandidate(
                TrustedPrincipalContext(
                    ParticipantId(author), AuthenticatedSourceReference("local-review")
                ),
                AbstractToSharedProposal(
                    operation_id,
                    AbstractCandidate(
                        MemoryKind(kind),
                        value,
                        confidence,
                        VerificationStatus(verification),
                    ),
                    tuple(source[0] for source in sources if source[2] != "inherited"),
                    datetime.fromisoformat(manifest["proposed_at"]),
                ),
                item_id,
                revision,
                tuple(manifest["audience"]),
                tuple(source[0] for source in sources if source[2] == "counter"),
            )
            return status, candidate, sources

    return gate.authorized(read)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Local reviewed shared abstraction control"
    )
    parser.add_argument("action", choices=("inspect", "approve", "reject", "revoke"))
    parser.add_argument("--events", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--decision-id")
    parser.add_argument("--review-version", default="local-review-v1")
    args = parser.parse_args(argv)
    EventStore._require_token("shared operation", args.operation_id)
    events = EventStore(args.events)
    ledger = PrivacyLedger(args.ledger)
    gate = PrivacyReadGate(ledger, events)
    if not gate.reconcile():
        raise ValueError("privacy reconciliation required")
    shared = SharedIntelligence(events, gate, clock=lambda: datetime.now(UTC))
    if args.action == "revoke":
        if args.decision_id is None:
            raise ValueError("revocation identity required")
        result = shared.revoke_review(args.operation_id, args.decision_id)
        print(result)
        return result
    status, candidate, sources = _inspect(events, gate, args.operation_id)
    print(
        json.dumps(
            {
                "operation_id": args.operation_id,
                "status": status,
                "candidate": candidate.proposal.candidate.value,
                "audience": candidate.audience,
                "purpose": "conversation",
                "source_material": sources,
            },
            ensure_ascii=False,
        )
    )
    if args.action == "inspect":
        return status
    if args.decision_id is None:
        raise ValueError("decision identity required")
    choice = args.action.upper()
    if (
        input(f"Type {choice} {args.operation_id} to confirm: ")
        != f"{choice} {args.operation_id}"
    ):
        raise ValueError("local review confirmation failed")
    result = shared.review(
        candidate,
        decision_id=args.decision_id,
        review_version=args.review_version,
        approve=args.action == "approve",
    )
    print(result)
    return result


if __name__ == "__main__":
    main()
