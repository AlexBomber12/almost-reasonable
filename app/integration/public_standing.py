"""Operator-authorized standing policy; issues only bounded daily public leases."""

from dataclasses import asdict, dataclass
from contextlib import closing
from datetime import UTC, timedelta
import json
import sqlite3
import time

from app.integration.public_grant import PublicGrantSpec
from app.integration.public_window import PublicOperatingWindow, WindowSpec
from app.threads.domain import require_reference
from app.threads.service import PolicyDeniedError
from app.trusted.public_monthly_budget import VERSION as BUDGET_VERSION


@dataclass(frozen=True, slots=True)
class StandingPublicPolicy:
    policy_id: str
    account_ref: str
    project_id: str
    version: str = "standing-public-v1"
    budget_version: str = BUDGET_VERSION

    def __post_init__(self):
        for value in (self.policy_id, self.account_ref):
            require_reference("standing public authority", value)
        require_reference(
            "standing lease reference", self.policy_id + "-99991231-grant"
        )
        if (
            not self.project_id.startswith("proj_")
            or self.version != "standing-public-v1"
            or self.budget_version != BUDGET_VERSION
        ):
            raise ValueError("invalid standing public budget binding")

    def lease(self, now):
        day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        reference = self.policy_id + "-" + day.strftime("%Y%m%d")
        grant_id = reference + "-grant"
        window = WindowSpec(
            reference,
            day + timedelta(days=1),
            96,
            24,
            24,
            3600,
            mission_evolution=True,
            publication_grant=grant_id,
        )
        grant = PublicGrantSpec(
            grant_id,
            self.account_ref,
            reference,
            window.expires_at,
            6,
            36,
            ("post", "reply"),
        )
        return window, grant


class StandingPublicRunner:
    """Keep root controls pinned across days and restarts; never clear any Stop."""

    def __init__(self, app, policy):
        self.app, self.policy = app, policy

    def _check(self):
        app = self.app
        app.operator.authenticate()
        app.guard()
        profile = app.current_research_profile()
        if (
            app.account != self.policy.account_ref
            or profile is None
            or not profile.standing_monthly
        ):
            raise PolicyDeniedError("standing public research authority unavailable")
        now = app.clock.now().astimezone(UTC).isoformat()
        spec = json.dumps(asdict(self.policy), sort_keys=True)
        controls = app.public_grants._controls()
        with app.journal.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS public_standing_authority(id TEXT PRIMARY KEY, spec TEXT NOT NULL, controls TEXT NOT NULL, last_seen TEXT NOT NULL, revoked INTEGER NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO public_standing_authority VALUES(?,?,?,?,0)",
                (self.policy.policy_id, spec, controls, now),
            )
            row = db.execute(
                "SELECT spec,controls,last_seen,revoked FROM public_standing_authority WHERE id=?",
                (self.policy.policy_id,),
            ).fetchone()
            if tuple(row[:2]) != (spec, controls) or row[3] or now < row[2]:
                raise PolicyDeniedError(
                    "standing public authority changed, revoked or clock regressed"
                )
            db.execute(
                "UPDATE public_standing_authority SET last_seen=? WHERE id=?",
                (now, self.policy.policy_id),
            )
        self.encoding, self.controls = spec, controls

    def guard(self):
        with closing(
            sqlite3.connect(self.app.journal.path.as_uri() + "?mode=ro", uri=True)
        ) as db:
            row = db.execute(
                "SELECT spec,controls,last_seen,revoked FROM public_standing_authority WHERE id=?",
                (self.policy.policy_id,),
            ).fetchone()
        if (
            row is None
            or row[0] != self.encoding
            or row[1] != self.app.public_grants._controls()
            or row[3]
            or self.app.clock.now().astimezone(UTC).isoformat() < row[2]
        ):
            raise PolicyDeniedError("standing public authority denied")

    def revoke(self):
        self.app.operator.authenticate()
        with self.app.journal.transaction() as db:
            if (
                db.execute(
                    "UPDATE public_standing_authority SET revoked=1 WHERE id=?",
                    (self.policy.policy_id,),
                ).rowcount
                != 1
            ):
                raise ValueError("standing public authority missing")

    def run(self, *, wait=time.sleep, should_stop=lambda: False):
        app = self.app
        try:
            with app.journal.handoff_lock("standing-public", blocking=False):
                self._check()
                app.standing_public = True
                app.journal.standing_admission = True
                app.standing_guard = self.guard
                try:
                    while not should_stop():
                        self._check()
                        window, grant = self.policy.lease(app.clock.now())
                        with app.journal.transaction() as db:
                            app.public_grants._schema(db)
                            row = db.execute(
                                "SELECT spec,revoked FROM public_grants WHERE id=?",
                                (grant.grant_id,),
                            ).fetchone()
                            prefix = self.policy.policy_id + "-"
                            revoked = db.execute(
                                "SELECT 1 FROM public_grants WHERE substr(id,1,?)=? AND revoked=1",
                                (len(prefix), prefix),
                            ).fetchone()
                        if revoked or (row is not None and row[0] != grant.encoded()):
                            raise PolicyDeniedError(
                                "standing public lease revoked or changed"
                            )
                        if row is None:
                            app.public_grants.issue(grant, window)
                        result = PublicOperatingWindow(app, window).run(wait=wait)
                        if result["state"] not in {"expired", "exhausted"}:
                            return result
                        while app.clock.now() < window.expires_at and not should_stop():
                            self._check()
                            wait(
                                min(
                                    1,
                                    (
                                        window.expires_at - app.clock.now()
                                    ).total_seconds(),
                                )
                            )
                    return {"state": "interrupted"}
                finally:
                    app.standing_public = False
                    app.journal.standing_admission = False
                    app.standing_guard = None
        except BlockingIOError:
            return {"state": "busy"}
