"""Protected graph validation and erasure; never a model-facing read API."""

import sqlite3
import hashlib
import json

from .model_shared_dependencies import (
    dependency_records,
    invalidate_dependencies,
    validate_dependencies,
)


_CHECKS = (
    """SELECT 1 FROM shared_mission_document d WHERE d.status='ready' AND (
        NOT json_valid(d.document)
        OR d.current_version<>json_extract(d.document,'$.current.version')
        OR (SELECT COUNT(*) FROM protected_mission_shared)<>
           (SELECT COUNT(*) FROM json_each(d.document,'$.proposals')) +
           (SELECT COUNT(*) FROM json_each(d.document,'$.proposals') j
              WHERE json_extract(j.value,'$.confirmation') IS NOT NULL)
        OR EXISTS (SELECT 1 FROM json_each(d.document,'$.proposals') j
            WHERE NOT EXISTS (SELECT 1 FROM protected_mission_shared m
                WHERE m.proposal_id=json_extract(j.value,'$.proposal.proposal_id')
                    AND m.phase='proposal'
                    AND m.shared_operation=json_extract(j.value,'$.proposal.initiating_event_id'))
            OR (json_extract(j.value,'$.confirmation') IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM protected_mission_shared m
                WHERE m.proposal_id=json_extract(j.value,'$.proposal.proposal_id')
                    AND m.phase='confirmation'
                    AND m.shared_operation=json_extract(j.value,'$.confirmation.evidence_ids[0]')))))""",
    """SELECT 1 FROM shared_operations o LEFT JOIN protected_shared_manifests m USING(operation_id)
        LEFT JOIN shared_payloads p USING(operation_id)
        WHERE (o.status='invalidated') <> (m.operation_id IS NULL)
        OR (o.status<>'invalidated' AND (
            json_type(m.binding) IS NOT 'object'
            OR json_type(m.binding,'$.candidate_digest') IS NOT 'text'
            OR length(json_extract(m.binding,'$.candidate_digest'))<>64
            OR json_type(m.binding,'$.kind') IS NOT 'text'
            OR json_type(m.binding,'$.confidence') IS NOT 'real'
            OR json_extract(m.binding,'$.kind')<>o.kind
            OR json_extract(m.binding,'$.confidence')<>p.confidence
            OR json_extract(m.binding,'$.item_id')<>o.item_id
            OR json_extract(m.binding,'$.revision')<>o.expected_revision
            OR json_extract(m.binding,'$.author')<>o.author_principal
            OR json_extract(m.binding,'$.mission_evidence')<>o.mission_evidence
            OR EXISTS (SELECT 1 FROM protected_shared_sources s WHERE s.operation_id=o.operation_id
                AND NOT EXISTS (SELECT 1 FROM json_each(m.binding,'$.sources') j
                    WHERE json_extract(j.value,'$[0]')=s.event_id
                        AND json_extract(j.value,'$[1]')=s.source_principal
                        AND json_extract(j.value,'$[2]')=s.role
                        AND json_extract(j.value,'$[3]')=s.classification))
            OR o.source_count<>json_array_length(m.binding,'$.sources')
            OR o.audience_count<>json_array_length(m.binding,'$.audience')
            OR EXISTS (SELECT 1 FROM protected_shared_audiences a WHERE a.operation_id=o.operation_id
                AND NOT EXISTS (SELECT 1 FROM json_each(m.binding,'$.audience') j
                    WHERE j.value=a.principal_id))))""",
    """SELECT 1 FROM shared_operations o WHERE
        (o.status<>'invalidated' AND EXISTS (SELECT 1 FROM shared_operations h
            WHERE h.item_id=o.item_id AND h.status='invalidated'))
        OR o.source_count <> (SELECT COUNT(*) FROM protected_shared_sources s
            WHERE s.operation_id=o.operation_id)
        OR o.audience_count <> (SELECT COUNT(*) FROM protected_shared_audiences a
            WHERE a.operation_id=o.operation_id)
        OR NOT EXISTS (SELECT 1 FROM protected_shared_sources s
            WHERE s.operation_id=o.operation_id AND s.role<>'inherited')
        OR (o.status='active' AND (o.reason<>'approved' OR
            o.screen_version<>'abstract-privacy-screen-v1' OR
            o.screen_result<>'shared_commit_disabled'))
        OR (o.status='invalidated') <> NOT EXISTS (SELECT 1 FROM shared_payloads p
            WHERE p.operation_id=o.operation_id)""",
    """SELECT 1 FROM protected_shared_sources s JOIN shared_operations o USING(operation_id)
        JOIN event_envelopes e USING(event_id)
        LEFT JOIN private_payloads p ON p.payload_id=e.payload_id
        LEFT JOIN protected_event_taints t ON t.event_id=e.event_id
        WHERE t.event_id IS NULL OR t.source_event_id<>e.event_id
        OR t.source_principal_id<>e.audience_principal_id
        OR t.source_provenance_type<>e.provenance_type
        OR e.event_type<>'participant_input' OR e.channel<>'private_chat'
        OR s.source_principal<>e.audience_principal_id
        OR e.audience_type<>'private_participant' OR e.provenance_type<>'participant_assertion'
        OR e.verification_status<>'unverified'
        OR (o.status<>'invalidated' AND (p.payload_id IS NULL
            OR p.audience_principal_id<>s.source_principal
            OR p.audience_type<>e.audience_type OR p.provenance_type<>e.provenance_type
            OR p.verification_status<>e.verification_status OR p.retention_status<>'active'))
        OR (s.role<>'inherited' AND s.source_principal<>o.author_principal)""",
    """SELECT 1 FROM protected_shared_audiences a JOIN shared_operations o USING(operation_id)
        WHERE a.purpose<>o.purpose""",
    """SELECT 1 FROM shared_operations o LEFT JOIN shared_operations p
        ON p.operation_id=o.parent_operation WHERE
        (o.expected_revision=0) <> (o.parent_operation IS NULL)
        OR (o.parent_operation IS NOT NULL AND (p.item_id<>o.item_id
            OR p.revision<>o.expected_revision OR p.status='quarantined'
            OR (o.status<>'invalidated' AND p.status<>'active')
            OR EXISTS (SELECT 1 FROM protected_shared_sources s
                WHERE s.operation_id=p.operation_id AND NOT EXISTS (
                    SELECT 1 FROM protected_shared_sources n
                    WHERE n.operation_id=o.operation_id AND n.event_id=s.event_id
                        AND n.source_principal=s.source_principal
                        AND n.role='inherited' AND n.classification=s.classification))
            OR EXISTS (SELECT 1 FROM protected_shared_audiences a
                WHERE a.operation_id=o.operation_id AND NOT EXISTS (
                    SELECT 1 FROM protected_shared_audiences b
                    WHERE b.operation_id=p.operation_id AND b.principal_id=a.principal_id))))""",
    """SELECT 1 FROM protected_shared_sources s JOIN shared_operations o USING(operation_id)
        WHERE s.role='inherited' AND NOT EXISTS (
            SELECT 1 FROM protected_shared_sources p WHERE p.operation_id=o.parent_operation
                AND p.event_id=s.event_id AND p.source_principal=s.source_principal
                AND p.classification=s.classification)""",
    """SELECT 1 FROM agent_reply_operations o WHERE o.shared_count <>
        (SELECT COUNT(*) FROM protected_reply_shared s WHERE s.operation_id=o.operation_id)""",
    """SELECT 1 FROM protected_reply_shared s JOIN agent_reply_operations r USING(operation_id)
        JOIN shared_operations o ON o.operation_id=s.shared_operation
        WHERE NOT EXISTS (SELECT 1 FROM protected_shared_audiences a
            WHERE a.operation_id=o.operation_id AND a.principal_id=r.audience_principal_id
                AND a.purpose='conversation')
        OR (r.retention_status='active' AND o.status<>'active')""",
    """SELECT 1 FROM shared_mission_document d WHERE d.status='ready' AND EXISTS (
        SELECT 1 FROM protected_mission_shared m JOIN shared_operations o
            ON o.operation_id=m.shared_operation WHERE o.status<>'active' OR o.mission_evidence<>1)""",
    """SELECT 1 FROM shared_operations o LEFT JOIN protected_shared_manifests m USING(operation_id)
        LEFT JOIN reviewed_shared_decisions d USING(operation_id)
        WHERE (o.status<>'invalidated' AND COALESCE(json_extract(m.binding,'$.reviewed'),0)=1
            AND (o.mission_evidence<>0 OR
                (o.status='active' AND (d.decision IS NULL OR d.decision<>'approved')) OR
                (d.decision='approved' AND o.status<>'active') OR
                (d.decision='rejected' AND o.status<>'quarantined')))
        OR (o.status<>'invalidated' AND COALESCE(json_extract(m.binding,'$.reviewed'),0)<>1
            AND d.operation_id IS NOT NULL)""",
    """SELECT 1 FROM protected_reviewed_consents c
        JOIN shared_operations o USING(operation_id)
        LEFT JOIN protected_shared_sources s ON s.operation_id=c.operation_id AND s.event_id=c.event_id
        LEFT JOIN protected_shared_audiences a ON a.operation_id=c.operation_id AND a.principal_id=c.audience
        WHERE s.event_id IS NULL OR a.principal_id IS NULL OR o.status='quarantined'""",
    """SELECT 1 FROM reviewed_shared_decisions d JOIN shared_operations o USING(operation_id)
        WHERE d.decision='approved' AND o.status='active' AND
            (SELECT COUNT(*) FROM protected_reviewed_consents c WHERE c.operation_id=o.operation_id)
            <>o.source_count*o.audience_count""",
    """SELECT 1 FROM reviewed_shared_revocations r JOIN shared_operations o USING(operation_id)
        WHERE o.status<>'invalidated'""",
)


