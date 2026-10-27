"""Ordinary finite authority, historical consumption and clock/restart bounds."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from app.ops import private_pilot_ops
from app.private_web.cli import _narrative_permission_current
from app.trusted.private_pilot import (
    PilotAllowance,
    PilotAuthorityError,
    load_activation,
)
from app.trusted.private_pilot_worker import PilotRequestGate, PilotStop
from test_int22_pilot import activation_file, _rewrite
from test_private_combined_journey import _permission


NOW = datetime(2026, 10, 27, 10, tzinfo=UTC)


def grant(tmp_path, *, quota=40):
    path, value = activation_file(tmp_path)
    value.update(
        version="private-operating-grant-v1",
        not_before=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(NOW + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        max_operations=quota,
    )
    _rewrite(path, value)
    return path, value, load_activation(path, now=NOW)


def test_ordinary_selected_bounds_and_permission_are_independent_of_acceptance(
    tmp_path,
):
    path, value, authority = grant(tmp_path)
    assert authority.ordinary and not authority.memory_acceptance
    assert authority.max_operations == 40
    assert load_activation(path, now=NOW + timedelta(hours=25)) == authority
    narrative = _permission(
        path.parent / "narrative.json",
        "private-narrative-permission-v3",
        authority.pilot_id,
        40,
    )
    assert _narrative_permission_current(narrative, authority.pilot_id)
    allowance = PilotAllowance.initialize(tmp_path / "allowance.db", authority)
    gate = PilotRequestGate(
        authority,
        allowance,
        activation_path=path,
        stop=PilotStop(),
        narrative_permission_path=narrative,
        clock=lambda: NOW,
    )
    assert gate.current() and gate.narrative_limit() == 40
    _permission(narrative, "private-narrative-permission-v3", authority.pilot_id, 41)
    assert gate.narrative_limit() is None
    value["task"] = "synthetic-private-combined-memory-b03e"
    _rewrite(path, value)
    assert not gate.current()


@pytest.mark.parametrize("quota", [0, -1, True, 2**63, 1.5])
def test_ordinary_invalid_quota(tmp_path, quota):
    with pytest.raises(PilotAuthorityError):
        grant(tmp_path, quota=quota)


def test_clock_watermark_expiry_and_read_only_inspection_survive_restart(tmp_path):
    path, value, authority = grant(tmp_path)
    db = tmp_path / "allowance.db"
    allowance = PilotAllowance.initialize(db, authority)
    assert allowance.current(NOW + timedelta(hours=3))
    assert not PilotAllowance(db, authority).current(NOW + timedelta(hours=2))
    assert allowance.current(NOW + timedelta(hours=4))
    assert not allowance.current(authority.expires_at)
    assert not PilotAllowance(db, authority).current(NOW + timedelta(hours=4))
    before = db.read_bytes()
    assert private_pilot_ops.plan(path, now=authority.expires_at)["current"] is False
    assert private_pilot_ops.plan(path, now=NOW)["max_operations"] == 40
    assert allowance.remaining() == 40
    assert db.read_bytes() == before
    with pytest.raises(PilotAuthorityError):
        load_activation(path, now=authority.expires_at)
    assert (
        load_activation(path, now=authority.expires_at, require_current=False)
        == authority
    )


def test_replacement_preserves_legacy_exhaustion_and_global_operations(tmp_path):
    path, value = activation_file(tmp_path)
    legacy = load_activation(path)
    db = tmp_path / "allowance.db"
    allowance = PilotAllowance.initialize(db, legacy)
    for i in range(5):
        assert allowance.reserve("reply:" + f"{i:064x}")
    value.update(
        version="private-operating-grant-v1",
        max_operations=40,
        not_before=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(NOW + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    _rewrite(path, value)
    ordinary = load_activation(path, now=NOW)
    with pytest.raises(PilotAuthorityError):
        PilotAllowance(db, ordinary)
    PilotAllowance.replace(db, ordinary)
    assert PilotAllowance(db, legacy).remaining() == 0
    fresh = PilotAllowance(db, ordinary)
    assert not allowance.reserve("reply:" + "f" * 64)
    assert not fresh.reserve("reply:" + "0" * 64)
    assert fresh.reserve(
        "narrative:" + "a" * 64, extraction_limit=40, reserve_reply=True
    )
    assert fresh.remaining() == 38
    value["pilot_id"] = "replacement"
    _rewrite(path, value)
    replacement = load_activation(path, now=NOW)
    PilotAllowance.replace(db, replacement)
    assert fresh.remaining() == 38
    assert not fresh.current(NOW)
    assert not fresh.reserve("reply:" + "a" * 64)
    latest = PilotAllowance(db, replacement)
    assert not latest.reserve("reply:" + "a" * 64)  # Old hold cannot cross grants.
    assert not latest.reserve(
        "narrative:" + "a" * 64, extraction_limit=40, reserve_reply=True
    )
    assert latest.reserve("reply:" + "b" * 64)
    assert latest.remaining() == 39
    with pytest.raises(PilotAuthorityError):
        PilotAllowance.replace(db, ordinary)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM consumed").fetchone()[0] == 5
        assert conn.execute("SELECT count(*) FROM operating_grants").fetchone()[0] == 2
    with pytest.raises(PilotAuthorityError):
        PilotAllowance.initialize(db, replacement)


def test_competing_instances_share_quota_and_uncertain_hold(tmp_path):
    _path, _value, authority = grant(tmp_path, quota=3)
    db = tmp_path / "allowance.db"
    first = PilotAllowance.initialize(db, authority)
    assert first.reserve(
        "narrative:" + "a" * 64, extraction_limit=3, reserve_reply=True
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(
            pool.map(
                lambda i: PilotAllowance(db, authority).reserve("reply:" + f"{i:064x}"),
                range(8),
            )
        )
    assert sum(outcomes) == 1
    assert first.remaining() == 0
    assert PilotAllowance(db, authority).reserve("reply:" + "a" * 64)
    assert not first.reserve("reply:" + "a" * 64)
    assert first.remaining() == 0


def test_candidate_planning_prepare_replace_and_expired_status_are_offline(
    tmp_path, capsys
):
    from app.core.store import EventStore
    from test_int22_controls import _stop

    path, _value, authority = grant(tmp_path)
    db = tmp_path / "allowance.db"
    assert private_pilot_ops.main(["plan", "--activation", str(path)]) == 0
    assert authority.digest in capsys.readouterr().out
    assert not db.exists()
    private_pilot_ops.prepare(path, db)  # future grants can be prepared offline
    with pytest.raises(PilotAuthorityError):
        private_pilot_ops.replace(path, db)  # exclusive selection, no refill
    provider = tmp_path / "provider.db"
    EventStore(provider)
    stop = path.parent / "stop.json"
    _stop(stop, True)
    before = {p: p.read_bytes() for p in (db, provider, stop, path)}
    state = private_pilot_ops.inspect(
        path, db, stop, provider, now=authority.expires_at
    )
    assert not state["ready"] and not state["authority_current"]
    assert state["remaining_operations"] == 40 and state["worker_stopped"]
    assert {p: p.read_bytes() for p in before} == before
    allowance = PilotAllowance(db, authority)
    assert allowance.current(NOW, observe=False)
    assert not allowance.current(NOW - timedelta(seconds=1), observe=False)
    for i in range(40):
        assert allowance.reserve("reply:" + f"{i:064x}")
    state = private_pilot_ops.inspect(path, db, stop, provider, now=NOW)
    assert not state["ready"] and state["remaining_operations"] == 0
    provider.write_bytes(b"not sqlite")
    with pytest.raises(PilotAuthorityError, match="provider state unavailable"):
        private_pilot_ops.inspect(path, db, stop, provider, now=NOW)


def test_replace_cli_retains_old_state_and_refuses_legacy_or_missing_database(
    tmp_path, capsys
):
    path, value, authority = grant(tmp_path)
    db = tmp_path / "allowance.db"
    PilotAllowance.initialize(db, authority)
    value["pilot_id"] = "explicit-replacement"
    _rewrite(path, value)
    assert (
        private_pilot_ops.main(
            ["replace", "--activation", str(path), "--allowance", str(db)]
        )
        == 0
    )
    assert "historical consumption" in capsys.readouterr().out
    assert not PilotAllowance(db, authority).current(NOW)
    with pytest.raises(PilotAuthorityError, match="ordinary grant required"):
        from dataclasses import replace

        PilotAllowance.replace(db, replace(authority, ordinary=False))
    with pytest.raises(PilotAuthorityError):
        PilotAllowance.replace(tmp_path / "missing.db", authority)
    empty = tmp_path / "empty.db"
    empty.touch(mode=0o600)
    with pytest.raises(PilotAuthorityError, match="grant preparation failed"):
        PilotAllowance.replace(empty, authority)
    assert empty.read_bytes() == b""
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM authority")
    with pytest.raises(PilotAuthorityError, match="invalid historical allowance"):
        PilotAllowance.replace(db, authority)


def test_binding_corruption_and_file_replacement_fail_closed(tmp_path, monkeypatch):
    from app.trusted import private_pilot

    path, value, authority = grant(tmp_path)
    db = tmp_path / "allowance.db"
    allowance = PilotAllowance.initialize(db, authority)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE operating_grants SET max_operations=41")
    with pytest.raises(PilotAuthorityError, match="binding changed"):
        allowance.remaining()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE operating_grants SET max_operations=40, last_seen='broken'"
        )
    with pytest.raises(PilotAuthorityError, match="state unavailable"):
        allowance.current(NOW)
    original = private_pilot.protected_state_identity
    calls = []

    def changed(path):
        calls.append(path)
        identity = original(path)
        return identity if len(calls) == 1 else (identity[0], identity[1], -1)

    value["pilot_id"] = "new-candidate"
    _rewrite(path, value)
    monkeypatch.setattr(private_pilot, "protected_state_identity", changed)
    with pytest.raises(PilotAuthorityError, match="location changed"):
        PilotAllowance.replace(db, load_activation(path, now=NOW))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM operating_grants").fetchone() == (1,)
    monkeypatch.undo()
    db.write_bytes(b"corrupt")
    with pytest.raises(PilotAuthorityError, match="state unavailable"):
        allowance.current(NOW)


def test_additional_limit_and_expiry_hold_do_not_refund_or_cross_grants(tmp_path):
    _path, _value, authority = grant(tmp_path, quota=5)
    db = tmp_path / "allowance.db"
    allowance = PilotAllowance.initialize(db, authority)
    assert allowance.reserve("evolution:" + "a" * 64, extraction_limit=1)
    assert not allowance.reserve("typed:" + "b" * 64, extraction_limit=1)
    assert allowance.reserve(
        "narrative:" + "c" * 64, extraction_limit=5, reserve_reply=True
    )
    assert not allowance.current(authority.expires_at)
    assert not allowance.reserve("reply:" + "c" * 64)
    assert not allowance.reserve("reply:" + "d" * 64)
    assert allowance.remaining() == 2


def test_inactive_or_uninitialized_worker_never_constructs_transport(
    tmp_path, monkeypatch
):
    from app.trusted import private_pilot_worker

    path, _value, authority = grant(tmp_path)
    db = tmp_path / "allowance.db"
    PilotAllowance.initialize(db, authority)
    provider = tmp_path / "provider.db"
    provider.touch(mode=0o600)
    seen = []
    monkeypatch.setattr(
        private_pilot_worker,
        "build_worker_openai_transport",
        lambda **_kwargs: seen.append("transport"),
    )
    arguments = dict(
        socket_path=tmp_path / "worker.sock",
        provider_database=provider,
        allowance_database=db,
        activation_path=path,
    )
    with pytest.raises(PilotAuthorityError, match="inactive"):
        private_pilot_worker.build_worker(
            **arguments, clock=lambda: NOW - timedelta(seconds=1)
        )
    with pytest.raises(PilotAuthorityError, match="initialized"):
        private_pilot_worker.build_worker(**arguments, clock=lambda: NOW)
    assert provider.read_bytes() == b"" and not seen
    with pytest.raises(PilotAuthorityError, match="inactive"):
        private_pilot_worker.build_worker(
            **arguments, clock=lambda: authority.expires_at
        )
    with pytest.raises(PilotAuthorityError, match="inactive"):
        private_pilot_worker.build_worker(**arguments, clock=lambda: NOW)
    assert not seen


def test_retired_legacy_worker_cannot_resume_or_commit_after_replacement(tmp_path):
    from test_int22_controls import _stop

    path, value = activation_file(tmp_path)
    legacy = load_activation(path)
    db = tmp_path / "allowance.db"
    allowance = PilotAllowance.initialize(db, legacy)
    stop = path.parent / "stop.json"
    _stop(stop, False)
    gate = PilotRequestGate(
        legacy, allowance, activation_path=path, stop=PilotStop(stop)
    )
    assert gate.current()
    value.update(version="private-operating-grant-v1", max_operations=30)
    _rewrite(path, value)
    PilotAllowance.replace(db, load_activation(path))
    value["version"] = "private-pilot-activation-v1"
    value["max_operations"] = 5
    _rewrite(path, value)
    assert not gate.current()
    assert gate.paid_transport_blocked()
    assert allowance.remaining() == 5  # old capacity cannot be reused
    db.write_bytes(b"corrupt")
    with pytest.raises(PilotAuthorityError):
        allowance.current(NOW)


@pytest.mark.parametrize("change", ["expiry", "artifact", "replacement", "stop"])
def test_paid_plain_reply_is_held_if_authority_changes_before_worker_release(
    tmp_path, monkeypatch, change
):
    from types import SimpleNamespace
    from threading import Thread
    import os
    from app.cognition.conversation_context import MemoryModelRequest
    from app.core.openai_worker_client import OpenAIWorkerClient
    from app.core.store import EventStore
    from app.trusted import private_pilot_worker
    from app.trusted.openai_transport import FakeOpenAITransport
    from test_int22_controls import _stop
    from test_openai_worker_protocol import model_request
    from test_b05e_combined import reply

    path, value, authority = grant(tmp_path)
    db = tmp_path / "allowance.db"
    allowance = PilotAllowance.initialize(db, authority)
    provider = tmp_path / "provider.db"
    EventStore(provider)
    stop = path.parent / "stop.json"
    _stop(stop, False)
    clock = SimpleNamespace(now=NOW)
    fake = FakeOpenAITransport([reply("Синтетический ответ.")])
    original = fake.call

    def changed(request, *, timeout_seconds):
        result = original(request, timeout_seconds=timeout_seconds)
        if change == "expiry":
            clock.now = authority.expires_at
        elif change == "stop":
            _stop(stop, True)
        else:
            value["pilot_id"] = "separately-approved-replacement"
            _rewrite(path, value)
            if change == "replacement":
                PilotAllowance.replace(db, load_activation(path, now=NOW))
        return result

    fake.call = changed
    monkeypatch.setattr(
        private_pilot_worker, "build_worker_openai_transport", lambda **_kwargs: fake
    )
    worker = private_pilot_worker.build_worker(
        socket_path=tmp_path / "worker.sock",
        provider_database=provider,
        allowance_database=db,
        activation_path=path,
        stop_path=stop,
        clock=lambda: clock.now,
    )
    worker.start()
    thread = Thread(target=worker.serve, kwargs={"max_connections": 1})
    thread.start()
    try:
        result = OpenAIWorkerClient(
            tmp_path / "worker.sock", expected_worker_uid=os.geteuid()
        ).infer(
            MemoryModelRequest(model_request(), (), False),
            operation_id="reply:" + "a" * 64,
            correlation_id="plain-reply",
        )
        assert result.outcome == "authorization_rejected"
        assert result.validation is None
        assert len(fake.calls) == 1 and allowance.remaining() == 39
        with sqlite3.connect(provider) as connection:
            assert connection.execute(
                "SELECT count(*) FROM provider_usage_attempts"
            ).fetchone() == (1,)
    finally:
        thread.join(5)
        worker.close()
        assert not thread.is_alive()


def _process_reserve(arguments):
    db, authority, index = arguments
    return PilotAllowance(db, authority).reserve("reply:" + f"{index:064x}")


def test_competing_processes_and_reopened_state_cannot_exceed_quota(tmp_path):
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing

    _path, _value, authority = grant(tmp_path, quota=3)
    db = tmp_path / "allowance.db"
    allowance = PilotAllowance.initialize(db, authority)
    with ProcessPoolExecutor(
        max_workers=4, mp_context=multiprocessing.get_context("fork")
    ) as pool:
        results = list(
            pool.map(_process_reserve, [(db, authority, i) for i in range(12)])
        )
    assert sum(results) == 3
    assert allowance.remaining() == PilotAllowance(db, authority).remaining() == 0
