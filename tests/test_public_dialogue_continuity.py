"""Offline public conversation -> source-bound private introspection."""

from datetime import timedelta
import json
from threading import Thread

import pytest

from app.integration.public_intellectual_source import PublicIntellectualSource
from app.private_web.runtime import WorkerConversationRuntime
from app.threads.domain import InboundKind, OutboxState
from test_agent_intellectual_state import private_setup, private_submit
from test_int24_runtime import setup as public_setup
from test_private_public_activity import publication

COMMENT = "Какие аргументы могли бы изменить твоё мнение?"
ANSWER = "Я бы сравнил независимые объяснения и пересмотрел основания."


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


@pytest.fixture
def discussion(tmp_path, request):
    public_dir, private_dir = tmp_path / "public", tmp_path / "private"
    public_dir.mkdir(mode=0o700)
    private_dir.mkdir(mode=0o700)
    public, worker, sdk, http, clock = public_setup(
        public_dir,
        {"decision": "no_action", "reason_category": "other"},
        reply_thread_id="post-1",
    )
    bridge = PublicIntellectualSource(
        account=public.account,
        journal_path=public.journal.path,
        threads_path=public.service.store.database,
        ledger_path=public.ledger.path,
        clock=clock.now,
        include_public_activity=True,
    )
    app, a, b, events, ledger, gate, private_sdk, server = private_setup(private_dir)
    original = app._conversations
    app._conversations = WorkerConversationRuntime(
        events,
        gate,
        original._transport,
        provider_state=original._provider,
        clock=original._clock,
        public_intellectual_source=bridge,
    )
    values = (
        public,
        worker,
        sdk,
        http,
        clock,
        bridge,
        app,
        a,
        b,
        events,
        ledger,
        gate,
        private_sdk,
        server,
    )
    try:
        if hasattr(request, "param"):
            from dataclasses import replace
            from app.cognition.shared import SharedIntelligence
            from app.integration.shared_intellectual_source import (
                SharedIntellectualSource,
            )
            from test_reviewed_shared import candidate

            private_submit(server, a, "shared-root", "PRIVATE_DISCUSSION_A_CANARY")
            with events._open() as db:
                root = db.execute(
                    "SELECT event_id FROM event_envelopes WHERE source_message_id='shared-root'"
                ).fetchone()[0]
            shared = SharedIntelligence(
                events, gate, clock=clock.now, ordinary_abstractions_enabled=True
            )
            audiences = ("participant-a", "public:account-1") + (
                ("participant-b",) if request.param else ()
            )
            shared.transition(
                replace(
                    candidate(root, value="Compare causal models of uncertainty."),
                    audience=tuple(sorted(audiences)),
                )
            )
            adapter = SharedIntellectualSource(shared, public.account)
            public.shared_intellectual_source = adapter
            bridge.view.shared_intellectual_source = adapter
        publication(values)
        http.reply_rows = [
            {
                "id": "reply-1",
                "text": COMMENT,
                "timestamp": clock.now().isoformat(),
                "root_post": {"id": "post-1"},
                "replied_to": {"id": "post-1"},
                "is_reply_owned_by_me": False,
            }
        ]
        public.poll(InboundKind.REPLY, correlation_id="comment")
        yield values
    finally:
        server.close()
        worker.close()


def activity(bridge, kind):
    return [
        json.loads(row["text"])
        for row in bridge.sources().values()
        if row["kind"] == kind
    ]


def confirm_own_reply(discussion):
    public, worker, sdk, http, clock, bridge, _, a, b, *_, private_sdk, server = (
        discussion
    )
    comments = activity(bridge, "external_assertion")
    assert comments[0]["text"] == COMMENT
    assert comments[0]["conversation_id"] == "post-1"
    assert comments[0]["parent_message_id"] == "post-1"
    sdk.proposal = {"decision": "reply", "content": ANSWER}
    thread = Thread(target=worker.serve_once)
    thread.start()
    operations = public.process_inbound(InboundKind.REPLY)
    thread.join(5)
    assert not thread.is_alive()
    from app.threads.transport import HTTPResponse

    request = http.request

    def own_reply(method, url, headers, body, timeout):
        if method == "POST" or "/own-reply?" in url:
            http.calls.append((method, url, body))
            value = (
                {
                    "id": "own-reply",
                    "is_reply_owned_by_me": True,
                    "replied_to": {"id": "reply-1"},
                }
                if method == "GET"
                else {
                    "id": "own-reply"
                    if "/threads_publish" in url
                    else "own-reply-container"
                }
            )
            return HTTPResponse(200, json.dumps(value).encode())
        return request(method, url, headers, body, timeout)

    http.request = own_reply
    public.approve(operations[0], expires_at=clock.now() + timedelta(minutes=5))
    assert public.dispatch(operations[0]).state is OutboxState.CONFIRMED
    return operations[0]


