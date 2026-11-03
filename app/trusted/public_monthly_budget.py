"""Conservative public-only admission, independent of delayed provider billing.

Two worker-owned wallets partition $20: Sol $15 and research $5. No shared
write credential, private-dialogue balance, or model supplied price is involved.
Unknown outcomes retain the whole reservation. Calendar months are UTC.
"""

from contextlib import closing
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
import stat
from uuid import uuid4

from app.core.public_rejection import PublicOutputRejected
from app.core.public_research_authority import validate_trusted_directories


# USD per million tokens, deliberately above standard and cache-write prices
# including a 10% regional surcharge (official pricing checked 2026-11-02).
# Research reserves the whole model context, including retrieved search tokens.
POLICIES = {
    "sol": ("gpt-6.1-sol", 15_000_000, 1_000_000, 133_120, 600, 1500, 0),
    "research": ("gpt-6-luna", 5_000_000, 700_000, 1_050_000, 60, 90, 11_000),
}
VERSION = "public-usd-conservative-20261102-v1"


class MonthlyBudget:
    """An existing, durable, pinned wallet; missing state never replenishes it."""

    @staticmethod
    def initialize(path, *, lane, project_id):
        path = Path(path)
        if (
            lane not in POLICIES
            or type(project_id) is not str
            or not project_id.startswith("proj_")
        ):
            raise ValueError("invalid public budget policy")
        validate_trusted_directories(path)
        policy = (VERSION, lane, project_id, *POLICIES[lane])
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE policy(value TEXT NOT NULL, last_seen TEXT NOT NULL)"
            )
            db.execute("INSERT INTO policy VALUES (?, '')", (json.dumps(policy),))
            db.execute(
                "CREATE TABLE public_budget_attempts("
                "operation TEXT PRIMARY KEY, month TEXT NOT NULL, "
                "charged INTEGER NOT NULL CHECK(charged>=0), "
                "settled INTEGER NOT NULL CHECK(settled IN (0,1)), settled_month TEXT)"
            )

    def __init__(self, path, *, lane, project_id, clock=lambda: datetime.now(UTC)):
        if (
            lane not in POLICIES
            or type(project_id) is not str
            or not project_id.startswith("proj_")
        ):
            raise ValueError("invalid public budget policy")
        self.path, self.lane, self.clock = Path(path), lane, clock
        self.policy = POLICIES[lane]
        if not self.path.is_absolute() or self.path.is_symlink():
            raise ValueError("invalid public budget path")
        validate_trusted_directories(self.path)
        info, parent = self.path.lstat(), self.path.parent.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise ValueError("unsafe public budget state")
        self.identity = info.st_dev, info.st_ino
        self.encoding = json.dumps((VERSION, lane, project_id, *self.policy))
        with self._open() as db:
            if db.execute("SELECT value FROM policy").fetchall() != [(self.encoding,)]:
                raise ValueError("public budget policy changed")

    def _open(self):
        info = self.path.lstat()
        if (info.st_dev, info.st_ino) != self.identity:
            raise ValueError("public budget state replaced")
        return closing(
            sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
        )

    def _month(self, db):
        observed = self.clock()
        if observed.tzinfo is None:
            raise ValueError("public budget clock is naive")
        now = observed.astimezone(UTC).isoformat()
        row = db.execute("SELECT value,last_seen FROM policy").fetchone()
        if row[0] != self.encoding or now < row[1]:
            raise ValueError("public budget policy or clock regressed")
        db.execute("UPDATE policy SET last_seen=?", (now,))
        return now[:7]

    def reserve(self, operation):
        if type(operation) is not str or not 1 <= len(operation) <= 128:
            raise ValueError("invalid budget operation")
        with self._open() as db, db:
            db.execute("BEGIN IMMEDIATE")
            month = self._month(db)
            if db.execute(
                "SELECT 1 FROM public_budget_attempts WHERE operation=?", (operation,)
            ).fetchone():
                return False
            used = db.execute(
                "SELECT COALESCE(SUM(charged),0) FROM public_budget_attempts WHERE settled=0 OR settled_month=?",
                (month,),
            ).fetchone()
            used = used[0]
            count = db.execute(
                "SELECT COUNT(*) FROM public_budget_attempts WHERE month=?", (month,)
            ).fetchone()[0]
            if used + self.policy[2] > self.policy[1] or (
                self.lane == "research" and count >= 100
            ):
                return False
            db.execute(
                "INSERT INTO public_budget_attempts VALUES (?,?,?,0,NULL)",
                (operation, month, self.policy[2]),
            )
        return True

    def settle(self, operation, usage):
        if type(usage) is not dict or any(
            type(usage.get(key)) is not int or usage[key] < 0
            for key in ("input_tokens", "output_tokens", "total_tokens")
        ):
            return
        inputs, outputs = usage["input_tokens"], usage["output_tokens"]
        if (
            inputs > self.policy[3]
            or outputs > 2048
            or usage["total_tokens"] != inputs + outputs
        ):
            return
        charge = (
            inputs * self.policy[4] + outputs * self.policy[5] + 99
        ) // 100 + self.policy[6]
        with self._open() as db, db:
            db.execute("BEGIN IMMEDIATE")
            month = self._month(db)
            db.execute(
                "UPDATE public_budget_attempts SET charged=?,settled=1,settled_month=? WHERE operation=? AND settled=0",
                (charge, month, operation),
            )

    def snapshot(self):
        with self._open() as db, db:
            db.execute("BEGIN IMMEDIATE")
            month = self._month(db)
            charged, count = db.execute(
                "SELECT COALESCE(SUM(charged),0),COUNT(*) FROM public_budget_attempts WHERE settled=0 OR settled_month=?",
                (month,),
            ).fetchone()
            return dict(
                month=month,
                charged_microusd=charged,
                attempts=count,
                limit_microusd=self.policy[1],
            )


