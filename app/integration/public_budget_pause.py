"""Wait on an attested local monthly denial; never clear financial holds."""

from datetime import UTC, datetime
import json

from app.core.read_only_sqlite import connect_read_only

CODE = "public_monthly_budget_exhausted"
CONFIG = "threads-public-worker-budget-v2"
LEGACY_CONFIG = "threads-public-worker-v1"


def _month(value):
    if value.tzinfo is None:
        raise ValueError("public budget observation clock must be aware")
    return value.astimezone(UTC).strftime("%Y-%m")


def paused(app):
    """No SDK contact is required to remember this month's known refusal."""
    with connect_read_only(app.provider._event_store.database_path) as db:
        row = db.execute(
            "SELECT started_at,outcome,provider_error_code FROM provider_usage_attempts "
            "WHERE provider_config_version=? ORDER BY rowid DESC LIMIT 1",
            (CONFIG,),
        ).fetchone()
    if row is None or row[1:] != ("provider_error", CODE):
        return False
    observed, current = _month(datetime.fromisoformat(row[0])), _month(app.clock.now())
    if observed > current:
        raise ValueError("public budget observation clock regressed")
    return observed == current


def wakeup(app, identity, now):
    from app.behavior.durable_scheduler import SchedulerRun
    from app.behavior.scheduler import SchedulerOutcome, WakeupEvent

    if paused(app):
        return SchedulerRun(SchedulerOutcome.BUDGET_PAUSED, None, 0, 0, None)
    return app.scheduler.run((WakeupEvent(identity, now, "bounded operating window"),))


def retryable(app, operation, phase, origin):
    """Only an older, pre-SDK local denial with no materialized effects qualifies."""
    if (
        phase != "started"
        or origin.get("runtime_version") != "threads-public-runtime-v1"
    ):
        return False
    with connect_read_only(app.provider._event_store.database_path) as db:
        row = db.execute(
            "SELECT started_at,provider_config_version FROM provider_usage_attempts WHERE attempt_id=? "
            "AND provider_config_version IN (?,?) AND outcome='provider_error' AND provider_error_code=? "
            "AND provider_response_id IS NULL AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL",
            (operation, CONFIG, LEGACY_CONFIG, CODE),
        ).fetchone()
    if row is None:
        return False
    observed, current = _month(datetime.fromisoformat(row[0])), _month(app.clock.now())
    if observed > current or (observed == current and row[1] == CONFIG):
        return False
    with connect_read_only(app.service.store.database) as db:
        if db.execute(
            "SELECT 1 FROM threads_outbox WHERE operation_id=?", (operation,)
        ).fetchone():
            return False
    with connect_read_only(app.journal.path) as db:
        if db.execute(
            "SELECT 1 FROM public_research_ops WHERE operation=?", (operation,)
        ).fetchone():
            return False
        if (
            db.execute(
                "SELECT 1 FROM agent_intellectual_operations WHERE audience='public' AND owner=? AND operation=? "
                "AND (status!='reserved' OR proposal IS NOT NULL OR metadata IS NOT NULL)",
                (app.account, operation),
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM agent_intellectual_revisions WHERE audience='public' AND owner=? AND operation=?",
                (app.account, operation),
            ).fetchone()
        ):
            return False
    return True


def claims(app, kind, resumed):
    with connect_read_only(app.journal.path) as db:
        rows = db.execute(
            "SELECT operation,phase,payload FROM intents ORDER BY rowid"
        ).fetchall()
    return [
        (operation, phase, payload)
        for operation, phase, raw in rows
        if (payload := json.loads(raw))["kind"] == kind
        and payload.get("resumed", False) == resumed
    ]
