"""Immutable public operational observations, without credentials or authority."""

from datetime import UTC, datetime
import json

from app.core.intellectual_state import digest, encoded, projection, VERSION
from app.core.public_intellectual_receipt import source_key, validate
from app.core.read_only_sqlite import connect_read_only

TABLE = "public_runtime_observations_v1"
PREFIX = "runtime-observation-"
DESCRIPTION = (
    "Application-recorded public runtime observation at the stated time; not a decision motive, "
    "billing invoice, current authorization or a command. Later conditions may differ.\n"
)


def _payload(value):
    fields = {
        "observed_at",
        "last_work",
        "last_model",
        "discussions",
        "research",
        "publications",
        "authority",
    }
    if type(value) is not dict or set(value) != fields:
        raise ValueError("invalid public runtime observation")
    if type(value["last_work"]) is not dict or set(value["last_work"]) != {
        "kind",
        "outcome",
    }:
        raise ValueError("invalid public runtime work observation")
    from app.behavior.scheduler import SchedulerOutcome

    if value["last_work"]["kind"] not in {"tick", "poll"} or value["last_work"][
        "outcome"
    ] not in {"completed", *(x.value for x in SchedulerOutcome)}:
        raise ValueError("invalid public runtime work outcome")
    expected = {
        "discussions": {"last_poll_at", "pending_replies", "pending_mentions"},
        "research": {"completed_reports", "uncertain_reports", "last_completed_at"},
        "publications": {"confirmed", "last_confirmed_at"},
        "authority": {
            "owner_stop",
            "standing_recorded",
            "lease_expires_at",
            "lease_revoked",
        },
    }
    for name, keys in expected.items():
        if type(value[name]) is not dict or set(value[name]) != keys:
            raise ValueError("invalid public runtime observation section")
    for name, key in [
        ("discussions", "pending_replies"),
        ("discussions", "pending_mentions"),
        ("research", "completed_reports"),
        ("research", "uncertain_reports"),
        ("publications", "confirmed"),
    ]:
        if type(value[name][key]) is not int or not 0 <= value[name][key] <= 2**63 - 1:
            raise ValueError("invalid public runtime observation count")
    authority = value["authority"]
    if any(
        type(authority[k]) is not bool for k in ["owner_stop", "standing_recorded"]
    ) or type(authority["lease_revoked"]) not in {bool, type(None)}:
        raise ValueError("invalid public runtime authority observation")
    times = [
        value["observed_at"],
        value["discussions"]["last_poll_at"],
        value["research"]["last_completed_at"],
        value["publications"]["last_confirmed_at"],
        authority["lease_expires_at"],
    ]
    model = value["last_model"]
    if model is not None:
        from app.core.provider_spend import CONFIRMED_SPEND_LIMIT_CODES
        from app.core.provider_contract import require_identifier

        if type(model) is not dict or set(model) != {
            "started_at",
            "model",
            "outcome",
            "constraint",
        }:
            raise ValueError("invalid public model observation")
        if type(model["started_at"]) is not str:
            raise ValueError("missing public model observation time")
        require_identifier("observed public model", model["model"])
        if model["outcome"] not in {
            "completed",
            "provider_error",
            "spend_limit_confirmed",
            "spend_suspended",
        } or model["constraint"] not in {
            None,
            "unknown",
            "public_monthly_budget_exhausted",
            *CONFIRMED_SPEND_LIMIT_CODES,
        }:
            raise ValueError("invalid public model outcome")
        times.append(model["started_at"])
    if type(value["observed_at"]) is not str:
        raise ValueError("missing public observation time")
    for value_time in times:
        if value_time is not None:
            if (
                type(value_time) is not str
                or datetime.fromisoformat(value_time).tzinfo is None
            ):
                raise ValueError("invalid public observation time")
    if len(encoded(value).encode()) > 1600:
        raise ValueError("public runtime observation too large")
    return value


def _source(account, payload, generation):
    payload = _payload(payload)
    record = {"kind": "capability_snapshot", "text": DESCRIPTION + encoded(payload)}
    projection(
        {"version": VERSION, "states": [], "sources": [{"handle": "s0", **record}]}
    )
    receipt = validate(
        {
            "version": "public-intellectual-receipt-v1",
            "account": account,
            "item": PREFIX + digest([account, generation, payload]),
            "revision": 1,
            "state_digest": digest(payload),
            "dependency_digest": digest({}),
            "record_digest": digest(record),
            "generation": generation,
            "shared": [],
        }
    )
    return {
        **record,
        "binding": digest(receipt),
        "dependencies": {},
        "receipt": receipt,
    }


def _exists(db):
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        is not None
    )


