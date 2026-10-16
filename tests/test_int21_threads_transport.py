"""Offline Graph contract tests through the durable Threads service."""

from collections import deque
from datetime import UTC, datetime, timedelta
import json

import pytest

from app.threads.domain import (
    AccountPolicy,
    InboundKind,
    OutboxState,
    PublicationType,
    SourceProvenance,
    Stage,
    issue_approval,
)
from app.threads.service import PolicyDeniedError, ThreadsService
from app.threads.simulator import (
    EffectClass,
    ProviderFailure,
    SimulationAuthorizationProvider,
)
from app.threads.store import ThreadsStore, ThreadsStoreError
from app.threads.transport import HTTPResponse, ThreadsHTTPTransport

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
TOKEN = "test-credential-do-not-log"


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.calls = []
        self.profile_id = "user-1"
        self.profile_reply = None

    def request(self, method, url, headers, body, timeout):
        assert TOKEN not in url
        assert url.startswith("https://graph.threads.net/v1.0/")
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert 0 < timeout <= 10
        if method == "GET" and "/me?fields=id" in url:
            return self.profile_reply or response(200, {"id": self.profile_id})
        self.calls.append((method, url, body))
        value = self.responses.popleft()
        if isinstance(value, Exception):
            raise value
        return value


def response(status, value):
    return HTTPResponse(status, json.dumps(value).encode())


def transport(fake):
    return ThreadsHTTPTransport(
        account_ref="account-1",
        provider_user_id="user-1",
        reply_thread_id="root-1",
        token=TOKEN,
        client=fake,
    )


def service(tmp_path, fake, *, authorization=None):
    policy = AccountPolicy(
        "account-1",
        Stage.STAGE_3,
        False,
        True,
        frozenset(
            {
                "threads_basic",
                "threads_content_publish",
                "threads_read_replies",
                "threads_manage_mentions",
            }
        ),
        True,
        True,
        True,
    )
    authorization = authorization or SimulationAuthorizationProvider()
    result = ThreadsService(
        ThreadsStore(tmp_path / "threads.sqlite3"),
        transport(fake),
        policy,
        utc_now=lambda: NOW,
        authorization_provider=authorization,
        live_enabled=True,
    )
    return result, authorization


def ready(svc, auth, *, operation_id="operation-1", parent=None):
    kind = PublicationType.REPLY if parent else PublicationType.POST
    svc.propose(
        operation_id=operation_id,
        publication_type=kind,
        draft_id="draft-1",
        content="Проверяем публикацию",
        source_provenance=SourceProvenance.PUBLIC,
        privacy_authorized=True,
        russian_output_authorized=True,
        correlation_id="correlation-1",
        now=NOW,
        parent_message_id=parent,
    )
    auth.set_privacy("account-1", operation_id, True)
    svc.submit_for_approval(operation_id, now=NOW)
    approval = issue_approval(
        reference="approval-1",
        account_ref="account-1",
        operation_id=operation_id,
        expires_at=NOW + timedelta(hours=1),
    )
    auth.set_approval(approval)
    svc.authorize(operation_id, approval, now=NOW)
    return approval


def details(parent=None):
    value = {"id": "post-1", "owner": {"id": "user-1"}}
    if parent:
        value["replied_to"] = {"id": parent}
    return value


def test_two_stage_post_verifies_real_identity_through_service(tmp_path):
    fake = FakeHTTP(
        response(200, {"id": "container-1"}),
        response(200, {"id": "post-1"}),
        response(200, details()),
    )
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth)
    item = svc.dispatch("operation-1", approval, now=NOW)
    assert (item.state, item.provider_container_id, item.provider_publication_id) == (
        OutboxState.CONFIRMED,
        "container-1",
        "post-1",
    )
    assert item.dispatch_evidence == "provider_publication_details_verified"
    assert "media_type=TEXT" in fake.calls[0][1]
    assert "creation_id=container-1" in fake.calls[1][1]
    assert fake.calls[2][0] == "GET"
    assert svc.store.get("operation-1") == item


