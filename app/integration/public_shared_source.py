"""Explicit application-only composition of existing private shared intelligence."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat

from app.cognition.shared import SharedIntelligence
from app.core.public_behavior import _unique_fields
from app.core.read_only_sqlite import connect_read_only
from app.core.store import EventStore
from app.integration.shared_intellectual_source import SharedIntellectualSource
from app.privacy.application import PrivacyReadGate
from app.privacy.ledger import PrivacyLedger
from app.trusted.openai_credential_files import read_protected_worker_file


def from_config(path, *, account, owner_directory, public_paths, clock):
    value = json.loads(
        read_protected_worker_file(Path(path), maximum_bytes=4096),
        object_pairs_hook=_unique_fields,
    )
    if (
        type(value) is not dict
        or set(value)
        != {"version", "account", "event_store_path", "ledger_path", "owner_directory"}
        or value["version"] != "public-shared-source-v1"
        or value["account"] != account
    ):
        raise ValueError("invalid public shared-source binding")
    owner = Path(value["owner_directory"])
    if not owner.is_absolute() or owner.resolve() != Path(owner_directory).resolve():
        raise ValueError("shared source requires common owner Stop")
    events, ledger = Path(value["event_store_path"]), Path(value["ledger_path"])
    forbidden = {Path(item).resolve() for item in public_paths}
    if (
        events.resolve() in forbidden
        or ledger.resolve() in forbidden
        or events.resolve() == ledger.resolve()
    ):
        raise ValueError("private and public stores must remain distinct")
    # Admission is read-only. Never bootstrap or migrate a missing source store.
    for kind, source in (
        ("events", events),
        ("ledger", ledger),
        ("owner", owner / "owner.sqlite"),
    ):
        entry = source.lstat()
        if (
            not source.is_absolute()
            or source.resolve() != source
            or not stat.S_ISREG(entry.st_mode)
            or entry.st_uid != os.geteuid()
            or stat.S_IMODE(entry.st_mode) != 0o600
            or entry.st_nlink != 1
        ):
            raise ValueError("unsafe existing shared-source store")
        try:
            with closing(connect_read_only(source)) as db:
                if kind == "events":
                    EventStore._verify_current_schema(db)
                elif kind == "ledger":
                    if not PrivacyLedger._has_valid_schema(db):
                        raise ValueError("uninitialized shared-source ledger")
                elif db.execute("SELECT version FROM owner_schema").fetchall() != [
                    (1,)
                ]:
                    raise ValueError("uninitialized shared-source owner")
        except (sqlite3.Error, RuntimeError):
            raise ValueError("invalid shared-source schema") from None
    private = EventStore(events)
    gate = PrivacyReadGate(PrivacyLedger(ledger), private, clock=clock)
    if not gate.reconcile():
        raise ValueError("shared source privacy reconciliation blocked")
    shared = SharedIntelligence(
        private, gate, clock=clock, ordinary_abstractions_enabled=True
    )
    return SharedIntellectualSource(shared, account)
