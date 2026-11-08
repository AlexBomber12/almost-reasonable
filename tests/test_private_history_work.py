"""History keeps every live check without repeating whole-graph work per reply."""

from collections import Counter

import pytest

from app.core import agent_output_store as outputs_module
from test_agent_intellectual_state import private_submit
from test_cognition_pipeline import principal
import test_public_private_intellectual as public_cases

integrated = public_cases.integrated


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


@pytest.mark.parametrize("history_size", [1, 6])
def test_history_validates_graphs_and_public_catalog_in_bounded_batches(
    integrated, monkeypatch, history_size
):
    bridge, app, browser, sdk, server = (
        integrated[5],
        integrated[6],
        integrated[7],
        integrated[12],
        integrated[13],
    )
    runtime = app._conversations
    for index in range(history_size):
        assert (
            private_submit(server, browser, f"past-{index}", "Обсудим интересы.")[
                "model_state"
            ]
            == "completed"
        )
    expected = runtime.history(principal("a"))
    calls = Counter()
    for name in ("shared", "generic", "narrative"):
        original = getattr(outputs_module, f"validate_{name}_graph")

        def counted(db, *, name=name, original=original):
            calls[name] += 1
            return original(db)

        monkeypatch.setattr(outputs_module, f"validate_{name}_graph", counted)
    source = bridge.view.intellectual.sources

    def counted_sources(**kwargs):
        calls["public_catalog"] += 1
        return source(**kwargs)

    monkeypatch.setattr(bridge.view.intellectual, "sources", counted_sources)
    assert runtime.history(principal("a")) == expected
    assert len(sdk.calls) == history_size
    assert all(calls[name] <= 2 for name in ("shared", "generic", "narrative")), calls
    assert calls["public_catalog"] <= 3, calls


def test_public_withdrawal_during_history_hides_all_dependent_replies(
    integrated, monkeypatch
):
    public, bridge, app, browser, server = (
        integrated[0],
        integrated[5],
        integrated[6],
        integrated[7],
        integrated[13],
    )
    for index in range(3):
        assert (
            private_submit(server, browser, f"withdraw-{index}", "Обсудим позицию.")[
                "model_state"
            ]
            == "completed"
        )
    original = bridge.historical_sources_batch
    withdrawn = []

    def withdraw_after_catalog(*args, **kwargs):
        result = original(*args, **kwargs)
        if not withdrawn:
            with public.service.store._open() as db:
                db.execute(
                    "DELETE FROM threads_inbound_events WHERE account_ref=?",
                    (public.account,),
                )
            withdrawn.append(True)
        return result

    monkeypatch.setattr(bridge, "historical_sources_batch", withdraw_after_catalog)
    history = app._conversations.history(principal("a"))
    assert withdrawn
    assert len([x for x in history if x.role == "participant"]) == 3
    assert not any(x.role == "agent" for x in history)
