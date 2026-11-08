"""Read one stable graph proof for a batch, retaining every reply's own checks."""

import pytest
import sqlite3

from app.core import agent_output_store as module
from test_agent_output_store import operation, setup  # noqa: F401
from test_event_store import principal


def read_batch(outputs, db, context, operations):
    reader = getattr(outputs, "_read_many", None)
    return (
        {op: outputs._read(db, context, op) for op in operations}
        if reader is None
        else reader(db, context, operations)
    )


def records(outputs, count):
    operations = tuple(f"reply-{index}" for index in range(count))
    for op in operations:
        outputs.reserve_reply(principal("a"), operation(operation_id=op))
    return operations


def count_graphs(monkeypatch):
    calls = {name: 0 for name in ("shared", "generic", "narrative")}
    for name in calls:
        original = getattr(module, f"validate_{name}_graph")

        def counted(db, *, name=name, original=original):
            calls[name] += 1
            return original(db)

        monkeypatch.setattr(module, f"validate_{name}_graph", counted)
    return calls


@pytest.mark.parametrize("count", [1, 6])
def test_stable_transaction_validates_each_graph_once(setup, monkeypatch, count):  # noqa: F811
    store, outputs = setup
    operations = records(outputs, count)
    expected = {op: outputs.read_reply(principal("a"), op) for op in operations}
    calls = count_graphs(monkeypatch)
    with store._open() as db:
        db.execute("BEGIN")
        assert read_batch(outputs, db, principal("a"), operations) == expected
    assert calls == {"shared": 1, "generic": 1, "narrative": 1}


def test_autocommit_batch_owns_and_closes_a_short_read_snapshot(setup, monkeypatch):  # noqa: F811
    store, outputs = setup
    operations = records(outputs, 3)
    calls = count_graphs(monkeypatch)
    with store._open() as db:
        assert not db.in_transaction
        before = db.total_changes
        assert all(read_batch(outputs, db, principal("a"), operations).values())
        assert not db.in_transaction
        assert db.total_changes == before
    assert calls == {"shared": 1, "generic": 1, "narrative": 1}


def test_write_after_graph_validation_requires_fresh_checks(setup, monkeypatch):  # noqa: F811
    store, outputs = setup
    operations = records(outputs, 3)
    record_reader = getattr(outputs, "_read_record", outputs._read)
    calls = count_graphs(monkeypatch)
    changed = []

    def changed_before_record(db, *args, **kwargs):
        if not changed:
            db.execute("UPDATE private_payloads SET retention_status='deleted'")
            changed.append(True)
        return record_reader(db, *args, **kwargs)

    attribute = "_read_record" if hasattr(outputs, "_read_record") else "_read"
    monkeypatch.setattr(outputs, attribute, changed_before_record)
    with store._open() as db:
        db.execute("BEGIN IMMEDIATE")
        result = read_batch(outputs, db, principal("a"), operations)
        assert result == dict.fromkeys(operations)
    assert calls["shared"] > 1


def test_empty_and_other_participant_batches_do_not_expose_replies(setup):  # noqa: F811
    store, outputs = setup
    operations = records(outputs, 2)
    with store._open() as db:
        db.execute("BEGIN")
        assert read_batch(outputs, db, principal("a"), ()) == {}
        assert read_batch(outputs, db, principal("b"), operations) == dict.fromkeys(
            operations
        )


def test_invalid_graph_suppresses_the_complete_batch(setup, monkeypatch):  # noqa: F811
    store, outputs = setup
    operations = records(outputs, 2)

    def invalid(_db):
        raise ValueError("invalid protected graph")

    monkeypatch.setattr(module, "validate_shared_graph", invalid)
    with store._open() as db:
        db.execute("BEGIN")
        assert read_batch(outputs, db, principal("a"), operations) == dict.fromkeys(
            operations
        )


def test_new_transaction_observes_another_connections_source_change(setup, monkeypatch):  # noqa: F811
    store, outputs = setup
    operations = records(outputs, 2)
    original = outputs._read_record
    calls = count_graphs(monkeypatch)
    changed = []

    def replaced_snapshot(db, *args, **kwargs):
        if not changed:
            db.commit()
            with sqlite3.connect(store.database_path) as other:
                other.execute("UPDATE private_payloads SET retention_status='deleted'")
            db.execute("BEGIN")
            changed.append(True)
        return original(db, *args, **kwargs)

    monkeypatch.setattr(outputs, "_read_record", replaced_snapshot)
    with store._open() as db:
        db.execute("BEGIN")
        assert read_batch(outputs, db, principal("a"), operations) == dict.fromkeys(
            operations
        )
    assert calls["shared"] > 1


def test_ended_transaction_does_not_keep_batch_proof(setup, monkeypatch):  # noqa: F811
    store, outputs = setup
    operations = records(outputs, 2)
    original = outputs._read_record
    calls = count_graphs(monkeypatch)

    def ended(db, *args, **kwargs):
        db.commit()
        return original(db, *args, **kwargs)

    monkeypatch.setattr(outputs, "_read_record", ended)
    with store._open() as db:
        db.execute("BEGIN")
        assert all(read_batch(outputs, db, principal("a"), operations).values())
    assert calls == {"shared": 3, "generic": 3, "narrative": 3}
