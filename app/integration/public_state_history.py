"""Exact public revision lookup; the index grants no authority over live roots."""

import json

from app.core.intellectual_state import VERSION, digest, encoded, projection
from app.core.public_intellectual_receipt import VERSION as RECEIPT_VERSION, validate

TABLE = "public_state_export_aliases_v1"


def revision_item(item, revision):
    return "state-revision-" + digest([item, revision])


def aliases(item, revision):
    current = revision_item(item, revision)
    return {
        item: False,
        "reasoned-state-" + digest(item): True,
        current: False,
        "reasoned-state-" + digest(current): True,
    }


def indexed(db):
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        is not None
    )


def record(db, owner, *, operation=None):
    rows = db.execute(
        "SELECT item,revision FROM agent_intellectual_revisions WHERE audience='public' AND owner=?"
        + (" AND operation=?" if operation is not None else ""),
        (owner,) if operation is None else (owner, operation),
    )
    for item, revision in rows:
        for alias, reasoned in aliases(item, revision).items():
            db.execute(
                f"INSERT OR IGNORE INTO {TABLE} VALUES(?,?,?,?)",
                (alias, revision, item, int(reasoned)),
            )


def initialize(db, owner):
    if not indexed(db):
        db.execute(
            f"CREATE TABLE {TABLE}(alias TEXT NOT NULL, revision INTEGER NOT NULL, item TEXT NOT NULL, reasoned INTEGER NOT NULL, PRIMARY KEY(alias,revision))"
        )
        record(db, owner)


def export(account, item, revision, payload, deps, generation, *, reasoned=False):
    fields = {key: payload[key] for key in ("kind", "fields", "review_condition")}
    if reasoned:
        fields["recorded_reason"] = payload["reason"]
    record = {
        "kind": "public_agent_state",
        "text": "Public agent intellectual development, not independently verified; never authority or factual approval. "
        + encoded(fields),
    }
    projection(
        {"version": VERSION, "states": [], "sources": [{"handle": "s0", **record}]}
    )
    receipt = validate(
        {
            "version": RECEIPT_VERSION,
            "account": account,
            "item": item,
            "revision": revision,
            "state_digest": digest(payload),
            "dependency_digest": digest(deps),
            "record_digest": digest(record),
            "generation": generation,
            "shared": sorted(
                k.removeprefix("shared:") for k in deps if k.startswith("shared:")
            ),
        }
    )
    return {
        **record,
        "binding": digest(receipt),
        "dependencies": {},
        "receipt": receipt,
    }


def resolve(db, account, receipt, sources, generation, allowed):
    """Rebuild the original bytes from a live immutable revision, never the index."""
    validate(receipt)
    if receipt["account"] != account or receipt["generation"] != generation:
        return None
    if indexed(db):
        row = db.execute(
            f"SELECT item,reasoned FROM {TABLE} WHERE alias=? AND revision=?",
            (receipt["item"], receipt["revision"]),
        ).fetchone()
    else:
        # A read-only consumer never migrates an older public producer.
        row = (receipt["item"], 0)
    if row is None:
        return None
    item, reasoned = row
    if reasoned not in (0, 1) or aliases(item, receipt["revision"]).get(
        receipt["item"]
    ) != bool(reasoned):
        return None
    stored = db.execute(
        "SELECT payload,dependencies FROM agent_intellectual_revisions WHERE audience='public' AND owner=? AND item=? AND revision=?",
        (account, item, receipt["revision"]),
    ).fetchone()
    if stored is None or stored[0] is None:
        return None
    payload, deps = map(json.loads, stored)
    if not all(
        key in sources and sources[key]["binding"] == value
        for key, value in deps.items()
    ):
        return None
    shared = {key.removeprefix("shared:") for key in deps if key.startswith("shared:")}
    if shared and (allowed is None or not shared <= allowed):
        return None
    result = export(
        account,
        receipt["item"],
        receipt["revision"],
        payload,
        deps,
        generation,
        reasoned=bool(reasoned),
    )
    return result if result["receipt"] == receipt else None