def test_reply_parent_is_sent_and_verified(tmp_path):
    fake = FakeHTTP(
        response(200, {"id": "container-1"}),
        response(200, {"id": "post-1"}),
        response(200, details("parent-1")),
    )
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth, parent="parent-1")
    assert svc.dispatch("operation-1", approval, now=NOW).state is OutboxState.CONFIRMED
    assert "reply_to_id=parent-1" in fake.calls[0][1]


@pytest.mark.parametrize(
    "stage,responses,expected",
    [
        (
            "container",
            [response(403, {"error": {"code": 10}})],
            OutboxState.FAILED_TERMINAL,
        ),
        (
            "container",
            [response(400, {"error": {"code": 100}})],
            OutboxState.FAILED_TERMINAL,
        ),
        (
            "container",
            [response(429, {"error": {"code": 4}})],
            OutboxState.UNKNOWN,
        ),
        ("container", [TimeoutError("secret body")], OutboxState.UNKNOWN),
        ("container", [response(500, {"error": {"code": 1}})], OutboxState.UNKNOWN),
        ("container", [HTTPResponse(200, b'{"id":')], OutboxState.UNKNOWN),
        ("container", [response(200, {})], OutboxState.UNKNOWN),
        (
            "publish",
            [
                response(200, {"id": "container-1"}),
                response(401, {"error": {"code": 190}}),
            ],
            OutboxState.FAILED_TERMINAL,
        ),
        (
            "publish",
            [response(200, {"id": "container-1"}), response(500, {"error": {}})],
            OutboxState.UNKNOWN,
        ),
        (
            "publish",
            [response(200, {"id": "container-1"}), response(200, {})],
            OutboxState.UNKNOWN,
        ),
        (
            "verify",
            [
                response(200, {"id": "container-1"}),
                response(200, {"id": "post-1"}),
                response(200, {"id": "post-1", "owner": {"id": "other"}}),
            ],
            OutboxState.UNKNOWN,
        ),
        (
            "verify",
            [
                response(200, {"id": "container-1"}),
                response(200, {"id": "post-1"}),
                TimeoutError("lost"),
            ],
            OutboxState.UNKNOWN,
        ),
    ],
)
def test_failures_preserve_effect_class_and_never_resend(
    tmp_path, stage, responses, expected
):
    fake = FakeHTTP(*responses)
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth)
    item = svc.dispatch("operation-1", approval, now=NOW)
    assert item.state is expected
    assert TOKEN not in repr(item)
    if expected is OutboxState.UNKNOWN:
        writes_before = [call for call in fake.calls if call[0] == "POST"]
        restarted, _ = service(tmp_path, fake, authorization=auth)
        assert (
            restarted.reconcile(
                "operation-1", correlation_id="reconcile-1", now=NOW
            ).state
            is OutboxState.UNKNOWN
        )
        with pytest.raises(ThreadsStoreError):
            restarted.dispatch("operation-1", approval, now=NOW)
        assert [call for call in fake.calls if call[0] == "POST"] == writes_before


def test_disabled_real_transport_and_missing_current_approval(tmp_path):
    fake = FakeHTTP()
    with pytest.raises(ValueError, match="disabled"):
        ThreadsService(
            ThreadsStore(tmp_path / "blocked.sqlite3"),
            transport(fake),
            service(tmp_path, fake)[0].policy,
        )
    svc, _ = service(tmp_path, fake)
    svc.propose(
        operation_id="operation-1",
        publication_type=PublicationType.POST,
        draft_id="draft-1",
        content="Проверяем",
        source_provenance=SourceProvenance.PUBLIC,
        privacy_authorized=True,
        russian_output_authorized=True,
        correlation_id="correlation-1",
        now=NOW,
    )
    svc.submit_for_approval("operation-1", now=NOW)
    approval = issue_approval(
        reference="approval-1",
        account_ref="account-1",
        operation_id="operation-1",
        expires_at=NOW + timedelta(hours=1),
    )
    with pytest.raises(PolicyDeniedError):
        svc.authorize("operation-1", approval, now=NOW)
    assert fake.calls == []