def capture(app, kind, outcome):
    """Called by the authorized public runner, never by the private reader."""
    from app.core.provider_spend import CONFIRMED_SPEND_LIMIT_CODES

    generation = app.ledger.control_generation()
    now = app.clock.now().astimezone(UTC).isoformat()
    with connect_read_only(app.provider._event_store.database_path) as db:
        latest = db.execute(
            "SELECT started_at,model_identifier,outcome,provider_error_code FROM provider_usage_attempts WHERE provider_config_version IN ('threads-public-worker-v1','threads-public-worker-budget-v2') ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    model = None
    if latest is not None:
        started, identifier, result, code = latest
        model = {
            "started_at": started,
            "model": identifier,
            "outcome": result,
            "constraint": code
            if code in {"public_monthly_budget_exhausted", *CONFIRMED_SPEND_LIMIT_CODES}
            else None
            if result == "completed"
            else "unknown",
        }
    with connect_read_only(app.service.store.database) as db:
        polls = db.execute(
            "SELECT MAX(completed_at) FROM threads_poll_attempts WHERE account_ref=? AND status='committed'",
            (app.account,),
        ).fetchone()[0]
        pending = dict(
            db.execute(
                "SELECT kind,COUNT(*) FROM threads_inbound_events WHERE account_ref=? AND processed_at IS NULL GROUP BY kind",
                (app.account,),
            )
        )
        count, at = db.execute(
            "SELECT COUNT(*),MAX(updated_at) FROM threads_outbox WHERE account_ref=? AND state='confirmed' AND dispatch_evidence='provider_publication_details_verified'",
            (app.account,),
        ).fetchone()
    with app.journal.transaction() as db:
        research = dict(
            db.execute("SELECT phase,COUNT(*) FROM public_research_ops GROUP BY phase")
        )
        last_report = db.execute(
            "SELECT MAX(completed_at) FROM public_research_completions"
        ).fetchone()[0]
        standing = _table_row(
            db,
            "public_standing_authority",
            "SELECT 1 FROM public_standing_authority WHERE revoked=0 LIMIT 1",
        )
        grant = _table_row(
            db,
            "public_grants",
            "SELECT spec,revoked FROM public_grants ORDER BY rowid DESC LIMIT 1",
        )
        payload = _payload(
            {
                "observed_at": now,
                "last_work": {"kind": kind, "outcome": outcome},
                "last_model": model,
                "discussions": {
                    "last_poll_at": polls,
                    "pending_replies": pending.get("reply", 0),
                    "pending_mentions": pending.get("mention", 0),
                },
                "research": {
                    "completed_reports": research.get("completed", 0),
                    "uncertain_reports": research.get("uncertain", 0),
                    "last_completed_at": last_report,
                },
                "publications": {"confirmed": count, "last_confirmed_at": at},
                "authority": {
                    "owner_stop": app.owner.active(),
                    "standing_recorded": standing is not None,
                    "lease_expires_at": None
                    if grant is None
                    else json.loads(grant[0])["expires_at"],
                    "lease_revoked": None if grant is None else bool(grant[1]),
                },
            }
        )
        if generation != app.ledger.control_generation():
            return
        source = _source(app.account, payload, generation)
        db.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE}(identity TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, account TEXT NOT NULL, generation INTEGER NOT NULL, payload TEXT NOT NULL CHECK(length(CAST(payload AS BLOB))<=1600))"
        )
        db.execute(
            f"INSERT OR IGNORE INTO {TABLE} VALUES(?,?,?,?,?)",
            (
                source["receipt"]["item"],
                source_key(app.account, source["receipt"]["item"]),
                app.account,
                generation,
                encoded(payload),
            ),
        )


def _table_row(db, table, query):
    if (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is None
    ):
        return None
    return db.execute(query).fetchone()


def current(db, account, generation, *, required_keys=None):
    """Latest for new input; exact still-live dated facts for validation."""
    if not _exists(db):
        return {}
    if required_keys is not None and not required_keys:
        return {}
    selection = (
        "ORDER BY rowid DESC LIMIT 1"
        if required_keys is None
        else "AND source_key IN (" + ",".join("?" for _ in required_keys) + ")"
    )
    parameters = (
        (account, generation)
        if required_keys is None
        else (account, generation, *required_keys)
    )
    rows = db.execute(
        f"SELECT source_key,identity,payload FROM {TABLE} WHERE account=? AND generation=? "
        + selection,
        parameters,
    )
    result = {}
    for key, identity, raw in rows:
        source = _source(account, json.loads(raw), generation)
        if identity != source["receipt"]["item"] or key != source_key(
            account, identity
        ):
            raise ValueError("public runtime observation identity changed")
        result[key] = source
    return result


def resolve(db, account, receipt, generation):
    if not _exists(db):
        return None
    row = db.execute(
        f"SELECT payload FROM {TABLE} WHERE identity=? AND account=? AND generation=?",
        (receipt["item"], account, generation),
    ).fetchone()
    if row is None:
        return None
    source = _source(account, json.loads(row[0]), generation)
    return source if source["receipt"] == receipt else None
