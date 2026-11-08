"""Recorded operational facts reach private dialogue without control authority."""

import json
from datetime import timedelta

import pytest

import test_public_private_intellectual as public_cases
from test_agent_intellectual_state import private_submit
from test_cognition_pipeline import principal
from test_public_experience import wake

integrated = public_cases.integrated


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


def observation(sdk):
    sources = json.loads(sdk.calls[-1]["input"])["intellectual_state"]["sources"]
    records = [source for source in sources if source["kind"] == "capability_snapshot"]
    assert len(records) == 1
    return json.loads(records[0]["text"].split("\n", 1)[1])


def test_public_budget_denial_and_pending_comments_reach_both_private_participants(
    integrated, monkeypatch
):
    from app.core.public_rejection import PublicOutputRejected
    from app.integration.public_runtime_observation import capture

    public, worker, sdk, _, clock, bridge, _, a, b, _, _, _, private_sdk, server = (
        integrated
    )
    bridge.include_public_activity = True

    def denied(**_kwargs):
        raise PublicOutputRejected(
            "public_monthly_budget_exhausted", "synthetic budget denial"
        )

    monkeypatch.setattr(sdk, "create", denied)
    wake(public, worker, clock, "budget-denied")
    capture(public, "tick", "failed")
    public_calls = len(sdk.calls)
    for actor, identity, text in [
        (a, "status-a", "Почему нет новых публикаций? PRIVATE_A_CANARY"),
        (b, "status-b", "Ты можешь сейчас отвечать публично?"),
    ]:
        assert (
            private_submit(server, actor, identity, text)["model_state"] == "completed"
        )
        value = observation(private_sdk)
        assert value["last_model"]["constraint"] == "public_monthly_budget_exhausted"
        assert value["last_work"] == {"kind": "tick", "outcome": "failed"}
        assert value["discussions"]["pending_replies"] == 1
        assert value["observed_at"] == clock.now().isoformat()
        assert value["authority"]["owner_stop"] is False
        assert str(public.journal.path) not in private_sdk.calls[-1]["input"]
    assert "PRIVATE_A_CANARY" not in private_sdk.calls[-1]["input"]
    assert len(sdk.calls) == public_calls
    from app.integration.public_runtime_observation import TABLE

    with public.journal.transaction() as db:
        assert db.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 1


def test_new_observation_updates_current_context_without_erasing_recorded_history(
    integrated,
):
    from app.integration.public_runtime_observation import capture

    public, _, _, _, clock, bridge, app, a, _, _, _, _, sdk, server = integrated
    bridge.include_public_activity = True
    capture(public, "tick", "failed")
    assert (
        private_submit(server, a, "first-status", "Что происходило?")["model_state"]
        == "completed"
    )
    first = observation(sdk)
    clock.advance(timedelta(seconds=1))
    capture(public, "poll", "completed")
    assert (
        private_submit(
            server, a, "second-status", "Каково последнее наблюдаемое состояние?"
        )["model_state"]
        == "completed"
    )
    second = observation(sdk)
    assert first["observed_at"] != second["observed_at"]
    assert second["last_work"] == {"kind": "poll", "outcome": "completed"}
    assert (
        len(
            [x for x in app._conversations.history(principal("a")) if x.role == "agent"]
        )
        == 2
    )


@pytest.mark.parametrize("damage", ["payload", "identity", "source_key"])
def test_current_reader_rejects_payload_changed_without_its_record_identity(
    integrated, damage
):
    from app.integration.public_runtime_observation import capture, current, TABLE

    public = integrated[0]
    capture(public, "tick", "failed")
    with public.journal.transaction() as db:
        raw = json.loads(db.execute(f"SELECT payload FROM {TABLE}").fetchone()[0])
        if damage == "payload":
            raw["discussions"]["pending_replies"] = 999
            db.execute(f"UPDATE {TABLE} SET payload=?", (json.dumps(raw),))
        else:
            db.execute(f"UPDATE {TABLE} SET {damage}='tampered'")
        with pytest.raises(ValueError, match="identity"):
            current(db, public.account, public.ledger.control_generation())


def test_operating_window_records_the_confirmed_result_after_dispatch(tmp_path):
    from app.integration.public_runtime_observation import current
    from test_finite_public_grant import setup, spec, grant, run_window, POST

    public, worker, _, _, clock = setup(
        tmp_path, POST, autonomous_publication_enabled=True
    )
    window = spec(
        publication_grant="finite-public-grant-1",
        max_model_calls=1,
        max_poll_pages=0,
        max_inbound_events=0,
    )
    try:
        public.public_grants.issue(grant(window), window)
        result = run_window(public, worker, clock, window, 1)
        assert result["state"] == "exhausted"
        with public.journal.transaction() as db:
            source = next(
                iter(
                    current(
                        db, public.account, public.ledger.control_generation()
                    ).values()
                )
            )
        value = json.loads(source["text"].split("\n", 1)[1])
        assert value["publications"]["confirmed"] == 1
        assert value["authority"]["lease_expires_at"] == window.expires_at.isoformat()
        assert value["authority"]["lease_revoked"] is False
        assert value["last_model"]["outcome"] == "completed"
    finally:
        worker.close()


@pytest.mark.parametrize("withdraw", [False, True])
def test_dated_observation_survives_a_new_status_but_not_withdrawal(
    integrated, withdraw
):
    from app.integration.public_runtime_observation import capture, TABLE

    public, bridge, app, actor, sdk, server = (
        integrated[0],
        integrated[5],
        integrated[6],
        integrated[7],
        integrated[12],
        integrated[13],
    )
    bridge.include_public_activity = True
    capture(public, "tick", "failed")
    with public.journal.transaction() as db:
        original_identity = db.execute(f"SELECT identity FROM {TABLE}").fetchone()[0]

    def changed():
        public.clock.advance(timedelta(seconds=1))
        capture(public, "poll", "completed")
        if withdraw:
            with public.journal.transaction() as db:
                db.execute(
                    f"DELETE FROM {TABLE} WHERE identity=?", (original_identity,)
                )

    sdk.before_result = changed
    result = private_submit(
        server, actor, "status-during-response", "Что тебе известно о публичной работе?"
    )
    assert result["model_state"] == ("unavailable" if withdraw else "completed")
    assert observation(sdk)["last_work"]["outcome"] == "failed"
    assert len(
        [x for x in app._conversations.history(principal("a")) if x.role == "agent"]
    ) == (0 if withdraw else 1)