def validate_shared_graph(db: sqlite3.Connection) -> None:
    """Counts, complete inherited dependencies, scope, and retention fail closed."""
    checks = (
        _CHECKS
        if db.execute("PRAGMA user_version").fetchone()[0] >= 11
        else _CHECKS[:-4]
    )
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None or any(
        db.execute(sql).fetchone() is not None for sql in checks
    ):
        raise ValueError("invalid_protected_shared_graph")
    if any(
        hashlib.sha256(value.encode("utf-8")).hexdigest()
        != json.loads(binding)["candidate_digest"]
        for value, binding in db.execute(
            "SELECT p.value, m.binding FROM shared_operations o JOIN shared_payloads p USING(operation_id) JOIN protected_shared_manifests m USING(operation_id) WHERE o.status='active'"
        )
    ):
        raise ValueError("shared_payload_binding_mismatch")
    for operation, dependencies, receipt in dependency_records(db):
        validate_dependencies(db, dependencies)
        if receipt:
            row = db.execute(
                "SELECT source_principal_id,source_event_id,proposal FROM private_extraction_receipts WHERE operation_id=?",
                (operation,),
            ).fetchone()
            snapshot = json.loads(row[2])["snapshot"]
            if (
                row[0] != dependencies["principal"]
                or row[1] not in dependencies["sources"]
                or snapshot["sources"] != dependencies["sources"]
                or sorted(item[3] for item in snapshot["selected"])
                != dependencies["reviews"]
            ):
                raise ValueError("shared model receipt binding mismatch")
        else:
            row = db.execute(
                "SELECT author_principal,parent_operation FROM shared_operations WHERE operation_id=?",
                (operation,),
            ).fetchone()
            sources = sorted(
                row[0]
                for row in db.execute(
                    "SELECT event_id FROM protected_shared_sources WHERE operation_id=?",
                    (operation,),
                )
            )
            audience = sorted(
                row[0]
                for row in db.execute(
                    "SELECT principal_id FROM protected_shared_audiences WHERE operation_id=?",
                    (operation,),
                )
            )
            if (
                row[0] != dependencies["principal"]
                or sources != dependencies["sources"]
                or audience != dependencies["audience"]
                or (row[1] is not None and row[1] not in dependencies["reviews"])
            ):
                raise ValueError("shared model candidate binding mismatch")


