"""Offline connected journeys; real stores/IPC, synthetic SDK and Graph only."""

from datetime import timedelta
import json

import pytest

from app.integration.public_window import PublicOperatingWindow
from app.integration.public_intellectual_source import PublicIntellectualSource
from app.owner_control.public_runtime import PublicRuntimeReadView
from app.private_web.runtime import WorkerConversationRuntime
from app.threads.domain import OutboxState
from app.threads.transport import HTTPResponse
from test_agent_intellectual_state import private_setup, private_submit, proposal
from test_b11c_window import setup, spec, serve, finish, NO_ACTION
from test_finite_public_grant import grant
from test_public_experience import wake
from test_cognition_pipeline import principal

POST = "Мне интересно, какие аргументы помогают пересматривать гипотезы."
COMMENT = "Как отличить независимые подтверждения от повторения одной ошибки?"
ANSWER = "Я бы сначала проверил, не имеют ли подтверждения общего источника."
QUESTION = "Когда независимость источников действительно повышает уверенность?"
REASON = "Возражение показало, что мне важно различать независимость и количество."


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


def test_new_owned_discussion_reply_and_reason_reach_both_private_chats_after_restart(
    tmp_path,
):
    public_dir, private_dir = tmp_path / "public", tmp_path / "private"
    public_dir.mkdir(mode=0o700)
    private_dir.mkdir(mode=0o700)
    public, worker, sdk, http, clock = setup(
        public_dir,
        {"decision": "propose_publication", "draft": POST},
        reply_thread_id=None,
        autonomous_publication_enabled=True,
    )
    private, a, b, events, _, gate, private_sdk, private_worker = private_setup(
        private_dir
    )
    original = private._conversations

    def source():
        return PublicIntellectualSource(
            account=public.account,
            journal_path=public.journal.path,
            threads_path=public.service.store.database,
            ledger_path=public.ledger.path,
            mission_path=public_dir / "mission.json",
            owner_directory=public.owner.directory,
            include_public_activity=True,
            clock=clock.now,
        )

    def reconnect():
        private._conversations = WorkerConversationRuntime(
            events,
            gate,
            original._transport,
            provider_state=original._provider,
            clock=original._clock,
            public_intellectual_source=source(),
            emergency_stop=public.owner.active,
        )

    try:
        reconnect()
        private_submit(
            private_worker,
            a,
            "private-a",
            "PRIVATE_JOURNEY_A_CANARY Моя личная тестовая тема.",
        )
        wake(public, worker, clock, "new-owned-root")
        root_operation = public.wakeup_operation("new-owned-root")
        public.approve(root_operation, expires_at=clock.now() + timedelta(minutes=5))
        assert public.dispatch(root_operation).state is OutboxState.CONFIRMED
        assert public.service.store.next_reply_thread(public.account) == "post-1"
        assert public.simulator.reply_thread_id is None
        request = http.request

        def graph(method, url, headers, body, timeout):
            if "/post-1/conversation?" in url:
                http.calls.append((method, url, body))
                return HTTPResponse(
                    200,
                    json.dumps(
                        {
                            "data": [
                                {
                                    "id": "new-comment",
                                    "text": COMMENT,
                                    "timestamp": clock.now().isoformat(),
                                    "root_post": {"id": "post-1"},
                                    "replied_to": {"id": "post-1"},
                                    "is_reply_owned_by_me": False,
                                }
                            ],
                            "paging": {},
                        }
                    ).encode(),
                )
            if method == "POST" and "reply_to_id=new-comment" in url:
                value = {"id": "own-reply-container"}
            elif method == "POST" and "creation_id=own-reply-container" in url:
                value = {"id": "own-reply"}
            elif method == "GET" and "/own-reply?" in url:
                value = {
                    "id": "own-reply",
                    "is_reply_owned_by_me": True,
                    "replied_to": {"id": "new-comment"},
                }
            else:
                return request(method, url, headers, body, timeout)
            http.calls.append((method, url, body))
            return HTTPResponse(200, json.dumps(value).encode())

        http.request = graph
        create = sdk.create

        def choose(**kwargs):
            frame = json.loads(kwargs["input"][-1]["content"])
            if frame["trigger"] == "public_message":
                handle = next(
                    row["handle"]
                    for row in frame["intellectual_state"]["sources"]
                    if row["kind"] == "external_assertion" and COMMENT in row["text"]
                )
                change = proposal(
                    handle,
                    kind="unresolved_question",
                    fields={"question": QUESTION, "status": "open"},
                )
                change["reason"] = REASON
                sdk.proposal = {
                    "decision": "reply",
                    "content": ANSWER,
                    "intellectual": change,
                }
            else:
                sdk.proposal = NO_ACTION
            return create(**kwargs)

        sdk.create = choose
        window = spec(
            publication_grant="finite-public-grant-1",
            max_model_calls=3,
            max_poll_pages=2,
            max_inbound_events=1,
            poll_interval_seconds=1,
        )
        public.public_grants.issue(grant(window), window)
        thread = serve(worker, 3)
        outcome = PublicOperatingWindow(public, window).run(
            wait=lambda seconds: clock.advance(timedelta(seconds=seconds))
        )
        finish(thread)
        assert outcome["consumed"]["inbound_events"] == 1
        assert outcome["outcomes"]["reply"] == 1
        assert outcome["outcomes"]["no_action"] == 2
        assert (
            public.public_grants.summary(window.publication_grant)[
                "publications_reserved"
            ]
            == 1
        )
        assert any("/post-1/conversation?" in url for _, url, _ in http.calls)
        assert all(
            "PRIVATE_JOURNEY_A_CANARY" not in str(call["input"]) for call in sdk.calls
        )
        with public.service.store._open() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM threads_outbox WHERE state='confirmed' AND publication_type='reply'"
                ).fetchone()[0]
                == 1
            )
        # New objects read the same durable experience, not an in-memory bridge cache.
        reconnect()
        delivered = []
        for browser, identity in ((a, "discuss-a"), (b, "discuss-b")):
            result = private_submit(
                private_worker,
                browser,
                identity,
                "Что тебе возразили, как ты ответил и что тебя теперь интересует?",
            )
            raw = private_sdk.calls[-1]["input"]
            frame = json.loads(raw)["intellectual_state"]
            assert any(
                row["kind"] == "external_assertion" and COMMENT in row["text"]
                for row in frame["sources"]
            ), "Private frame kinds: " + repr([row["kind"] for row in frame["sources"]])
            assert ANSWER in raw and POST in raw and QUESTION in raw and REASON in raw
            assert result["model_state"] == "completed" and len(result["turn"]) == 2
            delivered.append(result["turn"][-1]["event_id"])
            if browser is b:
                assert "PRIVATE_JOURNEY_A_CANARY" not in raw
        observed = PublicRuntimeReadView(
            account=public.account,
            journal_path=public.journal.path,
            threads_path=public.service.store.database,
            ledger_path=public.ledger.path,
            owner_directory=public.owner.directory,
            scheduler_path=public.scheduler.repository.path,
        ).owner_status()
        assert (
            observed["available"] and observed["development"]["recorded_revisions"] == 1
        )
        assert (
            observed["discussions"]["streams"]["reply"]["last_attempt"]["status"]
            == "committed"
        )
        assert "PRIVATE_JOURNEY_A_CANARY" not in json.dumps(observed)
        with public.journal.transaction() as db:
            db.execute(
                "UPDATE sources SET active=0 WHERE draft=(SELECT json_extract(payload,'$.draft') FROM intents WHERE operation=?)",
                (root_operation,),
            )
        for participant, reply_id in zip(
            (principal("a"), principal("b")), delivered, strict=True
        ):
            assert not any(
                row.event_id == reply_id
                for row in private._conversations.history(participant)
            )
        assert any(
            "PRIVATE_JOURNEY_A_CANARY" in row.content
            for row in private._conversations.history(principal("a"))
        )
    finally:
        private_worker.close()
        worker.close()