def test_mentions_poll_persists_cursor_and_pending_event(tmp_path):
    row = {"id": "mention-1", "text": "Привет", "timestamp": NOW.isoformat()}
    fake = FakeHTTP(
        response(
            200,
            {
                "data": [row],
                "paging": {"next": "next-page", "cursors": {"after": "opaque/+="}},
            },
        ),
        response(200, {"data": [], "paging": {}}),
    )
    svc, _ = service(tmp_path, fake)
    result = svc.poll(InboundKind.MENTION, now=NOW, correlation_id="poll-1")
    assert result.pages == 2
    assert result.new_events[0].event_id == "mention-1"
    assert svc.store.cursor("account-1", InboundKind.MENTION).startswith("c-")
    assert len(svc.store.pending_inbound_events("account-1", InboundKind.MENTION)) == 1
    assert "after=opaque%2F%2B%3D" in fake.calls[1][1]


def test_reply_poll_rejects_wrong_root_without_advancing(tmp_path):
    row = {
        "id": "reply-1",
        "text": "Привет",
        "timestamp": NOW.isoformat(),
        "root_post": {"id": "other-root"},
        "replied_to": {"id": "parent-1"},
        "is_reply_owned_by_me": False,
    }
    fake = FakeHTTP(response(200, {"data": [row]}))
    svc, _ = service(tmp_path, fake)
    with pytest.raises(ProviderFailure) as failure:
        svc.poll(InboundKind.REPLY, now=NOW, correlation_id="poll-1")
    assert failure.value.effect_class is EffectClass.PROVEN_PRE_DISPATCH
    assert svc.store.cursor("account-1", InboundKind.REPLY) is None


def test_http_adapter_bounded_reads_and_redirect_suppression(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError
    from urllib.request import Request
    from app.threads.transport import HTTPSClient, _NoRedirect, _MAX_RESPONSE

    class Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, limit):
            assert limit == _MAX_RESPONSE + 1
            return b"{}"

    class Opener:
        def __init__(self):
            self.fail = False

        def open(self, req, timeout):
            assert isinstance(req, Request) and timeout == 2
            if self.fail:
                raise HTTPError(
                    req.full_url, 403, "forbidden", {}, BytesIO(b'{"error":{}}')
                )
            return Reply()

    opener = Opener()
    monkeypatch.setattr("app.threads.transport.request.build_opener", lambda *_: opener)
    client = HTTPSClient()
    assert client.request("GET", "https://example.invalid", {}, None, 2).status == 200
    opener.fail = True
    assert client.request("GET", "https://example.invalid", {}, None, 2).status == 403
    assert (
        _NoRedirect().redirect_request(
            None, None, 302, "redirect", {}, "https://bad.invalid"
        )
        is None
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"token": ""},
        {"token": "contains space"},
        {"timeout": 0},
        {"timeout": 11},
        {"provider_user_id": "../other"},
    ],
)
def test_transport_rejects_untrusted_configuration(changes):
    kwargs = dict(
        account_ref="account-1",
        provider_user_id="user-1",
        reply_thread_id="root-1",
        token=TOKEN,
        client=FakeHTTP(),
    )
    kwargs.update(changes)
    with pytest.raises(ValueError):
        ThreadsHTTPTransport(**kwargs)


@pytest.mark.parametrize(
    "bad_response",
    [
        HTTPResponse(200, b"{"),
        HTTPResponse(200, b"[]"),
        HTTPResponse(302, b"{}"),
        HTTPResponse(200, b"x" * 64001),
        HTTPResponse(200, b"\xff"),
        "wrong type",
    ],
)
def test_container_response_is_never_accepted_without_valid_id(tmp_path, bad_response):
    fake = FakeHTTP(bad_response)
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth)
    assert svc.dispatch("operation-1", approval, now=NOW).state is OutboxState.UNKNOWN
    assert TOKEN not in repr(svc.store.get("operation-1"))