def erase_shared_source(db: sqlite3.Connection, event_id: str) -> None:
    """Called inside the reviewed pending-first EventStore erasure transaction."""
    db.execute(
        """UPDATE shared_operations SET status='invalidated', reason='source_invalidated'
        WHERE item_id IN (SELECT o.item_id FROM shared_operations o
            JOIN protected_shared_sources s USING(operation_id) WHERE s.event_id=?)""",
        (event_id,),
    )
    _erase_invalidated_shared(db)


def erase_shared_review(db: sqlite3.Connection, consent_id: str) -> None:
    """A revoked source consent invalidates its reviewed item and dependent replies."""
    db.execute(
        """UPDATE shared_operations SET status='invalidated', reason='review_revoked'
        WHERE item_id IN (SELECT o.item_id FROM shared_operations o
            JOIN protected_reviewed_consents c USING(operation_id)
            WHERE c.consent_id=?)""",
        (consent_id,),
    )
    _erase_invalidated_shared(db)


def revoke_shared_authorization(db: sqlite3.Connection, operation_id: str) -> None:
    db.execute(
        """UPDATE shared_operations SET status='invalidated', reason='review_revoked'
        WHERE item_id=(SELECT item_id FROM shared_operations WHERE operation_id=?)""",
        (operation_id,),
    )
    _erase_invalidated_shared(db)