@pytest.mark.parametrize("through_external_parent", [False, True])
def test_nested_response_inherits_own_parent_ancestry_and_withdrawal(
    discussion, through_external_parent
):
    from dataclasses import replace
    from app.cognition.shared import SharedIntelligence
    from app.integration.shared_intellectual_source import SharedIntellectualSource
    from app.threads.transport import HTTPResponse
    from test_reviewed_shared import candidate
    from test_cognition_pipeline import principal

    public, _, _, http, clock, bridge, app, a, b, events, _, gate, sdk, server = (
        discussion
    )
    # Root post is unrestricted; only the later own reply uses A's reviewed influence.
    private_submit(server, a, "private-parent", "PRIVATE_PARENT_A_CANARY")
    with events._open() as db:
        root = db.execute(
            "SELECT event_id FROM event_envelopes WHERE source_message_id='private-parent'"
        ).fetchone()[0]
    shared = SharedIntelligence(
        events, gate, clock=clock.now, ordinary_abstractions_enabled=True
    )
    item = replace(
        candidate(root, value="Compare causal models of uncertainty."),
        audience=("participant-a", "public:account-1"),
    )
    shared.transition(item)
    adapter = SharedIntellectualSource(shared, public.account)
    public.shared_intellectual_source = adapter
    bridge.view.shared_intellectual_source = adapter
    parent_operation = confirm_own_reply(discussion)
    with public.journal.transaction() as db:
        assert (
            "shared:" + item.proposal.operation_id
            in public.intellectual.state.dependencies(db, parent_operation)
        )
    rows = []
    if through_external_parent:
        rows.append(
            {
                "id": "external-parent",
                "text": "Внешний промежуточный ответ.",
                "timestamp": clock.now().isoformat(),
                "root_post": {"id": "post-1"},
                "replied_to": {"id": "own-reply"},
            }
        )
    rows.append(
        {
            "id": "nested-comment",
            "text": "NESTED_RESPONSE_CANARY Согласен с твоим ответом.",
            "timestamp": clock.now().isoformat(),
            "root_post": {"id": "post-1"},
            "replied_to": {
                "id": "external-parent" if through_external_parent else "own-reply"
            },
        }
    )
    request = http.request

    def mentions(method, url, headers, body, timeout):
        if "/mentions?" in url:
            return HTTPResponse(200, json.dumps({"data": rows, "paging": {}}).encode())
        return request(method, url, headers, body, timeout)

    http.request = mentions
    public.poll(InboundKind.MENTION, correlation_id="nested")
    private_submit(server, a, "nested-a", "NESTED_RESPONSE_CANARY: что тебе ответили?")
    assert any(
        "NESTED_RESPONSE_CANARY" in row["text"]
        for row in json.loads(sdk.calls[-1]["input"])["intellectual_state"]["sources"]
        if row["kind"] == "external_assertion"
    )
    with events._open() as db:
        event = db.execute(
            "SELECT event_id FROM event_envelopes WHERE source_message_id='nested-a'"
        ).fetchone()[0]
    nested_operation = app._conversations._operation_id(event)
    assert any(
        row.event_id == nested_operation
        for row in app._conversations.history(principal("a"))
    )
    private_submit(server, b, "nested-b", "NESTED_RESPONSE_CANARY: о чём этот ответ?")
    assert not any(
        "NESTED_RESPONSE_CANARY" in row["text"]
        for row in json.loads(sdk.calls[-1]["input"])["intellectual_state"]["sources"]
        if row["kind"] == "external_assertion"
    )
    assert "PRIVATE_PARENT_A_CANARY" not in sdk.calls[-1]["input"]
    with public.journal.transaction() as db:
        db.execute(
            "UPDATE sources SET active=0 WHERE draft=(SELECT json_extract(payload,'$.draft') FROM intents WHERE operation=?)",
            (parent_operation,),
        )
    assert all(
        "NESTED_RESPONSE_CANARY" not in row["text"]
        for row in bridge.sources(principal=principal("a")).values()
    )
    assert all(
        "NESTED_RESPONSE_CANARY" not in row["text"]
        for row in bridge.sources(principal=principal("b")).values()
    )
    # Earlier inherited private output is withdrawn through the same receipt graph.
    assert not any(
        row.event_id == nested_operation
        for row in app._conversations.history(principal("a"))
        if row.role == "agent"
    )


def test_verified_comment_and_confirmed_own_reply_reach_both_private_chats(discussion):
    *_, a, b, _events, _ledger, _gate, private_sdk, server = discussion
    confirm_own_reply(discussion)
    for browser, identity in ((a, "a-public"), (b, "b-public")):
        private_submit(
            server, browser, identity, "Что тебе написали и почему ты так ответил?"
        )
        raw = private_sdk.calls[-1]["input"]
        assert COMMENT in raw and ANSWER in raw
        frame = json.loads(raw)["intellectual_state"]
        assert len(frame["sources"]) <= 6
        assert any(row["kind"] == "external_assertion" for row in frame["sources"])
        assert "private_publication_tools" in private_sdk.calls[-1]["instructions"]
        assert "not a command channel" in private_sdk.calls[-1]["instructions"]