class BudgetedResponses:
    """Last fence before the SDK. Native authorization and Stops still precede it."""

    def __init__(self, delegate, budget):
        self.delegate, self.budget = delegate, budget

    def create(self, **kwargs):
        research = self.budget.lane == "research"
        kwargs["service_tier"] = kwargs.get("service_tier", "default")
        tools = kwargs.get("tools")
        valid_tools = (
            type(tools) is list
            and len(tools) == 1
            and type(tools[0]) is dict
            and tools[0].get("type") == "web_search"
            and kwargs.get("max_tool_calls") == 1
            if research
            else tools == []
        )
        allowed = {
            "model",
            "input",
            "tools",
            "text",
            "store",
            "max_output_tokens",
            "timeout",
            "reasoning",
            "service_tier",
        }
        if research:
            allowed |= {
                "instructions",
                "tool_choice",
                "include",
                "background",
                "stream",
                "max_tool_calls",
                "truncation",
                "parallel_tool_calls",
            }
        text_input = kwargs.get("input")
        if type(text_input) is list:
            text_input = all(
                type(item) is dict
                and set(item) == {"role", "content"}
                and item["role"] in {"system", "user"}
                and type(item["content"]) is str
                for item in text_input
            )
        else:
            text_input = type(text_input) is str
        if (
            kwargs.get("model") != self.budget.policy[0]
            or kwargs.get("service_tier", "default") != "default"
            or type(kwargs.get("max_output_tokens")) is not int
            or not 1 <= kwargs["max_output_tokens"] <= 2048
            or kwargs.get("store") is not False
            or not valid_tools
            or not text_input
            or not set(kwargs) <= allowed
            or kwargs.get("parallel_tool_calls", False) is not False
            or len(json.dumps(kwargs, ensure_ascii=False).encode()) > 131_072
        ):
            raise PublicOutputRejected(
                "public_budget_contract_invalid", "public budget contract rejected"
            )
        operation = uuid4().hex
        if not self.budget.reserve(operation):
            raise PublicOutputRejected(
                "public_monthly_budget_exhausted", "public monthly budget exhausted"
            )
        raw = self.delegate.create(**kwargs)
        usage = raw.get("usage") if type(raw) is dict else getattr(raw, "usage", None)
        if usage is not None and type(usage) is not dict:
            usage = {
                key: getattr(usage, key, None)
                for key in ("input_tokens", "output_tokens", "total_tokens")
            }
        self.budget.settle(operation, usage)
        return raw
