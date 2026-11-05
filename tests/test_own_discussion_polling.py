"""Offline Graph -> durable, account-bound discussion polling regressions."""

import sqlite3

import pytest

from app.threads.domain import InboundKind, OutboxState
from app.threads.service import ThreadsService, PolicyDeniedError
from app.threads.store import ThreadsStore
from app.threads.transport import ThreadsHTTPTransport
from test_int21_threads_transport import (
    FakeHTTP,
    NOW,
    TOKEN,
    ready,
    response,
    service,
)


def confirmed(svc, auth, http, root, operation):
    http.responses.extend(
        (
            response(200, {"id": "container-" + root}),
            response(200, {"id": root}),
            response(200, {"id": root, "owner": {"id": "user-1"}}),
        )
    )
    approval = ready(svc, auth, operation_id=operation)
    assert svc.dispatch(operation, approval, now=NOW).state is OutboxState.CONFIRMED


def reopened(svc, http, auth):
    return ThreadsService(
        ThreadsStore(svc.store.database),
        ThreadsHTTPTransport(
            account_ref="account-1",
            provider_user_id="user-1",
            reply_thread_id=None,
            token=TOKEN,
            client=http,
        ),
        svc.policy,
        utc_now=lambda: NOW,
        authorization_provider=auth,
        live_enabled=True,
    )


def comment(identity, root, parent=None, **changes):
    return (
        dict(
            id=identity,
            text="Содержательный встречный аргумент.",
            timestamp=NOW.isoformat(),
            root_post={"id": root},
            replied_to={"id": parent or root},
            is_reply_owned_by_me=False,
        )
        | changes
    )


def poll(svc, root, identity):
    return svc.poll(
        InboundKind.REPLY,
        reply_thread_id=root,
        now=NOW,
        correlation_id=identity,
        max_pages=1,
    )


def test_confirmed_own_posts_are_discovered_without_a_bootstrap_root(tmp_path):
    http = FakeHTTP()
    svc, auth = service(tmp_path, http)
    assert svc.store.next_reply_thread("account-1") is None
    confirmed(svc, auth, http, "own-one", "publish-one")
    confirmed(svc, auth, http, "own-two", "publish-two")
    svc = reopened(svc, http, auth)
    assert svc.store.next_reply_thread("account-1") == "own-one"
    http.responses.extend(
        (
            response(
                200,
                {
                    "data": [comment("first", "own-one")],
                    "paging": {"cursors": {"after": "cursor-one"}},
                },
            ),
            response(
                200,
                {
                    "data": [comment("second", "own-two")],
                    "paging": {"cursors": {"after": "cursor-two"}},
                },
            ),
            response(200, {"data": [comment("nested", "own-one", "first")]}),
        )
    )
    assert poll(svc, "own-one", "read-one").new_events[0].event_id == "first"
    assert svc.store.next_reply_thread("account-1") == "own-two"
    assert poll(svc, "own-two", "read-two").new_events[0].event_id == "second"
    svc = reopened(svc, http, auth)
    assert svc.store.next_reply_thread("account-1") == "own-one"
    result = poll(svc, "own-one", "continue-one")
    assert result.new_events[0].parent_message_id == "first"
    assert "/own-one/conversation?" in http.calls[-1][1]
    assert "after=cursor-one" in http.calls[-1][1]
    assert svc.store.cursor("account-1", InboundKind.REPLY) is None
    assert (
        svc.store.cursor("account-1", InboundKind.REPLY, reply_thread_id="own-two")
        != result.final_cursor
    )
    assert svc.store.next_reply_thread("other-account") is None


def test_a_failed_root_does_not_starve_the_next_owned_discussion(tmp_path):
    from app.threads.simulator import ProviderFailure

    http = FakeHTTP()
    svc, auth = service(tmp_path, http)
    confirmed(svc, auth, http, "own-one", "publish-one")
    confirmed(svc, auth, http, "own-two", "publish-two")
    http.responses.append(response(500, {}))
    with pytest.raises(ProviderFailure):
        poll(svc, "own-one", "failed-root")
    assert reopened(svc, http, auth).store.next_reply_thread("account-1") == "own-two"
    assert (
        svc.store.cursor("account-1", InboundKind.REPLY, reply_thread_id="own-one")
        is None
    )


def test_unconfirmed_foreign_or_changed_roots_cannot_choose_a_fetch_target(tmp_path):
    http = FakeHTTP()
    svc, auth = service(tmp_path, http)
    ready(svc, auth)
    for root in ("unpublished", "https://other.invalid", "root-1"):
        with pytest.raises((ValueError, PolicyDeniedError)):
            poll(svc, root, "denied-" + str(len(root)))
    assert not http.calls
    confirmed(svc, auth, http, "own-one", "published")
    with svc.store._open() as db:
        db.execute("UPDATE threads_outbox SET account_ref='other-account'")
    assert svc.store.next_reply_thread("account-1") is None
    with pytest.raises(PolicyDeniedError):
        poll(svc, "own-one", "foreign")


