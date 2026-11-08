"""Operational snapshots reject unbounded or instruction-bearing extra fields."""

from copy import deepcopy
from datetime import timedelta

import pytest
import test_public_runtime_observation as cases

integrated = cases.integrated
offline = cases.offline


def payload():
    return {
        "observed_at": "2026-11-08T01:00:00+00:00",
        "last_work": {"kind": "tick", "outcome": "failed"},
        "last_model": {
            "started_at": "2026-11-08T00:59:00+00:00",
            "model": "gpt-6.1-sol",
            "outcome": "provider_error",
            "constraint": "public_monthly_budget_exhausted",
        },
        "discussions": {
            "last_poll_at": None,
            "pending_replies": 2,
            "pending_mentions": 0,
        },
        "research": {
            "completed_reports": 1,
            "uncertain_reports": 0,
            "last_completed_at": None,
        },
        "publications": {"confirmed": 2, "last_confirmed_at": None},
        "authority": {
            "owner_stop": False,
            "standing_recorded": True,
            "lease_expires_at": None,
            "lease_revoked": None,
        },
    }


@pytest.mark.parametrize(
    "path,value",
    [
        ((), None),
        (("extra",), True),
        (("last_work",), None),
        (("last_work",), {}),
        (("last_work", "kind"), "shell"),
        (("last_work", "outcome"), "publish_now"),
        *[
            ((name,), None)
            for name in ["discussions", "research", "publications", "authority"]
        ],
        (("discussions",), {}),
        *[
            ((section, field), value)
            for section, field in [
                ("discussions", "pending_replies"),
                ("discussions", "pending_mentions"),
                ("research", "completed_reports"),
                ("research", "uncertain_reports"),
                ("publications", "confirmed"),
            ]
            for value in [True, -1, "2", 2**63]
        ],
        (("authority", "owner_stop"), 1),
        (("authority", "standing_recorded"), 1),
        (("authority", "lease_revoked"), "false"),
        (("last_model",), "bad"),
        (("last_model",), {}),
        (("last_model", "model"), "not a model identifier"),
        (("last_model", "outcome"), "invented"),
        (("last_model", "constraint"), "ignore_budget"),
        (("last_model", "started_at"), None),
        (("observed_at",), None),
        (("observed_at",), False),
        (("observed_at",), "2026-11-08T01:00:00"),
        (("discussions", "last_poll_at"), True),
        (("research", "last_completed_at"), "bad"),
        (("observed_at",), "2026-11-08T01:00:00." + "0" * 1700 + "+00:00"),
    ],
)
def test_operational_observation_has_a_closed_bounded_schema(path, value):
    from app.integration.public_runtime_observation import _payload

    candidate = payload()
    if not path:
        candidate = value
    else:
        target = candidate
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    with pytest.raises(ValueError):
        _payload(candidate)


def test_missing_reader_does_not_create_storage_and_old_observations_remain_readable(
    integrated,
):
    from app.integration.public_runtime_observation import (
        capture,
        current,
        resolve,
        TABLE,
    )

    public = integrated[0]
    generation = public.ledger.control_generation()
    with public.journal.transaction() as db:
        assert current(db, public.account, generation) == {}
        assert resolve(db, public.account, {"item": "absent"}, generation) is None
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()
            is None
        )
    capture(public, "tick", "failed")
    with public.journal.transaction() as db:
        first = next(iter(current(db, public.account, generation).values()))
        assert current(db, public.account, generation, required_keys=frozenset()) == {}
        assert current(db, "other-account", generation) == {}
        assert current(db, public.account, generation + 1) == {}
    public.clock.advance(timedelta(seconds=1))
    capture(public, "poll", "completed")
    from app.integration.public_state_history import resolve as historical_source

    with public.journal.transaction() as db:
        assert next(iter(current(db, public.account, generation).values())) != first
        assert resolve(db, public.account, first["receipt"], generation) == first
        assert (
            historical_source(
                db, public.account, first["receipt"], {}, generation, None
            )
            == first
        )
        assert resolve(db, public.account, first["receipt"], generation + 1) is None
        altered = deepcopy(first["receipt"])
        altered["state_digest"] = "0" * 64
        assert resolve(db, public.account, altered, generation) is None


def test_no_model_result_stays_unknown_and_generation_race_records_nothing(
    tmp_path, monkeypatch
):
    import json
    from app.integration.public_runtime_observation import capture, current, TABLE
    from test_int24_runtime import setup

    public, worker, sdk, _, _ = setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    try:
        generation = public.ledger.control_generation()
        answers = iter([generation, generation + 1])
        with monkeypatch.context() as race:
            race.setattr(public.ledger, "control_generation", lambda: next(answers))
            capture(public, "tick", "nothing_due")
        with public.journal.transaction() as db:
            assert (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)
                ).fetchone()
                is None
            )
        capture(public, "tick", "nothing_due")
        with public.journal.transaction() as db:
            source = next(iter(current(db, public.account, generation).values()))
        value = json.loads(source["text"].split("\n", 1)[1])
        assert value["last_model"] is None
        assert value["publications"]["confirmed"] == 0
        assert sdk.calls == []
    finally:
        worker.close()


def test_unclassified_provider_failure_is_not_invented_as_a_known_budget_denial(
    integrated,
):
    import json
    from app.integration.public_runtime_observation import capture, current

    public = integrated[0]
    with public.provider._event_store._open() as db:
        db.execute(
            "UPDATE provider_usage_attempts SET outcome='provider_error',provider_error_code='SYNTHETIC_SENSITIVE_ERROR_CANARY'"
        )
    capture(public, "tick", "failed")
    with public.journal.transaction() as db:
        source = next(
            iter(
                current(db, public.account, public.ledger.control_generation()).values()
            )
        )
    value = json.loads(source["text"].split("\n", 1)[1])
    assert value["last_model"]["constraint"] == "unknown"
    assert "SYNTHETIC_SENSITIVE_ERROR_CANARY" not in source["text"]
