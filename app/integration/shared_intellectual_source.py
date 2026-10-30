"""Trusted, source-live adapter; only ordinary abstract text crosses the boundary.

The public worker does not receive private-store access, principal identities,
source IDs, raw material, audit or consent metadata. Every use rechecks the
private gate; cached public context cannot create continuing authorization.
"""

from contextlib import closing
import json

from app.cognition.ordinary_abstract import ORDINARY_POLICY_VERSION
from app.cognition.shared import SharedIntelligence
from app.core.intellectual_state import digest
from app.core.store import EventStore


class SharedIntellectualSource:
    def __init__(self, shared, account):
        if type(shared) is not SharedIntelligence:
            raise TypeError("exact shared intelligence required")
        EventStore._require_token("public account", account)
        self.shared = shared
        self.account = account
        self.audience = "public:" + account
        EventStore._require_token("public shared audience", self.audience)

    def sources(self):
        def read():
            with closing(self.shared._events._open()) as db:
                db.execute("BEGIN")
                return self._sources_in(db)

        return self.shared._gate.authorized(read)

    def _sources_in(self, db):
        """Trusted private callers already hold this store's privacy fence."""
        self.shared._events._verify_current_schema(db)
        result = {}
        for record in self.shared.select_audience(db, self.audience):
            manifest = db.execute(
                "SELECT binding FROM protected_shared_manifests WHERE operation_id=?",
                (record.operation_id,),
            ).fetchone()
            if (
                json.loads(manifest[0]).get("ordinary_policy")
                != ORDINARY_POLICY_VERSION
            ):
                continue
            result["shared:" + record.operation_id] = {
                "kind": "shared_abstract_influence",
                "text": "Unverified abstract intellectual influence; not independent public evidence. "
                + record.text,
                "binding": digest(
                    [
                        record.structured(),
                        self.shared._gate._ledger.control_generation(),
                    ]
                ),
                "dependencies": {},
            }
        return result