def test_discussion_migration_preserves_legacy_cursor_and_pending_events(tmp_path):
    svc, _ = service(tmp_path, FakeHTTP())
    with svc.store._open() as db:
        db.execute(
            "INSERT INTO threads_poll_cursors VALUES('account-1','reply','legacy',?)",
            (NOW.isoformat(),),
        )
        db.execute("DROP TABLE threads_discussion_polls")
        db.execute("UPDATE threads_schema_metadata SET version=10")
    restored = ThreadsStore(svc.store.database)
    assert restored.cursor("account-1", InboundKind.REPLY) == "legacy"
    assert restored.next_reply_thread("account-1") is None
    with sqlite3.connect(restored.database) as db:
        assert db.execute(
            "SELECT count(*) FROM threads_discussion_polls"
        ).fetchone() == (0,)


@pytest.mark.parametrize("changed", ["stop", "ownership"])
def test_controls_and_root_ownership_are_rechecked_before_page_commit(
    tmp_path, changed
):
    http = FakeHTTP()
    svc, auth = service(tmp_path, http)
    confirmed(svc, auth, http, "own-one", "published")
    http.responses.append(response(200, {"data": [comment("one", "own-one")]}))
    request = http.request

    def revoke(*args):
        page = request(*args)
        if changed == "stop":
            svc.policy.emergency_stop = True
        else:
            with svc.store._open() as db:
                db.execute("UPDATE threads_outbox SET dispatch_evidence=NULL")
        return page

    http.request = revoke
    with pytest.raises(PolicyDeniedError):
        poll(svc, "own-one", "revoked")
    assert (
        svc.store.cursor("account-1", InboundKind.REPLY, reply_thread_id="own-one")
        is None
    )
    with svc.store._open() as db:
        assert (
            db.execute("SELECT count(*) FROM threads_inbound_events").fetchone()[0] == 0
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"root_post": {"id": "foreign-root"}},
        {"replied_to": None},
        {"replied_to": {}},
        {"is_reply_owned_by_me": "false"},
    ],
)
def test_wrong_or_missing_conversation_relationships_do_not_advance_cursor(
    tmp_path, changes
):
    from app.threads.simulator import ProviderFailure

    http = FakeHTTP()
    svc, auth = service(tmp_path, http)
    confirmed(svc, auth, http, "own-one", "published")
    http.responses.append(
        response(200, {"data": [comment("one", "own-one", **changes)]})
    )
    with pytest.raises(ProviderFailure):
        poll(svc, "own-one", "wrong-relation")
    assert (
        svc.store.cursor("account-1", InboundKind.REPLY, reply_thread_id="own-one")
        is None
    )


def test_scoped_cursors_cannot_cross_roots_or_the_legacy_endpoint(tmp_path):
    http = FakeHTTP()
    svc, auth = service(tmp_path, http)
    confirmed(svc, auth, http, "own-one", "published")
    http.responses.append(
        response(200, {"data": [], "paging": {"cursors": {"after": "a"}}})
    )
    cursor = poll(svc, "own-one", "scope").final_cursor
    transport = svc.simulator
    assert transport.owns_cursor(InboundKind.REPLY, cursor, reply_thread_id="own-one")
    assert not transport.owns_cursor(
        InboundKind.REPLY, cursor, reply_thread_id="own-two"
    )
    assert not transport.owns_cursor(InboundKind.REPLY, cursor)
    for root, stream in (
        ("own-two", InboundKind.REPLY),
        ("own-one", InboundKind.MENTION),
    ):
        with pytest.raises(ValueError):
            transport.poll(stream, cursor, reply_thread_id=root)
    with pytest.raises(ValueError):
        svc.poll(
            InboundKind.MENTION,
            reply_thread_id="own-one",
            now=NOW,
            correlation_id="wrong-stream",
        )
    with pytest.raises(ValueError):
        svc.store.cursor("account-1", InboundKind.MENTION, reply_thread_id="own-one")
    from test_threads_polling import service as simulated_service

    simulated = simulated_service(tmp_path / "simulation")
    with pytest.raises(ValueError):
        poll(simulated, "own-one", "not-live")


def test_page_commit_requires_an_admitted_matching_discussion(tmp_path):
    from app.threads.domain import InboundEvent
    from app.threads.store import ThreadsStoreError

    svc, _ = service(tmp_path, FakeHTTP())
    args = dict(
        correlation_id="direct",
        account_ref="account-1",
        stream=InboundKind.REPLY,
        verified_source_ref=svc.simulator.source_ref,
        expected_cursor=None,
        next_cursor=None,
        now=NOW,
        precommit_check=lambda: None,
        reply_thread_id="own-one",
    )
    with pytest.raises(ThreadsStoreError, match="not admitted"):
        svc.store.commit_poll_page(**args, events=())
    svc.store.start_discussion_poll("account-1", "own-one", NOW)
    for root, parent in (("other-root", "parent"), ("own-one", None)):
        event = InboundEvent(
            "direct",
            "direct",
            "account-1",
            svc.simulator.source_ref,
            InboundKind.REPLY,
            root,
            parent,
            NOW,
            "Синтетический комментарий.",
        )
        with pytest.raises((ThreadsStoreError, ValueError)):
            svc.store.commit_poll_page(**args, events=(event,))
