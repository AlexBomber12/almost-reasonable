"""Fresh connected turns must not rebuild public ancestry for every old reply."""

import pytest
from copy import deepcopy
from collections import Counter

from test_agent_intellectual_state import private_submit
from test_cognition_pipeline import principal
from test_historical_public_context import public_revision
from test_public_experience import wake
import test_public_private_intellectual as public_cases

integrated = public_cases.integrated


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


@pytest.mark.parametrize("history_size", [1, 6])
def test_fresh_reply_has_bounded_public_graph_builds(
    integrated,
    monkeypatch,
    history_size,  # noqa: F811
):
    bridge, app, a, sdk, server = (
        integrated[5],
        integrated[6],
        integrated[7],
        integrated[12],
        integrated[13],
    )
    for index in range(history_size):
        assert (
            private_submit(server, a, f"past-{index}", "Обсудим интересы.")[
                "model_state"
            ]
            == "completed"
        )
    original = bridge.view.intellectual.sources
    builds = []
    old_operations = {
        x.event_id
        for x in app._conversations.history(principal("a"))
        if x.role == "agent"
    }
    reads = Counter()
    read = app._conversations._outputs._read

    def counted_read(db, participant, operation):
        if operation in old_operations:
            reads[operation] += 1
        return read(db, participant, operation)

    def counted(**kwargs):
        builds.append(True)
        return original(**kwargs)

    monkeypatch.setattr(bridge.view.intellectual, "sources", counted)
    monkeypatch.setattr(app._conversations._outputs, "_read", counted_read)
    assert (
        private_submit(server, a, "fresh", "Что изменилось в твоих интересах?")[
            "model_state"
        ]
        == "completed"
    )
    assert len(sdk.calls) == history_size + 1
    assert len(builds) <= 16, "fresh reply repeatedly reconstructs unrelated history"
    assert max(reads.values()) <= 8, (
        "projection repeatedly revalidates the same prior reply"
    )
    assert (
        len(
            [x for x in app._conversations.history(principal("a")) if x.role == "agent"]
        )
        == history_size + 1
    )


def test_source_withdrawal_after_admission_still_prevents_release(
    integrated,  # noqa: F811
):
    public, bridge, app, a, sdk, server = (
        integrated[0],
        integrated[5],
        integrated[6],
        integrated[7],
        integrated[12],
        integrated[13],
    )
    for index in range(4):
        assert (
            private_submit(server, a, f"retained-{index}", "Продолжим обсуждение.")[
                "model_state"
            ]
            == "completed"
        )

    def withdraw():
        with public.service.store._open() as db:
            db.execute(
                "DELETE FROM threads_inbound_events WHERE account_ref=?",
                (public.account,),
            )

    sdk.before_result = withdraw
    assert (
        private_submit(server, a, "withdraw-during-model", "Обсудим позицию.")[
            "model_state"
        ]
        == "unavailable"
    )
    assert bridge.sources() == {}
    assert not any(
        x.role == "agent" for x in app._conversations.history(principal("a"))
    )


def test_one_batch_keeps_historical_and_current_requirements_independent(
    integrated, monkeypatch
):
    public, worker, sdk, _, clock, bridge, _, _, _, events, *_ = integrated
    old = bridge.sources()
    old_receipts = {key: value["receipt"] for key, value in old.items()}
    public_revision(public, worker, sdk, clock, "batch-revision", 61)
    current = bridge.sources()
    current_receipts = {key: value["receipt"] for key, value in current.items()}
    original = bridge.view.intellectual.sources
    builds = []

    def counted(**kwargs):
        builds.append(True)
        return original(**kwargs)

    monkeypatch.setattr(bridge.view.intellectual, "sources", counted)
    requests = [
        (old_receipts, frozenset()),
        (old_receipts, frozenset(old_receipts)),
        (current_receipts, frozenset(current_receipts)),
        ({"bad": {}}, frozenset()),
        ({}, frozenset()),
    ]
    with events._open() as db:
        actual = bridge.historical_sources_batch(
            requests, principal=principal("a"), _private_db=db
        )
    historical = {
        key: {name: value for name, value in source.items() if name != "reasonless_key"}
        for key, source in old.items()
    }
    assert actual == [historical, {}, current, {}, {}]
    assert len(builds) == 2
    with public.service.store._open() as db:
        db.execute(
            "DELETE FROM threads_inbound_events WHERE account_ref=?", (public.account,)
        )
    with events._open() as db:
        assert bridge.historical_sources_batch(
            requests, principal=principal("a"), _private_db=db
        ) == [{} for _ in requests]


