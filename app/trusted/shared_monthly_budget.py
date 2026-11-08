"""One credential-free, worker-owned conservative $40 project ledger.

Migration imports all three quiescent lane wallets without repricing history or
discarding unknown outcomes. Reservations and search admission are serialized
in the same SQLite transaction. Only the broker owns this database.
"""

from contextlib import closing
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sqlite3

from app.trusted.private_pilot import protected_state_identity
from app.trusted.public_monthly_budget import MonthlyBudget, POLICIES


VERSION = "shared-project-usd-20261108-v1"
LIMIT = 40_000_000
# Pinned upper prices include cache writes and regional processing. Sol's
# bounded input is below 272K; research reserves its full long-context window.
# Official model pricing checked 2026-11-08. Unknowns keep their full reserve.
SHARED_POLICIES = {
    "sol": ("gpt-6.1-sol", LIMIT, 400_000, 133_120, 275, 1100, 0, 11),
    "private_sol": ("gpt-6.1-sol", LIMIT, 400_000, 133_120, 275, 1100, 0, 11),
    "research": ("gpt-6-luna", LIMIT, 320_000, 1_050_000, 28, 83, 11_000, 3),
}


def _encoding(project_id):
    if type(project_id) is not str or not project_id.startswith("proj_"):
        raise ValueError("invalid shared budget project")
    return json.dumps([VERSION, project_id, LIMIT, SHARED_POLICIES], sort_keys=True)


