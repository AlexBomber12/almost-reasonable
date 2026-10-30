"""Source-live scheduling preferences within the owner's existing finite limits."""

from datetime import datetime
import json
import math

from app.core.intellectual_state import digest
from app.core.read_only_sqlite import connect_read_only
from app.threads.service import PolicyDeniedError


def cadence_sources(path, generation):
    with connect_read_only(path) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='public_cadence_policy'"
        ).fetchone():
            return {}
        row = db.execute(
            "SELECT enabled,minimum,maximum FROM public_cadence_policy WHERE singleton=1"
        ).fetchone()
    if row is None or row[0] != 1:
        return {}
    return {
        "cadence-policy": {
            "kind": "capability_snapshot",
            "text": f"Optional cadence_preference seconds must be an integer from {row[1]} to {row[2]}. This describes existing owner bounds, never grants tools, spending or publication authority.",
            "binding": digest([list(row), generation]),
            "dependencies": {},
        }
    }


class AgentCadence:
    def __init__(self, app, enabled, policy):
        if type(enabled) is not bool:
            raise ValueError("invalid agent cadence mode")
        self.app, self.enabled = app, enabled
        self.minimum = max(1, math.ceil(policy.cadence.total_seconds()))
        if enabled and self.minimum > 86400:
            raise ValueError("owner cadence exceeds supported preference bound")
        with app.journal.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS public_cadence_policy(singleton INTEGER PRIMARY KEY CHECK(singleton=1),enabled INTEGER NOT NULL,minimum INTEGER NOT NULL,maximum INTEGER NOT NULL)"
            )
            db.execute(
                "INSERT OR REPLACE INTO public_cadence_policy VALUES(1,?,?,86400)",
                (int(enabled), self.minimum),
            )

    def guard(self):
        app = self.app
        operation = app.scheduler.repository.active_agent_cadence()
        if operation is not None and (
            not self.enabled or not app.intellectual.valid(operation, revisions=False)
        ):
            raise PolicyDeniedError(
                "agent cadence source withdrawn; operator reset required"
            )

    def apply(self, operation, payload):
        app = self.app
        if (
            not self.enabled
            or payload["kind"] != "wake"
            or app.operating_window is None
        ):
            return
        with connect_read_only(app.journal.path) as db:
            row = db.execute(
                "SELECT payload FROM agent_intellectual_revisions WHERE operation=?",
                (operation,),
            ).fetchone()
        if row is None:
            return
        value = json.loads(row[0])
        if value["kind"] != "cadence_preference":
            return
        seconds = value["fields"]["seconds"]
        if not self.minimum <= seconds <= 86400:
            raise PolicyDeniedError("cadence preference exceeds owner bounds")
        app.guard()
        app.require_mission(payload)
        if not app.source_valid(payload):
            raise PolicyDeniedError("cadence preference source changed")
        app.scheduler.repository.select_agent_cadence(
            operation, seconds, datetime.fromisoformat(payload["decided_at"])
        )