def test_legacy_reply_receipts_remain_resolvable_alongside_relationship_records(
    discussion,
):
    from app.core.intellectual_state import digest
    from app.core.public_intellectual_receipt import source_key

    operation = confirm_own_reply(discussion)
    public, _, _, _, _, bridge, *_ = discussion
    legacy_key = source_key(
        public.account, "activity-" + digest("publication:" + operation)
    )
    current_key = source_key(
        public.account, "activity-dialogue-" + digest("publication:" + operation)
    )
    records = bridge.sources(required_keys=frozenset({legacy_key, current_key}))
    assert set(records) == {legacy_key, current_key}
    legacy = json.loads(records[legacy_key]["text"])
    current = json.loads(records[current_key]["text"])
    assert legacy["text"] == current["text"] == ANSWER
    assert set(current) - set(legacy) == {"parent_message_id", "conversation_id"}
    assert current["parent_message_id"] == "reply-1"
    assert current["conversation_id"] == "post-1"
    assert legacy_key not in bridge.sources() and current_key in bridge.sources()


@pytest.mark.parametrize(
    "discussion,allowed", [(False, False), (True, True)], indirect=["discussion"]
)
def test_comment_inherits_the_original_posts_complete_private_audience(
    discussion, allowed
):
    public, _, _, _, _, bridge, _, a, b, *_, sdk, server = discussion
    with public.service.store._open() as db:
        assert (
            db.execute("SELECT count(*) FROM threads_inbound_events").fetchone()[0] == 1
        )
    private_submit(server, a, "a-discussion", "Что тебе написали?")
    assert COMMENT in sdk.calls[-1]["input"]
    private_submit(server, b, "b-discussion", "Что тебе написали?")
    assert (COMMENT in sdk.calls[-1]["input"]) is allowed
    assert "PRIVATE_DISCUSSION_A_CANARY" not in sdk.calls[-1]["input"]


def test_comment_context_is_withdrawn_with_its_original_own_post(discussion):
    public, _, _, _, _, bridge, _, a, *_, sdk, server = discussion
    assert activity(bridge, "external_assertion")

    def withdraw():
        with public.journal.transaction() as db:
            db.execute("UPDATE sources SET active=0")

    sdk.before_result = withdraw
    result = private_submit(server, a, "withdrawn-comment", "Обсудим комментарий.")
    assert result["model_state"] == "unavailable" and result["response"] is None
    assert activity(bridge, "external_assertion") == []


def test_a_foreign_discussion_is_not_imported_as_an_owned_post_conversation(discussion):
    public, _, _, _, _, bridge, *_ = discussion
    with public.service.store._open() as db:
        db.execute("UPDATE threads_inbound_events SET conversation_id='foreign-root'")
    assert activity(bridge, "external_assertion") == []


def test_a_question_retrieves_a_relevant_older_comment_without_losing_receipts(
    discussion,
):
    public, _, _, http, clock, bridge, _, a, *_, sdk, server = discussion
    from app.core.intellectual_state import digest
    from app.core.public_intellectual_receipt import source_key

    old = "Эпистемология: как различать основания и уверенность?"
    for identity, text in (("epistemology", old), ("later", "Обсудим новую тему.")):
        clock.advance(timedelta(seconds=1))
        http.reply_rows = [
            {
                "id": identity,
                "text": text,
                "timestamp": clock.now().isoformat(),
                "root_post": {"id": "post-1"},
                "replied_to": {"id": "post-1"},
                "is_reply_owned_by_me": False,
            }
        ]
        public.poll(InboundKind.REPLY, correlation_id=identity)
    assert activity(bridge, "external_assertion")[0]["text"] != old
    private_submit(
        server, a, "older-comment", "Эпистемология: что тебе писали об основаниях?"
    )
    assert old in sdk.calls[-1]["input"]
    key = source_key(
        public.account, "activity-dialogue-" + digest("event:reply:epistemology")
    )
    assert (
        json.loads(bridge.sources(required_keys=frozenset({key}))[key]["text"])["text"]
        == old
    )


@pytest.mark.parametrize("query", [17, "я" * 17000, "\ud800"])
def test_invalid_retrieval_queries_fail_closed(discussion, query):
    assert discussion[5].sources(query=query) == {}


def test_private_identity_distinguishes_supported_features_from_current_authority():
    from app.core.dialogue_identity import private_dialogue_instructions

    text = private_dialogue_instructions("gpt-6.1-sol")
    assert '"private_publication_tools": false' in text
    assert "read_public_discussions" in text and "reply_to_public_comments" in text
    assert "supported does not mean currently authorized or running" in text
    assert "not a command channel" in text