def test_transport_rejects_foreign_outbound_items(tmp_path):
    fake = FakeHTTP()
    svc, auth = service(tmp_path, fake)
    ready(svc, auth)
    from dataclasses import replace

    foreign = replace(svc.store.get("operation-1"), account_ref="other")
    with pytest.raises(ValueError, match="account"):
        svc.simulator.create_container(foreign)
    with pytest.raises(ValueError, match="account"):
        svc.simulator.publish(foreign, "container-1")
    assert fake.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"data": "wrong"},
        {"data": [], "paging": []},
        {"data": [], "paging": {"cursors": []}},
        {"data": [], "paging": {"next": "next-page", "cursors": {"after": ""}}},
        {"data": [], "paging": {"next": 17, "cursors": {"after": "next"}}},
        {"data": [], "paging": {"next": "next-page"}},
        {"data": [17]},
        {
            "data": [
                {
                    "id": "mention-1",
                    "text": "Привет",
                    "timestamp": "2026-10-16T12:00:00",
                }
            ]
        },
        {"data": [{"id": "mention-1", "text": "Привет", "timestamp": "not-a-date"}]},
    ],
)
def test_malformed_mention_pages_do_not_advance_cursor(tmp_path, payload):
    fake = FakeHTTP(response(200, payload))
    svc, _ = service(tmp_path, fake)
    with pytest.raises(ProviderFailure):
        svc.poll(InboundKind.MENTION, now=NOW, correlation_id="poll-1")
    assert svc.store.cursor("account-1", InboundKind.MENTION) is None


def test_valid_reply_and_mention_parent_are_preserved(tmp_path):
    reply = {
        "id": "reply-1",
        "text": "Привет",
        "timestamp": NOW.isoformat(),
        "root_post": {"id": "root-1"},
        "replied_to": {"id": "parent-1"},
        "is_reply_owned_by_me": False,
    }
    mention = {
        "id": "mention-1",
        "text": "Привет",
        "timestamp": NOW.isoformat(),
        "root_post": {"id": "root-2"},
        "replied_to": {"id": "parent-2"},
    }
    fake = FakeHTTP(
        response(200, {"data": [reply]}), response(200, {"data": [mention]})
    )
    svc, _ = service(tmp_path, fake)
    assert (
        svc.poll(InboundKind.REPLY, now=NOW, correlation_id="poll-1")
        .new_events[0]
        .parent_message_id
        == "parent-1"
    )
    event = svc.poll(InboundKind.MENTION, now=NOW, correlation_id="poll-2").new_events[
        0
    ]
    assert (event.conversation_id, event.parent_message_id) == ("root-2", "parent-2")


def test_invalid_stored_cursor_and_stream_rejected_before_http():
    fake = FakeHTTP()
    client = transport(fake)
    with pytest.raises(ValueError, match="stream"):
        client.poll("reply", None)
    with pytest.raises(ValueError, match="cursor"):
        client.poll(InboundKind.REPLY, "../../other")
    with pytest.raises(ValueError, match="cursor"):
        client.poll(InboundKind.REPLY, "c-_w")
    assert fake.calls == []


def test_account_preflight_prevents_publication_to_wrong_profile(tmp_path):
    fake = FakeHTTP()
    fake.profile_id = "other-user"
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth)
    item = svc.dispatch("operation-1", approval, now=NOW)
    assert item.state is OutboxState.READY
    assert item.dispatch_evidence == "provider_account_mismatch"
    assert fake.calls == []


