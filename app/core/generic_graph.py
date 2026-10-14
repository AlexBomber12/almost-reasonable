"""Protected generic-memory dependency validation and source erasure."""

import hashlib
import json
import sqlite3

MAX_GENERIC_GRAPH_OPERATIONS = 128
MAX_GENERIC_GRAPH_REPLIES = 4096


def validate_generic_graph(db: sqlite3.Connection) -> None:
    """Validate every retained identity, payload, edge, and complete source union."""
    limits = (
        ("generic_memory_items", MAX_GENERIC_GRAPH_OPERATIONS),
        ("generic_memory_operations", MAX_GENERIC_GRAPH_OPERATIONS),
        ("generic_memory_payloads", MAX_GENERIC_GRAPH_OPERATIONS),
        ("generic_memory_sources", MAX_GENERIC_GRAPH_OPERATIONS * 100),
        ("generic_memory_parents", MAX_GENERIC_GRAPH_OPERATIONS * 16),
        ("protected_reply_generic", MAX_GENERIC_GRAPH_REPLIES),
    )
    for table, bound in limits:
        if db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > bound:
            raise ValueError("invalid_generic_graph")
    if (
        db.execute(
            "SELECT COUNT(*) FROM agent_reply_operations WHERE generic_count>0"
        ).fetchone()[0]
        > MAX_GENERIC_GRAPH_REPLIES
    ):
        raise ValueError("invalid_generic_graph")
    items = {
        item_id: (principal, head)
        for item_id, principal, head in db.execute(
            "SELECT item_id,audience_principal_id,current_revision FROM generic_memory_items"
        )
    }
    operations = {
        operation_id: (
            item_id,
            revision,
            expected,
            source_count,
            parent_count,
            status,
            graph_digest,
        )
        for operation_id, item_id, revision, expected, source_count, parent_count, status, graph_digest in db.execute(
            """SELECT operation_id,item_id,revision,expected_revision,
                    source_count,parent_count,status,graph_digest
                FROM generic_memory_operations"""
        )
    }
    sources = {operation_id: set() for operation_id in operations}
    for operation_id, event_id, role, principal, retention in db.execute(
        """SELECT operation_id,event_id,role,source_principal_id,retention_status
        FROM generic_memory_sources"""
    ):
        operation = operations.get(operation_id)
        if (
            operation is None
            or operation[0] not in items
            or principal != items[operation[0]][0]
        ):
            raise ValueError("invalid_generic_graph")
        if (retention == "active") != (operation[5] == "active"):
            raise ValueError("invalid_generic_graph")
        event = db.execute(
            """SELECT e.audience_principal_id,e.event_type,e.channel,e.audience_type,
                e.provenance_type,e.verification_status,e.payload_id,p.retention_status,
                p.audience_principal_id,p.audience_type,p.provenance_type,
                p.verification_status,t.source_event_id,t.source_principal_id,
                t.source_provenance_type
            FROM event_envelopes e LEFT JOIN private_payloads p
                ON p.payload_id=e.payload_id
            LEFT JOIN protected_event_taints t ON t.event_id=e.event_id
            WHERE e.event_id=?""",
            (event_id,),
        ).fetchone()
        if (
            event is None
            or event[:6]
            != (
                principal,
                "participant_input",
                "private_chat",
                "private_participant",
                "participant_assertion",
                "unverified",
            )
            or (operation[5] == "active" and (event[6] is None or event[7] != "active"))
            or (
                operation[5] == "active"
                and event[8:12]
                != (
                    principal,
                    "private_participant",
                    "participant_assertion",
                    "unverified",
                )
            )
            or event[12:15] != (event_id, principal, "participant_assertion")
        ):
            raise ValueError("invalid_generic_graph")
        sources[operation_id].add((event_id, role))
    parents = {operation_id: set() for operation_id in operations}
    for operation_id, parent in db.execute(
        "SELECT operation_id,parent_operation_id FROM generic_memory_parents"
    ):
        if operation_id not in operations or parent not in operations:
            raise ValueError("invalid_generic_graph")
        parents[operation_id].add(parent)
    payloads = {
        operation_id: (
            kind,
            statement,
            confidence,
            verification,
            reason,
            review,
            review_state,
            binding,
        )
        for operation_id, kind, statement, confidence, verification, reason, review, review_state, binding in db.execute(
            "SELECT * FROM generic_memory_payloads"
        )
    }
    by_item = {item_id: {} for item_id in items}
    for operation_id, operation in operations.items():
        item_id, revision, expected, source_count, parent_count, status, digest = (
            operation
        )
        if item_id not in items or revision in by_item[item_id]:
            raise ValueError("invalid_generic_graph")
        by_item[item_id][revision] = operation_id
        if (
            source_count != len(sources[operation_id])
            or parent_count != len(parents[operation_id])
            or (status == "active") != (operation_id in payloads)
            or digest
            != hashlib.sha256(
                json.dumps(
                    [sorted(sources[operation_id]), sorted(parents[operation_id])],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        ):
            raise ValueError("invalid_generic_graph")
    for item_id, (_principal, head) in items.items():
        chain = by_item[item_id]
        if set(chain) != set(range(1, head + 1)):
            raise ValueError("invalid_generic_graph")
        statuses = {operations[operation_id][5] for operation_id in chain.values()}
        if len(statuses) != 1:
            raise ValueError("invalid_generic_graph")
        for revision, operation_id in chain.items():
            if operations[operation_id][2] != (None if revision == 1 else revision - 1):
                raise ValueError("invalid_generic_graph")
    visited = set()
    visiting = set()

    def visit(operation_id: str) -> None:
        if operation_id in visiting:
            raise ValueError("invalid_generic_graph")
        if operation_id in visited:
            return
        visiting.add(operation_id)
        item_id, revision, _expected, _source_count, _parent_count, status, _digest = (
            operations[operation_id]
        )
        inherited = set()
        if revision > 1:
            prior = by_item[item_id][revision - 1]
            visit(prior)
            inherited.update(sources[prior])
        for parent in parents[operation_id]:
            visit(parent)
            parent_item, parent_revision, *_ = operations[parent]
            if (
                parent_item == item_id
                or items[parent_item][0] != items[item_id][0]
                or parent_revision > items[parent_item][1]
                or (status == "active" and operations[parent][5] != "active")
            ):
                raise ValueError("invalid_generic_graph")
            inherited.update(sources[parent])
        if not inherited <= sources[operation_id]:
            raise ValueError("invalid_generic_graph")
        if status == "active":
            (
                kind,
                statement,
                confidence,
                verification,
                reason,
                review,
                review_state,
                binding,
            ) = payloads[operation_id]
            try:
                data = json.loads(binding)
                direct = {(data["source"], "initiating")}
                direct.update((event, "supporting") for event in data["support"])
                direct.update((event, "counterevidence") for event in data["counter"])
                bound_parents = {
                    (parent_item, parent_revision)
                    for parent_item, parent_revision in data["parents"]
                }
            except (TypeError, ValueError, KeyError, IndexError):
                raise ValueError("invalid_generic_graph") from None
            actual_parents = {
                (operations[p][0], operations[p][1]) for p in parents[operation_id]
            }
            if (
                data["item"] != item_id
                or data["expected"] != operations[operation_id][2]
                or data["kind"] != kind
                or data["statement"] != statement
                or data["confidence"] != confidence
                or data["verification"] != verification
                or data["reason"] != reason
                or data["review"] != review
                or data["review_state"] != review_state
                or data["model"]
                != db.execute(
                    "SELECT model_identifier FROM generic_memory_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()[0]
                or data["prompt"]
                != db.execute(
                    "SELECT prompt_version FROM generic_memory_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()[0]
                or data["core"]
                != db.execute(
                    "SELECT core_self_version FROM generic_memory_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()[0]
                or bound_parents != actual_parents
                or direct | inherited != sources[operation_id]
            ):
                raise ValueError("invalid_generic_graph")
        visiting.remove(operation_id)
        visited.add(operation_id)

    for operation_id in operations:
        visit(operation_id)
    for reply_id, reply_principal, reply_status, generic_count in db.execute(
        """SELECT operation_id,audience_principal_id,retention_status,generic_count
        FROM agent_reply_operations
        WHERE operation_id IN (
            SELECT operation_id FROM agent_reply_operations WHERE generic_count>0
            UNION SELECT operation_id FROM protected_reply_generic
        )"""
    ):
        generic_ids = [
            row[0]
            for row in db.execute(
                "SELECT generic_operation_id FROM protected_reply_generic WHERE operation_id=?",
                (reply_id,),
            )
        ]
        if len(generic_ids) != generic_count:
            raise ValueError("invalid_generic_graph")
        reply_sources = {
            row[0]
            for row in db.execute(
                "SELECT event_id FROM agent_reply_sources WHERE operation_id=?",
                (reply_id,),
            )
        }
        for generic_id in generic_ids:
            operation = operations.get(generic_id)
            if (
                operation is None
                or items[operation[0]][0] != reply_principal
                or (reply_status == "active" and operation[5] != "active")
                or not {source for source, _role in sources[generic_id]}
                <= reply_sources
            ):
                raise ValueError("invalid_generic_graph")


def erase_generic_source(db: sqlite3.Connection, event_id: str) -> None:
    affected = {
        row[0]
        for row in db.execute(
            """SELECT DISTINCT o.item_id FROM generic_memory_operations o
        JOIN generic_memory_sources s USING(operation_id) WHERE s.event_id=?""",
            (event_id,),
        )
    }
    while True:
        descendants = {
            row[0]
            for row in db.execute(
                """SELECT DISTINCT child.item_id FROM generic_memory_parents p
            JOIN generic_memory_operations parent
                ON parent.operation_id=p.parent_operation_id
            JOIN generic_memory_operations child
                ON child.operation_id=p.operation_id
            WHERE parent.item_id IN ({})""".format(
                    ",".join("?" for _ in affected) or "NULL"
                ),
                tuple(sorted(affected)),
            )
        }
        if descendants <= affected:
            break
        affected.update(descendants)
    for item_id in sorted(affected):
        db.execute(
            """UPDATE generic_memory_operations SET status='invalidated'
            WHERE item_id=?""",
            (item_id,),
        )
        db.execute(
            """DELETE FROM generic_memory_payloads WHERE operation_id IN
            (SELECT operation_id FROM generic_memory_operations WHERE item_id=?)""",
            (item_id,),
        )
        db.execute(
            """UPDATE generic_memory_sources SET retention_status='invalidated'
            WHERE operation_id IN
            (SELECT operation_id FROM generic_memory_operations WHERE item_id=?)""",
            (item_id,),
        )
    db.execute(
        """UPDATE agent_reply_operations SET retention_status='invalidated'
        WHERE operation_id IN (
            SELECT r.operation_id FROM protected_reply_generic r
            JOIN generic_memory_operations o
                ON o.operation_id=r.generic_operation_id
            WHERE o.status='invalidated')"""
    )
    db.execute(
        """DELETE FROM agent_reply_payloads WHERE operation_id IN
        (SELECT operation_id FROM agent_reply_operations WHERE retention_status='invalidated')"""
    )
    db.execute(
        """UPDATE agent_reply_sources SET retention_status='invalidated'
        WHERE operation_id IN
        (SELECT operation_id FROM agent_reply_operations WHERE retention_status='invalidated')"""
    )