def test_batch_union_does_not_change_the_bound_of_an_individual_receipt_set(integrated):
    from app.core.public_intellectual_receipt import source_key

    bridge, events = integrated[5], integrated[9]
    current = bridge.sources()
    key, record = next(iter(current.items()))
    requests = [({key: record["receipt"]}, frozenset())]
    for index in range(100):
        receipt = deepcopy(record["receipt"])
        receipt["item"] = f"absent-{index}"
        requests.append(
            ({source_key(receipt["account"], receipt["item"]): receipt}, frozenset())
        )
    requests.append(({str(i): {} for i in range(101)}, frozenset()))
    with events._open() as db:
        actual = bridge.historical_sources_batch(
            requests, principal=principal("a"), _private_db=db
        )
    assert actual[0] == {key: record}
    assert actual[1:] == [{} for _ in requests[1:]]


def test_batch_generation_change_discards_every_result(integrated, monkeypatch):
    public, bridge, events = integrated[0], integrated[5], integrated[9]
    receipts = {key: value["receipt"] for key, value in bridge.sources().items()}
    original = bridge._sources

    def changed(**kwargs):
        result = original(**kwargs)
        with public.ledger._open() as db:
            public.ledger._increment_control_generation(db)
        return result

    monkeypatch.setattr(bridge, "_sources", changed)
    with events._open() as db:
        assert bridge.historical_sources_batch(
            [(receipts, frozenset())] * 2, principal=principal("a"), _private_db=db
        ) == [{}, {}]


def test_public_catalog_reads_control_generation_a_bounded_number_of_times(
    integrated, monkeypatch
):
    public, worker, sdk, _, clock, bridge, *_ = integrated
    sdk.proposal = {"decision": "no_action", "reason_category": "other"}
    for index in range(6):
        wake(public, worker, clock, f"catalog-history-{index}")
    original = bridge.view.ledger.control_generation
    reads = []

    def counted():
        reads.append(True)
        return original()

    monkeypatch.setattr(bridge.view.ledger, "control_generation", counted)
    assert bridge.view.intellectual.sources()
    assert len(reads) <= 6, (
        "catalog rereads the same control generation for each old intent"
    )


@pytest.mark.parametrize("reader", ["state", "experience"])
def test_generation_change_during_a_catalog_read_discards_the_catalog(
    integrated, monkeypatch, reader
):
    public, bridge = integrated[0], integrated[5]
    if reader == "state":
        owner = bridge.view.intellectual
        method = "_origin_allowed"
        read = owner.sources
    else:
        owner = bridge.view.experience_history
        method = "_event_allowed"
        read = owner._candidates
    original = getattr(owner, method)

    def changed(*args):
        result = original(*args)
        with public.ledger._open() as db:
            public.ledger._increment_control_generation(db)
        return result

    monkeypatch.setattr(owner, method, changed)
    assert read() == {}


def test_batch_drops_all_receipts_after_the_public_producer_binding_is_withdrawn(
    integrated,
):
    public, bridge, events = integrated[0], integrated[5], integrated[9]
    receipts = {key: source["receipt"] for key, source in bridge.sources().items()}
    assert receipts
    with public.journal.transaction() as db:
        db.execute("UPDATE public_runtime_schema SET account='withdrawn-account'")
    with events._open() as db:
        assert bridge.historical_sources_batch(
            [(receipts, frozenset()), (receipts, frozenset(receipts))],
            principal=principal("a"),
            _private_db=db,
        ) == [{}, {}]
