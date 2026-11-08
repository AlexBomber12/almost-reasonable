"""Read a pinned conservative wallet without writing or owning provider access."""

from contextlib import closing
from datetime import UTC, datetime
import json
import sqlite3
from time import monotonic

from app.trusted.private_pilot import protected_state_identity, PilotAuthorityError
from app.trusted.openai_credential_files import TrustedTransportConfigurationError
from app.trusted.public_monthly_budget import POLICIES, VERSION


def observe_budget(path, *, clock=lambda: datetime.now(UTC)):
    try:
        if path.suffix == ".json":
            from app.trusted.shared_budget_service import observe_remote_budget

            return observe_remote_budget(path)
        identity = protected_state_identity(path)
        observed = clock()
        if observed.tzinfo is None:
            raise ValueError("wallet clock is naive")
        now = observed.astimezone(UTC)
        month = now.strftime("%Y-%m")
        deadline = monotonic() + 0.5
        with closing(
            sqlite3.connect(
                path.as_uri() + "?mode=ro", uri=True, timeout=0.2, isolation_level=None
            )
        ) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=200")
            db.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
            db.execute("BEGIN")
            rows = db.execute("SELECT value,last_seen FROM policy").fetchall()
            if len(rows) != 1:
                raise ValueError("wallet policy unavailable")
            raw, last_seen = rows[0]
            policy = json.loads(raw)
            if (
                type(policy) is not list
                or len(policy) != 10
                or policy[1] not in POLICIES
                or type(policy[2]) is not str
                or not policy[2].startswith("proj_")
                or policy != [VERSION, policy[1], policy[2], *POLICIES[policy[1]]]
                or raw
                != json.dumps([VERSION, policy[1], policy[2], *POLICIES[policy[1]]])
                or type(last_seen) is not str
                or (last_seen and now.isoformat() < last_seen)
            ):
                raise ValueError("wallet policy or clock unavailable")
            settled, unresolved, holds = db.execute(
                "SELECT COALESCE(SUM(CASE WHEN settled=1 AND settled_month=? THEN charged ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN settled=0 THEN charged ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN settled=0 THEN 1 ELSE 0 END),0) FROM public_budget_attempts",
                (month,),
            ).fetchone()
            attempts = db.execute(
                "SELECT COUNT(*) FROM public_budget_attempts WHERE month=?", (month,)
            ).fetchone()[0]
            db.execute("ROLLBACK")
        if identity != protected_state_identity(path):
            raise ValueError("wallet replaced")
        lane = policy[1]
        limit, reservation = POLICIES[lane][1:3]
        used = settled + unresolved
        return {
            "available": True,
            "scope": "conservative_monthly_wallet",
            "billing_authority": False,
            "collected_at": now.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
            "month": month,
            "lane": lane,
            "limit_microusd": limit,
            "settled_microusd": settled,
            "unresolved_microusd": unresolved,
            "unresolved_attempts": holds,
            "remaining_microusd": max(0, limit - used),
            "month_attempts": attempts,
            "search_limit": 100 if lane == "research" else None,
            "next_reservation_microusd": reservation,
            "admission_available": used + reservation <= limit
            and (lane != "research" or attempts < 100),
        }
    except (
        OSError,
        ValueError,
        TypeError,
        sqlite3.Error,
        OverflowError,
        PilotAuthorityError,
        TrustedTransportConfigurationError,
        KeyError,
    ):
        return {"available": False}