def erase_model_shared_candidates(
    db: sqlite3.Connection, consent_id: str, scope
) -> None:
    """Only exact consent dependencies and unconsented inputs in that grant's scope."""
    invalidate_dependencies(db, consent_id=consent_id, scope=scope)
    # Legacy candidates retain authoritative source/audience lineage, though not
    # the model's reviewed input. Match the revoked grant's exact scope only.
    if scope is not None and scope[3] == "shared_abstraction":
        db.execute(
            """UPDATE shared_operations SET status='invalidated',reason='review_revoked'
            WHERE item_id IN (SELECT o.item_id FROM shared_operations o
                JOIN protected_shared_sources s USING(operation_id)
                JOIN protected_shared_audiences a USING(operation_id)
                JOIN protected_shared_manifests m USING(operation_id)
                WHERE o.operation_id LIKE 'sharedgen:%'
                AND json_type(m.binding,'$.model_dependencies') IS NULL
                AND s.source_principal=? AND s.event_id=? AND a.principal_id=?)""",
            scope[:3],
        )
    _erase_invalidated_shared(db)


def _erase_invalidated_shared(db: sqlite3.Connection) -> None:
    invalidate_dependencies(db)
    db.execute(
        "DELETE FROM protected_shared_manifests WHERE operation_id IN "
        "(SELECT operation_id FROM shared_operations WHERE status='invalidated')"
    )
    if db.execute("PRAGMA user_version").fetchone()[0] >= 11:
        db.execute(
            "DELETE FROM protected_reviewed_consents WHERE operation_id IN "
            "(SELECT operation_id FROM shared_operations WHERE status='invalidated')"
        )
    db.execute(
        "DELETE FROM shared_payloads WHERE operation_id IN "
        "(SELECT operation_id FROM shared_operations WHERE status='invalidated')"
    )
    db.execute(
        """UPDATE agent_reply_operations SET retention_status='invalidated'
        WHERE operation_id IN (SELECT s.operation_id FROM protected_reply_shared s
            JOIN shared_operations o ON o.operation_id=s.shared_operation
            WHERE o.status='invalidated')"""
    )
    db.execute(
        "DELETE FROM agent_reply_payloads WHERE operation_id IN "
        "(SELECT operation_id FROM agent_reply_operations WHERE retention_status='invalidated')"
    )
    db.execute(
        "UPDATE agent_reply_sources SET retention_status='invalidated' WHERE operation_id IN "
        "(SELECT operation_id FROM agent_reply_operations WHERE retention_status='invalidated')"
    )
    from app.core.intellectual_state import erase_source

    for (operation,) in db.execute(
        "SELECT operation_id FROM agent_reply_operations WHERE retention_status='invalidated'"
    ):
        erase_source(db, "context:" + operation)
    db.execute(
        """UPDATE shared_mission_document SET status='reassessment', document=NULL
        WHERE EXISTS (SELECT 1 FROM protected_mission_shared m JOIN shared_operations o
            ON o.operation_id=m.shared_operation WHERE o.status='invalidated')"""
    )
    if db.execute("PRAGMA user_version").fetchone()[0] >= 9:
        db.execute(
            "DELETE FROM private_extraction_receipts WHERE operation_id IN ("
            "SELECT 'researchq:' || substr(operation_id,7) FROM agent_reply_operations WHERE retention_status='invalidated')"
        )