class SharedWallet:
    """Durable shared admission; never initialize implicitly on a live path."""

    @staticmethod
    def initialize(path, *, project_id, sources):
        encoding = _encoding(project_id)
        if type(sources) is not dict or set(sources) != set(POLICIES):
            raise ValueError("all three source wallets are required")
        rows, watermark = [], ""
        for lane, source in sources.items():
            old = MonthlyBudget(source, lane=lane, project_id=project_id)
            with old._open() as db:
                watermark = max(
                    watermark, db.execute("SELECT last_seen FROM policy").fetchone()[0]
                )
                for operation, month, charged, settled, settled_month in db.execute(
                    "SELECT operation,month,charged,settled,settled_month FROM public_budget_attempts"
                ):
                    if (
                        type(operation) is not str
                        or not 1 <= len(operation) <= 128
                        or type(charged) is not int
                        or not 0 <= charged <= POLICIES[lane][2]
                        or settled not in (0, 1)
                        or re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month) is None
                        or (
                            settled == 1
                            and (
                                type(settled_month) is not str
                                or re.fullmatch(
                                    r"[0-9]{4}-(0[1-9]|1[0-2])", settled_month
                                )
                                is None
                            )
                        )
                        or (settled == 0 and settled_month is not None)
                    ):
                        raise ValueError("invalid source budget record")
                    rows.append(
                        (lane, operation, month, charged, settled, settled_month)
                    )
        path = Path(path)
        # The existing initializer supplies the same exclusive, protected-file
        # creation used by legacy wallets. Replace its schema in one transaction.
        MonthlyBudget.initialize(path, lane="sol", project_id=project_id)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("DROP TABLE public_budget_attempts")
            db.execute("UPDATE policy SET value=?,last_seen=?", (encoding, watermark))
            db.execute(
                "CREATE TABLE shared_attempts(lane TEXT NOT NULL, operation TEXT NOT NULL,"
                "month TEXT NOT NULL, charged INTEGER NOT NULL CHECK(charged>=0),"
                "settled INTEGER NOT NULL CHECK(settled IN (0,1)), settled_month TEXT,"
                "PRIMARY KEY(lane,operation))"
            )
            db.executemany("INSERT INTO shared_attempts VALUES (?,?,?,?,?,?)", rows)

    def __init__(self, path, *, project_id, clock=lambda: datetime.now(UTC)):
        self.path, self.clock = Path(path), clock
        self.encoding = _encoding(project_id)
        self.identity = protected_state_identity(self.path)
        with self._open() as db:
            if db.execute("SELECT value FROM policy").fetchall() != [(self.encoding,)]:
                raise ValueError("shared budget policy changed")

    def _open(self):
        if protected_state_identity(self.path) != self.identity:
            raise ValueError("shared budget state replaced")
        return closing(
            sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
        )

    def _month(self, db, *, write=True):
        observed = self.clock()
        if observed.tzinfo is None:
            raise ValueError("shared budget clock is naive")
        now = observed.astimezone(UTC).isoformat()
        rows = db.execute("SELECT value,last_seen FROM policy").fetchall()
        if len(rows) != 1 or rows[0][0] != self.encoding:
            raise ValueError("shared budget policy changed")
        last_seen = rows[0][1]
        if now < last_seen:
            raise ValueError("shared budget clock regressed")
        if write:
            db.execute("UPDATE policy SET last_seen=?", (now,))
        return now[:7]

    @staticmethod
    def _policy(lane):
        if type(lane) is not str or lane not in SHARED_POLICIES:
            raise ValueError("invalid shared budget lane")
        return SHARED_POLICIES[lane]

    def _status(self, db, lane, month):
        policy = self._policy(lane)
        settled, unresolved, holds = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN settled=1 AND settled_month=? THEN charged ELSE 0 END),0),"
            "COALESCE(SUM(CASE WHEN settled=0 THEN charged ELSE 0 END),0),"
            "COALESCE(SUM(CASE WHEN settled=0 THEN 1 ELSE 0 END),0) FROM shared_attempts",
            (month,),
        ).fetchone()
        attempts = db.execute(
            "SELECT COUNT(*) FROM shared_attempts WHERE lane=? AND month=?",
            (lane, month),
        ).fetchone()[0]
        return dict(
            month=month,
            charged_microusd=settled + unresolved,
            attempts=attempts,
            limit_microusd=LIMIT,
            settled_microusd=settled,
            unresolved_microusd=unresolved,
            unresolved_attempts=holds,
            month_attempts=attempts,
            search_limit=100 if lane == "research" else None,
            next_reservation_microusd=policy[2],
            remaining_microusd=max(0, LIMIT - settled - unresolved),
            admission_available=settled + unresolved + policy[2] <= LIMIT
            and (lane != "research" or attempts < 100),
        )

    def reserve(self, lane, operation):
        policy = self._policy(lane)
        if type(operation) is not str or not 1 <= len(operation) <= 128:
            raise ValueError("invalid shared budget operation")
        with self._open() as db, db:
            db.execute("BEGIN IMMEDIATE")
            month = self._month(db)
            if (
                not self._status(db, lane, month)["admission_available"]
                or db.execute(
                    "SELECT 1 FROM shared_attempts WHERE lane=? AND operation=?",
                    (lane, operation),
                ).fetchone()
            ):
                return False
            db.execute(
                "INSERT INTO shared_attempts VALUES (?,?,?,?,0,NULL)",
                (lane, operation, month, policy[2]),
            )
        return True

    def settle(self, lane, operation, usage):
        policy = self._policy(lane)
        if type(usage) is not dict or any(
            type(usage.get(k)) is not int or usage[k] < 0
            for k in ("input_tokens", "output_tokens", "total_tokens")
        ):
            return
        inputs, outputs = usage["input_tokens"], usage["output_tokens"]
        cached = usage.get("cached_input_tokens", 0)
        if (
            inputs > policy[3]
            or outputs > 2048
            or usage["total_tokens"] != inputs + outputs
            or type(cached) is not int
            or not 0 <= cached <= inputs
        ):
            return
        charge = (
            (inputs - cached) * policy[4]
            + cached * policy[7]
            + outputs * policy[5]
            + 99
        ) // 100 + policy[6]
        with self._open() as db, db:
            db.execute("BEGIN IMMEDIATE")
            month = self._month(db)
            db.execute(
                "UPDATE shared_attempts SET charged=?,settled=1,settled_month=? WHERE lane=? AND operation=? AND settled=0",
                (charge, month, lane, operation),
            )

    def snapshot(self, lane):
        self._policy(lane)
        with self._open() as db, db:
            db.execute("BEGIN IMMEDIATE")
            return self._status(db, lane, self._month(db))

    def observe(self, lane):
        """Telemetry neither settles reservations nor advances the clock fence."""
        self._policy(lane)
        with self._open() as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            return self._status(db, lane, self._month(db, write=False))
