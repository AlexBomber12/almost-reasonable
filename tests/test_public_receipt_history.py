"""Growing protected history stays reconcilable, erasable and restorable."""

from datetime import timedelta
import json
import sqlite3
from threading import Event, Thread

import pytest

from app.core.intellectual_state import digest, encoded, validate_private_graph
from app.core.public_intellectual_receipt import source_key, validate, validate_private
from app.privacy.application import EventStoreErasureAdapter, EventStoreRestoreAdapter
from app.privacy.deletion import DeletionCoordinator
from app.privacy.recovery import QuarantinedRestore, create_sqlite_backup
from test_int24_runtime import NOW
from test_privacy_deletion import context, request
from test_public_experience import wake
from test_public_private_intellectual import integrated as integrated_fixture


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


@pytest.fixture
def integrated(tmp_path):
    yield from integrated_fixture.__wrapped__(tmp_path)


@pytest.fixture
def running_private(integrated, monkeypatch):
    server = integrated[-1]
    stopped, idle = Event(), Event()
    accept = server._accept_connection

    def observed_accept():
        connection = accept()
        if connection is None:
            idle.set()
        return connection

    monkeypatch.setattr(server, "_accept_connection", observed_accept)
    thread = Thread(
        target=lambda: server.serve(should_stop=stopped.is_set), daemon=True
    )
    thread.start()
    try:
        # A normal idle accept timeout must not end this production server loop.
        assert idle.wait(timeout=3)
        yield
    finally:
        stopped.set()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("delete", [False, True])
def test_growing_public_receipts_survive_graph_validation_restore_and_deletion(
    integrated, running_private, tmp_path, delete
):
    public, worker, _, _, clock, _, _, a, _, events, ledger, gate, sdk, server = (
        integrated
    )
    calls = len(sdk.calls)
    for turn in range(5):
        question = (
            "MULTI_TURN_DELETE_CANARY Обсудим интерес."
            if turn == 0
            else "LATER_OWN_INPUT Продолжим разговор."
        )
        status, _, body = a.submit(f"growing-{turn}", question)
        assert (
            status == "201 Created" and json.loads(body)["model_state"] == "completed"
        )
        assert len(sdk.calls) == calls + turn + 1
        frame = json.loads(sdk.calls[-1]["input"])["intellectual_state"]
        assert len(frame["sources"]) <= 6
        with events._open() as db:
            validate_private_graph(db)
        assert gate.reconcile()
        if turn < 4:
            for item in range(6):
                clock.advance(timedelta(days=1))
                wake(public, worker, clock, f"cohort-{turn}-{item}")
    with events._open() as db:
        snapshots = [
            json.loads(row[0])
            for row in db.execute("SELECT snapshot FROM agent_intellectual_operations")
        ]
        assert max(len(row["external_receipts"]) for row in snapshots) > 6
        validate_private_graph(db)
        root = db.execute(
            "SELECT event_id FROM event_envelopes WHERE source_message_id='growing-0'"
        ).fetchone()[0]
    assert gate.reconcile()
    backup = tmp_path / "growing-history.db"
    create_sqlite_backup(events.database_path, backup, 14, NOW)
    before = tmp_path / "verified-before.db"
    assert (
        QuarantinedRestore(ledger, 14, EventStoreRestoreAdapter)
        .restore(backup, before, NOW)
        .activation_eligible
    )
    if delete:
        result = DeletionCoordinator(ledger, EventStoreErasureAdapter(events)).delete(
            context(), request(content_reference=root)
        )
        assert result.disposition.value == "applied" and gate.reconcile()
        after = tmp_path / "verified-after.db"
        assert (
            QuarantinedRestore(ledger, 14, EventStoreRestoreAdapter)
            .restore(backup, after, NOW)
            .activation_eligible
        )
        for path in (events.database_path, after):
            with sqlite3.connect(path) as db:
                validate_private_graph(db)
                dump = "\n".join(db.iterdump())
                assert (
                    "MULTI_TURN_DELETE_CANARY" not in dump and "LATER_OWN_INPUT" in dump
                )


@pytest.mark.parametrize("limit", ["count", "bytes"])
def test_historical_receipt_bounds_reject_oversized_protected_snapshots(limit):
    receipts = {}
    for index in range(101 if limit == "count" else 30):
        receipt = {
            "version": "public-intellectual-receipt-v1",
            "account": "account-1",
            "item": f"item-{index}",
            "revision": 1,
            "generation": 0,
            "state_digest": "1" * 64,
            "dependency_digest": "2" * 64,
            "record_digest": "3" * 64,
            "shared": []
            if limit == "count"
            else [f"shared-{item:02d}-" + "x" * 72 for item in range(16)],
        }
        validate(receipt)
        receipts[source_key(receipt["account"], receipt["item"])] = receipt
    deps = {key: digest(receipt) for key, receipt in receipts.items()}
    if limit == "bytes":
        assert (
            len(encoded({"dependencies": deps, "external_receipts": receipts}).encode())
            > 45056
        )
    with (
        sqlite3.connect(":memory:") as db,
        pytest.raises(ValueError, match="receipt set mismatch"),
    ):
        validate_private(
            db,
            "operation",
            {
                "dependencies": deps,
                "external_receipts": receipts,
                "source_keys": [],
                "projection": {"sources": []},
            },
        )
