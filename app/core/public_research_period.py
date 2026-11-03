"""UTC monthly research allowances, preserving legacy consumption and replay IDs."""

from collections import Counter
from datetime import UTC
import json


PERIOD_SCHEMA = {
    "public_research_period_origins": "CREATE TABLE public_research_period_origins (account TEXT PRIMARY KEY, first_month TEXT NOT NULL, legacy TEXT NOT NULL, last_seen TEXT NOT NULL)",
    "public_research_period_work": "CREATE TABLE public_research_period_work (operation TEXT PRIMARY KEY, account TEXT NOT NULL, month TEXT NOT NULL, topic TEXT NOT NULL)",
}


def initialize_periods(db):
    for sql in PERIOD_SCHEMA.values():
        db.execute(sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))


def period_counts(db, account, now, legacy):
    now = now.astimezone(UTC).isoformat()
    db.execute(
        "INSERT OR IGNORE INTO public_research_period_origins VALUES(?,?,?,?)",
        (account, now[:7], json.dumps(legacy), now),
    )
    first, original, previous = db.execute(
        "SELECT first_month,legacy,last_seen FROM public_research_period_origins WHERE account=?",
        (account,),
    ).fetchone()
    if now < previous:
        raise ValueError("research allowance clock regressed")
    db.execute(
        "UPDATE public_research_period_origins SET last_seen=? WHERE account=?",
        (now, account),
    )
    counts = Counter(json.loads(original) if first == now[:7] else {})
    counts.update(
        dict(
            db.execute(
                "SELECT topic,COUNT(*) FROM public_research_period_work WHERE account=? AND month=? GROUP BY topic",
                (account, now[:7]),
            )
        )
    )
    return dict(counts)


def record_period(db, operation, account, topic, now):
    db.execute(
        "INSERT INTO public_research_period_work VALUES(?,?,?,?)",
        (operation, account, now.astimezone(UTC).strftime("%Y-%m"), topic),
    )
