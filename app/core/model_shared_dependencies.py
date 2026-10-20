"""Erasable, trusted bindings for every input shown to a shared proposal worker."""

import json


def validate_dependencies(db, data):
    """Closed representation, exact consent matrix, and protected review lineage."""
    if (
        type(data) is not dict
        or set(data)
        != {"version", "principal", "sources", "audience", "consents", "reviews"}
        or data["version"] != "shared-model-dependencies-v1"
        or type(data["principal"]) is not str
        or any(
            type(data[key]) is not list
            or any(type(value) is not str for value in data[key])
            or sorted(set(data[key])) != data[key]
            for key in ("sources", "audience", "reviews")
        )
        or not 1 <= len(data["sources"]) <= 50
        or not 1 <= len(data["audience"]) <= 16
        or len(data["reviews"]) > 1
        or type(data["consents"]) is not list
    ):
        raise ValueError("invalid shared model dependencies")
    expected = [
        (source, audience)
        for source in data["sources"]
        for audience in data["audience"]
    ]
    if len(data["consents"]) != len(expected) or any(
        type(row) is not list
        or len(row) != 3
        or tuple(row[:2]) != pair
        or (row[2] is not None and type(row[2]) is not str)
        for row, pair in zip(data["consents"], expected)
    ):
        raise ValueError("invalid shared model consents")
    for source in data["sources"]:
        if (
            db.execute(
                "SELECT 1 FROM event_envelopes e JOIN private_payloads p USING(payload_id) "
                "WHERE e.event_id=? AND e.audience_principal_id=? AND e.event_type='participant_input' "
                "AND p.retention_status='active'",
                (source, data["principal"]),
            ).fetchone()
            is None
        ):
            raise ValueError("shared model source unavailable")
    for operation in data["reviews"]:
        row = db.execute(
            "SELECT status,author_principal FROM shared_operations WHERE operation_id=?",
            (operation,),
        ).fetchone()
        sources = {
            row[0]
            for row in db.execute(
                "SELECT event_id FROM protected_shared_sources WHERE operation_id=?",
                (operation,),
            )
        }
        audience = {
            row[0]
            for row in db.execute(
                "SELECT principal_id FROM protected_shared_audiences WHERE operation_id=?",
                (operation,),
            )
        }
        if (
            row != ("active", data["principal"])
            or not sources <= set(data["sources"])
            or not set(data["audience"]) <= audience
        ):
            raise ValueError("shared model review unavailable")


def dependency_records(db):
    """Both pending/received receipts and persisted candidate manifests."""
    for operation, binding in db.execute(
        "SELECT operation_id,binding FROM protected_shared_manifests"
    ).fetchall():
        data = json.loads(binding)
        if "model_dependencies" in data:
            yield operation, data["model_dependencies"], False
    if db.execute("PRAGMA user_version").fetchone()[0] >= 11:
        for operation, proposal in db.execute(
            "SELECT operation_id,proposal FROM private_extraction_receipts WHERE operation_id LIKE 'sharedgen:%'"
        ).fetchall():
            data = json.loads(proposal)
            if type(data) is not dict or data.get("version") not in {
                "shared-proposal-receipt-v1",
                "shared-proposal-receipt-v2",
            }:
                raise ValueError("invalid shared receipt version")
            if data.get("version") == "shared-proposal-receipt-v2":
                yield operation, data["snapshot"]["dependencies"], True


def invalidate_dependencies(db, *, consent_id=None, scope=None):
    """Reach a fixed point before deleting manifests; invalidate complete items."""
    records = list(dependency_records(db))
    while True:
        invalid = {
            row[0]
            for row in db.execute(
                "SELECT operation_id FROM shared_operations WHERE status='invalidated'"
            )
        }
        affected = []
        for operation, data, receipt in records:
            related = consent_id is not None and any(
                bound == consent_id
                or (
                    bound is None
                    and scope
                    == (data["principal"], source, audience, "shared_abstraction")
                )
                for source, audience, bound in data["consents"]
            )
            if related or operation in invalid or invalid.intersection(data["reviews"]):
                affected.append(operation)
                if receipt:
                    db.execute(
                        "DELETE FROM private_extraction_receipts WHERE operation_id=?",
                        (operation,),
                    )
        changed = 0
        for operation in affected:
            changed += db.execute(
                "UPDATE shared_operations SET status='invalidated',reason='review_revoked' "
                "WHERE status<>'invalidated' AND item_id=(SELECT item_id FROM shared_operations WHERE operation_id=?)",
                (operation,),
            ).rowcount
        if not changed:
            return
