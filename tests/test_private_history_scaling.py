"""History validation must not rebuild unrelated source archives per reply."""

from test_agent_intellectual_state import private_setup, private_submit
from test_event_store import principal


def test_default_working_context_keeps_compact_state_and_recent_tail(
    tmp_path, monkeypatch
):
    from datetime import timedelta
    import json
    import test_cognition_pipeline as fixtures
    from test_agent_intellectual_state import proposal

    app, a, _b, events, _ledger, _gate, sdk, worker = private_setup(tmp_path)
    try:
        sdk.change = proposal()
        private_submit(worker, a, "learn", "SYNTHETIC_OLD_RAW_SOURCE")
        clock = app._conversations._clock()
        monkeypatch.setattr(fixtures, "NOW", clock + timedelta(seconds=1))
        for index in range(25):
            fixtures.append_event(
                events, principal("a"), f"filler-{index}", f"Later input {index}"
            )
        app._conversations._clock = lambda: clock + timedelta(minutes=5)
        sdk.change = None
        private_submit(worker, a, "recall", "Что тебя интересует?")
        data = json.loads(sdk.calls[-1]["input"])
        assert len(data["conversation"]) <= 13
        assert data["history_omitted"]
        assert (
            data["intellectual_state"]["states"][0]["fields"]["topic"]
            == "Русская архитектура"
        )
        assert "SYNTHETIC_OLD_RAW_SOURCE" not in sdk.calls[-1]["input"]
        history = app._conversations.history(principal("a"))
        assert sum(item.role == "participant" for item in history) == 27
        assert history[0].content == "SYNTHETIC_OLD_RAW_SOURCE"
    finally:
        worker.close()


def test_full_recent_tail_does_not_validate_unselected_old_replies(
    tmp_path, monkeypatch
):
    from app.cognition.conversation_context import MemoryConversationAssembler
    from app.core.agent_output_store import AgentOutputStore
    from app.core.context import ContextLimits
    from app.core.store import EventStore
    from test_cognition_pipeline import append_event

    store = EventStore(tmp_path / "events.db")
    identifiers = [
        append_event(store, principal("a"), f"msg-{i}", f"Input {i}") for i in range(20)
    ]
    read = AgentOutputStore._read
    checked = []

    def count(self, db, who, operation):
        checked.append(operation)
        return read(self, db, who, operation)

    monkeypatch.setattr(AgentOutputStore, "_read", count)
    with store._open() as db:
        request, sources = MemoryConversationAssembler(
            store, ContextLimits(32_000, 3)
        ).build(principal("a"), identifiers[-1], db)
    assert len(checked) == 2
    assert [x.content for x in request.conversation.conversation] == [
        "Input 17",
        "Input 18",
        "Input 19",
    ]
    assert request.conversation.history_omitted
    assert set(sources) == set(identifiers[-3:])


def test_ordinary_history_does_not_reassemble_all_sources_for_each_reply(
    tmp_path, monkeypatch
):
    app, a, _b, _events, _ledger, _gate, _sdk, worker = private_setup(tmp_path)
    try:
        for index in range(3):
            assert (
                private_submit(worker, a, str(index), "Продолжим разговор.")[
                    "model_state"
                ]
                == "completed"
            )

        def unrelated_archive(*_args, **_kwargs):
            raise AssertionError(
                "ordinary history must not reassemble all private/public sources"
            )

        monkeypatch.setattr(
            app._conversations._intellectual, "sources", unrelated_archive
        )
        history = app._conversations.history(principal("a"))
        assert sum(item.role == "agent" for item in history) == 3
    finally:
        worker.close()