def test_explicit_live_binding_requires_real_transport_and_stage_3(tmp_path):
    from app.threads.simulator import ThreadsSimulator

    fake = FakeHTTP()
    svc, _ = service(tmp_path, fake)
    with pytest.raises(ValueError, match="Stage 3"):
        ThreadsService(
            ThreadsStore(tmp_path / "other.sqlite3"),
            ThreadsSimulator(account_ref="account-1"),
            svc.policy,
            live_enabled=True,
        )
    from dataclasses import replace

    with pytest.raises(ValueError, match="Stage 3"):
        ThreadsService(
            ThreadsStore(tmp_path / "third.sqlite3"),
            transport(fake),
            replace(svc.policy, stage=Stage.SIMULATION),
            live_enabled=True,
        )


def test_later_malformed_page_does_not_commit_earlier_events(tmp_path):
    row = {"id": "mention-1", "text": "Привет", "timestamp": NOW.isoformat()}
    fake = FakeHTTP(
        response(
            200,
            {
                "data": [row],
                "paging": {"next": "next-page", "cursors": {"after": "next"}},
            },
        ),
        HTTPResponse(200, b'{"data": ['),
    )
    svc, _ = service(tmp_path, fake)
    with pytest.raises(ProviderFailure):
        svc.poll(InboundKind.MENTION, now=NOW, correlation_id="poll-1")
    assert svc.store.cursor("account-1", InboundKind.MENTION) is None
    assert svc.store.pending_inbound_events("account-1", InboundKind.MENTION) == ()


def test_known_publication_id_reconciles_from_exact_provider_readback(tmp_path):
    fake = FakeHTTP(
        response(200, {"id": "container-1"}),
        response(200, {"id": "post-1"}),
        TimeoutError("readback lost"),
        response(200, details()),
    )
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth)
    uncertain = svc.dispatch("operation-1", approval, now=NOW)
    assert (uncertain.state, uncertain.provider_publication_id) == (
        OutboxState.UNKNOWN,
        "post-1",
    )
    restarted, _ = service(tmp_path, fake, authorization=auth)
    reconciled = restarted.reconcile(
        "operation-1", correlation_id="reconcile-1", now=NOW
    )
    assert (reconciled.state, reconciled.provider_publication_id) == (
        OutboxState.CONFIRMED,
        "post-1",
    )
    assert reconciled.reconciliation_status == "verified_success"
    assert len(fake.calls) == 4


def test_known_id_with_wrong_parent_remains_unresolved(tmp_path):
    fake = FakeHTTP(
        response(200, {"id": "container-1"}),
        response(200, {"id": "post-1"}),
        TimeoutError("readback lost"),
        response(200, details("wrong-parent")),
    )
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth)
    assert svc.dispatch("operation-1", approval, now=NOW).state is OutboxState.UNKNOWN
    assert (
        svc.reconcile("operation-1", correlation_id="reconcile-1", now=NOW).state
        is OutboxState.UNKNOWN
    )


def test_reconciliation_cannot_substitute_a_different_publication_id(
    tmp_path, monkeypatch
):
    from app.threads.transport import ConfirmedPublication

    fake = FakeHTTP(
        response(200, {"id": "container-1"}),
        response(200, {"id": "post-1"}),
        TimeoutError("readback lost"),
    )
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth)
    assert svc.dispatch("operation-1", approval, now=NOW).state is OutboxState.UNKNOWN
    monkeypatch.setattr(
        svc.simulator,
        "reconcile",
        lambda _item: ConfirmedPublication("other-post", "forged"),
    )
    item = svc.reconcile("operation-1", correlation_id="reconcile-1", now=NOW)
    assert item.state is OutboxState.UNKNOWN
    assert item.provider_publication_id == "post-1"


@pytest.mark.parametrize(
    "profile_reply",
    [
        response(429, {"error": {"code": 4}}),
        response(500, {"error": {"code": 1}}),
        HTTPResponse(200, b"{"),
    ],
)
def test_failed_account_preflight_has_proven_no_write(tmp_path, profile_reply):
    fake = FakeHTTP()
    fake.profile_reply = profile_reply
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth)
    item = svc.dispatch("operation-1", approval, now=NOW)
    assert item.state is OutboxState.READY
    assert item.provider_container_id is None
    assert fake.calls == []


