"""Payload-free observations, never a verdict about current beliefs or health."""

from app.core.intellectual_state import digest


def intellectual_lifecycle(db, account):
    counts = dict(
        db.execute(
            "SELECT status,COUNT(*) FROM agent_intellectual_operations "
            "WHERE audience='public' AND owner=? GROUP BY status",
            (account,),
        )
    )
    revisions, redacted = db.execute(
        "SELECT COUNT(*),COUNT(*)-COUNT(payload) FROM agent_intellectual_revisions "
        "WHERE audience='public' AND owner=?",
        (account,),
    ).fetchone()
    return {
        "scope": "recorded_public_lifecycle",
        "operations": {
            status: counts.get(status, 0)
            for status in (
                "reserved",
                "received",
                "accepted",
                "rejected",
                "unavailable",
                "no_change",
            )
        },
        "recorded_revisions": revisions,
        "redacted_revisions": redacted,
        "recent": [
            {
                "reference": digest(operation)[:16],
                "status": status,
                "proposal_recorded": bool(has_proposal),
            }
            for operation, status, has_proposal in db.execute(
                "SELECT operation,status,proposal IS NOT NULL AND proposal!='null' "
                "FROM agent_intellectual_operations WHERE audience='public' AND owner=? "
                "ORDER BY rowid DESC LIMIT 8",
                (account,),
            )
        ],
    }


def discussion_observation(db, account):
    streams = {}
    for stream in ("reply", "mention"):
        attempts = dict(
            db.execute(
                "SELECT status,COUNT(*) FROM threads_poll_attempts "
                "WHERE account_ref=? AND stream=? GROUP BY status",
                (account, stream),
            )
        )
        latest = db.execute(
            "SELECT status,started_at,completed_at FROM threads_poll_attempts "
            "WHERE account_ref=? AND stream=? ORDER BY rowid DESC LIMIT 1",
            (account, stream),
        ).fetchone()
        received, pending = db.execute(
            "SELECT COUNT(*),COUNT(*)-COUNT(processed_at) FROM threads_inbound_events "
            "WHERE account_ref=? AND kind=?",
            (account, stream),
        ).fetchone()
        streams[stream] = {
            "attempts": attempts,
            "last_attempt": None
            if latest is None
            else dict(
                zip(("status", "started_at", "completed_at"), latest, strict=True)
            ),
            "received": received,
            "pending": pending,
        }
    return {"scope": "recorded_public_polling", "streams": streams}
