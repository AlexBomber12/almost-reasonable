"""Protected historical routing receipts; current sources are checked separately."""

import re

from app.core.intellectual_state import digest, encoded
from app.threads.domain import require_reference

VERSION = "public-intellectual-receipt-v1"
PREFIX = "public-state:"
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def source_key(account, item):
    return PREFIX + digest([account, item])


def validate(value):
    if (
        type(value) is not dict
        or set(value)
        != {
            "version",
            "account",
            "item",
            "revision",
            "state_digest",
            "dependency_digest",
            "record_digest",
            "generation",
            "shared",
        }
        or value["version"] != VERSION
    ):
        raise ValueError("invalid public intellectual receipt")
    require_reference("public receipt account", value["account"])
    require_reference("public receipt item", value["item"])
    if (
        type(value["revision"]) is not int
        or not 1 <= value["revision"] <= 2**31 - 1
        or type(value["generation"]) is not int
        or value["generation"] < 0
        or any(
            type(value[key]) is not str or _HASH.fullmatch(value[key]) is None
            for key in ("state_digest", "dependency_digest", "record_digest")
        )
        or type(value["shared"]) is not list
        or len(value["shared"]) > 16
        or any(type(operation) is not str for operation in value["shared"])
        or value["shared"] != sorted(set(value["shared"]))
        or len(encoded(value).encode()) > 4096
    ):
        raise ValueError("invalid public intellectual receipt fields")
    for operation in value["shared"]:
        require_reference("public receipt shared ancestor", operation)
    return value


def collect(dependencies, sources):
    return {
        key: validate(sources[key]["receipt"])
        for key in dependencies
        if key.startswith(PREFIX)
    }


def validate_private(db, operation, snapshot):
    deps = snapshot["dependencies"]
    external = {key for key in deps if key.startswith(PREFIX)}
    receipts = snapshot.get("external_receipts", {})
    if type(receipts) is not dict or set(receipts) != external or len(receipts) > 6:
        raise ValueError("public intellectual receipt set mismatch")
    for key, receipt in receipts.items():
        validate(receipt)
        if key != source_key(receipt["account"], receipt["item"]) or deps[
            key
        ] != digest(receipt):
            raise ValueError("public intellectual receipt binding mismatch")
        for shared in receipt["shared"]:
            if (
                db.execute(
                    "SELECT 1 FROM protected_reply_shared WHERE operation_id=? AND shared_operation=?",
                    (operation, shared),
                ).fetchone()
                is None
            ):
                raise ValueError("public intellectual ancestor missing")
            for (event,) in db.execute(
                "SELECT event_id FROM protected_shared_sources WHERE operation_id=?",
                (shared,),
            ):
                if deps.get("event:" + event) != event:
                    raise ValueError("public intellectual root missing")
        for index, selected in enumerate(snapshot["source_keys"]):
            if selected == key:
                shown = snapshot["projection"]["sources"][index]
                if (
                    digest({name: shown[name] for name in ("kind", "text")})
                    != receipt["record_digest"]
                ):
                    raise ValueError("public intellectual projection mismatch")
