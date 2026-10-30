"""Erasable exact-question receipts in the existing protected receipt namespace."""

import json
import re

from app.core.store import EventStore
from app.trusted.public_research import PublicResearchRequest, encode_public_request


VERSION = "private-agent-research-question-v1"


def identity(operation):
    return "researchq:" + operation.removeprefix("reply:")


def snapshot(db, operation):
    row = db.execute(
        "SELECT initiating_event_id,audience_principal_id,retention_status FROM agent_reply_operations WHERE operation_id=?",
        (operation,),
    ).fetchone()
    if row is None or row[2] != "active":
        raise ValueError("research question reply unavailable")
    result = {}
    for key, table, column, operation_column in (
        ("sources", "agent_reply_sources", "event_id", "operation_id"),
        ("shared", "protected_reply_shared", "shared_operation", "operation_id"),
        ("generic", "protected_reply_generic", "generic_operation_id", "operation_id"),
        (
            "narrative",
            "protected_reply_narrative",
            "narrative_operation_id",
            "reply_operation_id",
        ),
    ):
        result[key] = [
            r[0]
            for r in db.execute(
                f"SELECT {column} FROM {table} WHERE {operation_column}=? ORDER BY {column}",
                (operation,),
            )
        ]
    return row[:2], result


def read(db, operation):
    row = db.execute(
        "SELECT source_event_id,source_principal_id,proposal FROM private_extraction_receipts WHERE operation_id=?",
        (identity(operation),),
    ).fetchone()
    if row is None:
        return None

    def unique(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError("duplicate question receipt field")
            data[key] = value
        return data

    if len(row[2].encode("utf-8")) > 32768:
        raise ValueError("oversized research question receipt")
    data = json.loads(row[2], object_pairs_hook=unique)
    if (
        type(data) is not dict
        or set(data) != {"version", "question", "profile_digest", "snapshot"}
        or data["version"] != VERSION
        or type(data["profile_digest"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", data["profile_digest"]) is None
        or type(data["question"]) is not str
        or len(data["question"].encode("utf-8")) > 512
    ):
        raise ValueError("invalid research question receipt")
    encode_public_request(
        PublicResearchRequest("question-validation", data["question"], "0" * 64)
    )
    routing, current = snapshot(db, operation)
    if row[:2] != routing or data["snapshot"] != current:
        raise ValueError("research question context changed")
    for key, values in current.items():
        if len(values) > (100 if key == "sources" else 16) or (
            key == "sources" and not values
        ):
            raise ValueError("research question dependency bound")
        for value in values:
            EventStore._require_token("question dependency", value)
    return data


def bind(db, operation, question, profile_digest):
    routing, current = snapshot(db, operation)
    data = {
        "version": VERSION,
        "question": question,
        "profile_digest": profile_digest,
        "snapshot": current,
    }
    encoded = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    previous = read(db, operation)
    if previous is not None:
        if previous != data:
            raise ValueError("research question identity conflict")
        return
    db.execute(
        "INSERT INTO private_extraction_receipts VALUES(?,?,?,?)",
        (identity(operation), *routing, encoded),
    )
    read(db, operation)


def validate_receipts(db):
    for (operation,) in db.execute(
        "SELECT operation_id FROM private_extraction_receipts WHERE operation_id LIKE 'researchq:%'"
    ).fetchall():
        read(db, "reply:" + operation.removeprefix("researchq:"))
