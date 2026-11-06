"""Completed POSTs carry only their independently authorized durable turn."""

import json

import pytest

from test_agent_intellectual_state import private_setup, private_submit


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


def test_completed_post_carries_its_turn_without_reading_full_history(
    tmp_path, monkeypatch
):
    app, a, b, _events, _ledger, _gate, sdk, server = private_setup(tmp_path)
    try:

        def forbidden_history(*_args):
            raise AssertionError("POST must not load the entire history")

        monkeypatch.setattr(app._conversations, "history", forbidden_history)
        result = private_submit(server, a, "fast-one", "PRIVATE_FAST_A")
        assert [row["role"] for row in result["turn"]] == ["participant", "agent"]
        assert [row["content"] for row in result["turn"]] == [
            "PRIVATE_FAST_A",
            result["response"],
        ]
        assert all(row["event_id"] and row["occurred_at"] for row in result["turn"])
        repeated = json.loads(a.submit("fast-one", "PRIVATE_FAST_A")[2])
        assert repeated["turn"] == result["turn"] and len(sdk.calls) == 1
        other = private_submit(server, b, "fast-one", "PRIVATE_FAST_B")
        assert "PRIVATE_FAST_A" not in json.dumps(other)
        assert other["turn"][0]["event_id"] != result["turn"][0]["event_id"]
    finally:
        server.close()


def test_stopped_post_does_not_attach_an_agent_turn(tmp_path):
    app, a, _b, _events, _ledger, _gate, sdk, server = private_setup(tmp_path)
    try:
        app._conversations._stop = lambda: True
        result = json.loads(a.submit("stopped", "Сохранённый вопрос.")[2])
        assert result["model_state"] != "completed"
        assert [row["role"] for row in result["turn"]] == ["participant"]
        assert sdk.calls == []
    finally:
        server.close()


def test_post_withholds_turn_metadata_if_the_original_source_cannot_be_read(
    tmp_path, monkeypatch
):
    _, a, _, _, _, _, sdk, server = private_setup(tmp_path)
    try:
        monkeypatch.setattr(
            "app.cognition.read_store.retrieve_exact_private_event", lambda *_args: None
        )
        result = private_submit(
            server,
            a,
            "missing-source",
            "Синтетический вопрос.",
            "503 Service Unavailable",
        )
        assert "turn" not in result and "response" not in result
        assert len(sdk.calls) == 1
    finally:
        server.close()


def test_a_page_bound_to_another_account_cannot_read_or_submit_in_the_new_session(
    tmp_path,
):
    _, _a, b, _, _, _, sdk, server = private_setup(tmp_path)
    try:
        private_submit(server, b, "b-only", "PRIVATE_NEW_ACCOUNT_B")
        wrong = {"HTTP_X_CHAT_ACCOUNT": "alice"}
        code, _, body = b.request("GET", "/api/history", extra_environ=wrong)
        assert code == "409 Conflict" and b"PRIVATE_NEW_ACCOUNT_B" not in body
        assert (
            b.submit("wrong-page", "Старый аккаунт страницы.", extra_environ=wrong)[0]
            == "409 Conflict"
        )
        assert len(sdk.calls) == 1
        assert (
            b.request(
                "GET", "/api/history", extra_environ={"HTTP_X_CHAT_ACCOUNT": "bob"}
            )[0]
            == "200 OK"
        )
    finally:
        server.close()