def test_terminal_page_after_cursor_does_not_fetch_again(tmp_path):
    row = {"id": "mention-1", "text": "Привет", "timestamp": NOW.isoformat()}
    fake = FakeHTTP(
        response(200, {"data": [row], "paging": {"cursors": {"after": "terminal"}}})
    )
    svc, _ = service(tmp_path, fake)
    result = svc.poll(InboundKind.MENTION, now=NOW, correlation_id="poll-1")
    assert result.pages == 1
    assert result.final_cursor is None
    assert [event.event_id for event in result.new_events] == ["mention-1"]
    assert len(fake.calls) == 1


def test_publication_id_is_durable_before_readback_interruption(tmp_path, monkeypatch):
    from app.threads.simulator import SimulatedCrash

    fake = FakeHTTP(
        response(200, {"id": "container-1"}),
        response(200, {"id": "post-1"}),
        response(200, details()),
    )
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth)
    monkeypatch.setattr(
        svc.simulator,
        "verify_publication",
        lambda *_: (_ for _ in ()).throw(SimulatedCrash(transmission_possible=True)),
    )
    with pytest.raises(SimulatedCrash):
        svc.dispatch("operation-1", approval, now=NOW)
    sending = svc.store.get("operation-1")
    assert (sending.state, sending.provider_publication_id) == (
        OutboxState.SENDING,
        "post-1",
    )
    restarted, _ = service(tmp_path, fake, authorization=auth)
    assert (
        restarted.recover_sending("operation-1", now=NOW).state is OutboxState.UNKNOWN
    )
    reconciled = restarted.reconcile(
        "operation-1", correlation_id="reconcile-1", now=NOW
    )
    assert (reconciled.state, reconciled.provider_publication_id) == (
        OutboxState.CONFIRMED,
        "post-1",
    )
    assert len([call for call in fake.calls if call[0] == "POST"]) == 2


def test_sending_evidence_rejects_invalid_publication_id(tmp_path):
    fake = FakeHTTP()
    svc, auth = service(tmp_path, fake)
    ready(svc, auth)
    with pytest.raises(ValueError, match="publication ID"):
        svc.store.update_sending_evidence(
            "operation-1", "invalid_id", now=NOW, provider_publication_id="../bad"
        )


def test_http_adapter_enforces_total_deadline_and_one_inflight_request(monkeypatch):
    from threading import Event
    from app.threads.transport import HTTPSClient

    release = Event()
    finished = Event()

    class SlowOpener:
        def open(self, req, timeout):
            release.wait(1)
            finished.set()

            class Reply:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return None

                def read(self, _limit):
                    return b"{}"

            return Reply()

    monkeypatch.setattr(
        "app.threads.transport.request.build_opener", lambda *_: SlowOpener()
    )
    client = HTTPSClient()
    with pytest.raises(TimeoutError, match="deadline"):
        client.request("GET", "https://example.invalid", {}, None, 0.01)
    with pytest.raises(TimeoutError, match="still active"):
        client.request("GET", "https://example.invalid", {}, None, 0.01)
    release.set()
    assert finished.wait(1)


def test_http_adapter_worker_error_and_start_failure_are_bounded(monkeypatch):
    from app.threads.transport import HTTPSClient

    class FailingOpener:
        def open(self, _req, timeout):
            raise OSError("socket broke")

    monkeypatch.setattr(
        "app.threads.transport.request.build_opener", lambda *_: FailingOpener()
    )
    client = HTTPSClient()
    with pytest.raises(OSError, match="socket broke"):
        client.request("GET", "https://example.invalid", {}, None, 1)

    class BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("no thread")

    monkeypatch.setattr("app.threads.transport.Thread", BrokenThread)
    with pytest.raises(TimeoutError, match="could not start"):
        client.request("GET", "https://example.invalid", {}, None, 1)
    with pytest.raises(TimeoutError, match="could not start"):
        client.request("GET", "https://example.invalid", {}, None, 1)
