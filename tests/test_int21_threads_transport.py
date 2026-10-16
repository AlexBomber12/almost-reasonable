"""Offline Graph contract tests through the durable Threads service."""

from collections import deque
from datetime import UTC, datetime, timedelta
import json
import sqlite3

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
from app.threads.transport import HTTPResponse, ThreadsHTTPTransport, _MAX_RESPONSE

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
    result = svc.poll(
        InboundKind.MENTION, now=NOW, correlation_id="poll-1", max_pages=1
    )
    assert result.pages == 1
    assert result.new_events[0].event_id == "mention-1"
    assert svc.store.cursor("account-1", InboundKind.MENTION).startswith("c-")
    assert len(svc.store.pending_inbound_events("account-1", InboundKind.MENTION)) == 1
    finished = svc.poll(InboundKind.MENTION, now=NOW, correlation_id="poll-2")
    assert finished.pages == 1
    assert svc.store.cursor("account-1", InboundKind.MENTION) is None
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
    assert "reverse=false" in fake.calls[0][1]


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
        HTTPResponse(200, b"x" * (_MAX_RESPONSE + 1)),
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
        {"data": [], "paging": {"next": "next-page", "cursors": {"after": "\ud800"}}},
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
    with pytest.raises(ValueError, match="cursor"):
        client.poll(InboundKind.REPLY, 17)
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


@pytest.mark.parametrize("activation", ["false", 1, None])
def test_live_activation_rejects_non_boolean_values(tmp_path, activation):
    fake = FakeHTTP()
    svc, _ = service(tmp_path, fake)
    with pytest.raises(ValueError, match="Boolean"):
        ThreadsService(
            ThreadsStore(tmp_path / "invalid-live.sqlite3"),
            transport(fake),
            svc.policy,
            live_enabled=activation,
        )
    assert fake.calls == []


def test_revoked_stage_prevents_live_publication_even_with_simulation_enabled(tmp_path):
    fake = FakeHTTP()
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth)
    svc.policy.stage = Stage.SIMULATION
    svc.policy.simulation_enabled = True
    with pytest.raises(PolicyDeniedError, match="requires Stage 3"):
        svc.dispatch("operation-1", approval, now=NOW)
    assert svc.store.get("operation-1").state is OutboxState.READY
    assert fake.calls == []


def test_rebinding_simulation_service_to_real_transport_cannot_poll(tmp_path):
    from dataclasses import replace

    from app.threads.simulator import ThreadsSimulator

    fake = FakeHTTP()
    live, _ = service(tmp_path, fake)
    simulated = ThreadsService(
        ThreadsStore(tmp_path / "simulation.sqlite3"),
        ThreadsSimulator(account_ref="account-1"),
        replace(live.policy, stage=Stage.SIMULATION, simulation_enabled=True),
    )
    simulated.simulator = transport(fake)
    with pytest.raises(PolicyDeniedError, match="binding changed"):
        simulated.poll(InboundKind.MENTION, now=NOW, correlation_id="poll-rebound")
    assert fake.calls == []


def test_rebinding_live_service_transport_cannot_dispatch(tmp_path):
    from app.threads.simulator import ThreadsSimulator

    fake = FakeHTTP()
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth)
    svc.simulator = ThreadsSimulator(account_ref="account-1")
    with pytest.raises(PolicyDeniedError, match="binding changed"):
        svc.dispatch("operation-1", approval, now=NOW)
    assert fake.calls == []


@pytest.mark.parametrize(
    "field,value",
    [("reply_thread_id", "other-root"), ("provider_user_id", "other-user")],
)
def test_live_provider_binding_change_prevents_poll(tmp_path, field, value):
    fake = FakeHTTP()
    svc, _ = service(tmp_path, fake)
    setattr(svc.simulator, field, value)
    with pytest.raises(PolicyDeniedError, match="binding changed"):
        svc.poll(InboundKind.REPLY, now=NOW, correlation_id="poll-rebound")
    assert fake.calls == []


def test_live_source_mutation_cannot_persist_forged_provenance(tmp_path):
    fake = FakeHTTP(
        response(
            200,
            {
                "data": [
                    {"id": "mention-1", "text": "Привет", "timestamp": NOW.isoformat()}
                ],
                "paging": {},
            },
        )
    )
    svc, _ = service(tmp_path, fake)
    svc.simulator.source_ref = "forged-live-source"
    with pytest.raises(PolicyDeniedError, match="binding changed"):
        svc.poll(InboundKind.MENTION, now=NOW, correlation_id="poll-forged-source")
    assert fake.calls == []
    assert svc.store.pending_inbound_events("account-1", InboundKind.MENTION) == ()


def test_real_transport_subclass_cannot_enter_non_live_service(tmp_path):
    from dataclasses import replace

    class DerivedTransport(ThreadsHTTPTransport):
        pass

    fake = FakeHTTP()
    live, _ = service(tmp_path, fake)
    derived = DerivedTransport(
        account_ref="account-1",
        provider_user_id="user-1",
        reply_thread_id="root-1",
        token=TOKEN,
        client=fake,
    )
    with pytest.raises(ValueError, match="exact ThreadsSimulator"):
        ThreadsService(
            ThreadsStore(tmp_path / "subclass.sqlite3"),
            derived,
            replace(live.policy, stage=Stage.SIMULATION, simulation_enabled=True),
        )
    assert fake.calls == []


def test_live_activation_cannot_be_disabled_after_construction(tmp_path):
    fake = FakeHTTP()
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth)
    svc.live_enabled = False
    svc.policy.stage = Stage.SIMULATION
    svc.policy.simulation_enabled = True
    with pytest.raises(PolicyDeniedError, match="binding changed"):
        svc.dispatch("operation-1", approval, now=NOW)
    assert fake.calls == []


@pytest.mark.parametrize("field", ["root_post", "replied_to"])
@pytest.mark.parametrize("value", ["bad", {}, {"id": "../bad"}, None])
def test_malformed_present_mention_relationship_keeps_cursor(tmp_path, field, value):
    row = {"id": "mention-1", "text": "Привет", "timestamp": NOW.isoformat()}
    row[field] = value
    fake = FakeHTTP(response(200, {"data": [row], "paging": {}}))
    svc, _ = service(tmp_path, fake)
    with pytest.raises(ProviderFailure, match="poll_page_invalid"):
        svc.poll(InboundKind.MENTION, now=NOW, correlation_id="poll-1")
    assert svc.store.cursor("account-1", InboundKind.MENTION) is None


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


@pytest.mark.parametrize("relation", ["bad", {}, {"id": "../bad"}])
def test_malformed_publication_parent_cannot_confirm_or_reconcile(tmp_path, relation):
    malformed = details()
    malformed["replied_to"] = relation
    fake = FakeHTTP(
        response(200, {"id": "container-1"}),
        response(200, {"id": "post-1"}),
        response(200, malformed),
        response(200, malformed),
    )
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth)
    item = svc.dispatch("operation-1", approval, now=NOW)
    assert (item.state, item.provider_publication_id) == (OutboxState.UNKNOWN, "post-1")
    assert (
        svc.reconcile("operation-1", correlation_id="reconcile-1", now=NOW).state
        is OutboxState.UNKNOWN
    )
    assert len(fake.calls) == 4


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


@pytest.mark.parametrize("fails", [False, True])
def test_http_adapter_releases_slot_before_result_is_visible(monkeypatch, fails):
    from queue import Queue

    from app.threads.transport import HTTPSClient

    class CheckingQueue(Queue):
        def put(self, item, *args, **kwargs):
            assert client._active_request.acquire(blocking=False)
            client._active_request.release()
            return super().put(item, *args, **kwargs)

    class Opener:
        def open(self, _req, timeout):
            if fails:
                raise OSError("connection failed")

            class Reply:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return None

                def read(self, _limit):
                    return b"{}"

            return Reply()

    monkeypatch.setattr("app.threads.transport.Queue", CheckingQueue)
    monkeypatch.setattr(
        "app.threads.transport.request.build_opener", lambda *_: Opener()
    )
    client = HTTPSClient()
    for _ in range(2):
        if fails:
            with pytest.raises(OSError, match="connection failed"):
                client.request("GET", "https://example.invalid", {}, None, 1)
        else:
            assert client.request(
                "GET", "https://example.invalid", {}, None, 1
            ) == HTTPResponse(200, b"{}")


def test_forward_reply_poll_reaches_new_arrivals_after_saved_cursor(tmp_path):
    def reply(name):
        return {
            "id": name,
            "text": "Привет",
            "timestamp": NOW.isoformat(),
            "root_post": {"id": "root-1"},
            "replied_to": {"id": "parent-1"},
            "is_reply_owned_by_me": False,
        }

    fake = FakeHTTP(
        response(
            200,
            {
                "data": [reply("reply-1")],
                "paging": {"next": "next-page", "cursors": {"after": "page-1"}},
            },
        ),
        response(
            200,
            {"data": [reply("reply-2")], "paging": {"cursors": {"after": "terminal"}}},
        ),
        response(
            200,
            {
                "data": [reply("reply-2"), reply("reply-3")],
                "paging": {"cursors": {"after": "terminal-2"}},
            },
        ),
    )
    svc, _ = service(tmp_path, fake)
    first = svc.poll(InboundKind.REPLY, now=NOW, correlation_id="poll-1", max_pages=1)
    assert [event.event_id for event in first.new_events] == ["reply-1"]
    assert first.final_cursor is not None
    second = svc.poll(InboundKind.REPLY, now=NOW, correlation_id="poll-2")
    assert [event.event_id for event in second.new_events] == ["reply-2"]
    third = svc.poll(InboundKind.REPLY, now=NOW, correlation_id="poll-3")
    assert [event.event_id for event in third.new_events] == ["reply-3"]
    assert third.duplicate_count == 1
    assert all("reverse=false" in call[1] for call in fake.calls)
    assert "after=page-1" in fake.calls[1][1] and "after=page-1" in fake.calls[2][1]


def test_long_opaque_provider_cursor_persists_and_resumes(tmp_path):
    raw_cursor = "opaque/+=!" * 30
    fake = FakeHTTP(
        response(
            200,
            {
                "data": [
                    {"id": "mention-1", "text": "Привет", "timestamp": NOW.isoformat()}
                ],
                "paging": {"next": "next-page", "cursors": {"after": raw_cursor}},
            },
        ),
        response(200, {"data": [], "paging": {}}),
    )
    svc, _ = service(tmp_path, fake)
    first = svc.poll(InboundKind.MENTION, now=NOW, correlation_id="poll-1", max_pages=1)
    assert len(first.final_cursor) > 128
    assert svc.store.cursor("account-1", InboundKind.MENTION) == first.final_cursor
    second = svc.poll(InboundKind.MENTION, now=NOW, correlation_id="poll-2")
    assert second.final_cursor is None
    assert "after=opaque%2F%2B%3D%21" in fake.calls[1][1]


def test_oversized_stored_cursor_rejected_before_http():
    from app.threads.transport import _MAX_CURSOR_LENGTH

    fake = FakeHTTP()
    with pytest.raises(ValueError, match="stored cursor"):
        transport(fake).poll(InboundKind.MENTION, "c-" + "A" * _MAX_CURSOR_LENGTH)
    assert fake.calls == []


@pytest.mark.parametrize("invalid_kind", ["oversized", "wrong_type"])
def test_store_rejects_invalid_bounded_cursor(tmp_path, invalid_kind):
    from app.threads.transport import _MAX_CURSOR_LENGTH

    store = ThreadsStore(tmp_path / "cursor.sqlite3")
    invalid_cursor = (
        "c-" + "A" * _MAX_CURSOR_LENGTH if invalid_kind == "oversized" else 17
    )
    with pytest.raises(ValueError, match="pagination cursor"):
        store.commit_poll_page(
            correlation_id="poll-1",
            account_ref="account-1",
            stream=InboundKind.MENTION,
            verified_source_ref="meta-threads-v1",
            expected_cursor=None,
            next_cursor=invalid_cursor,
            events=(),
            now=NOW,
            precommit_check=lambda: None,
        )


@pytest.mark.parametrize("reset,next_cursor", [(True, "cursor-1"), (1, None)])
def test_store_rejects_ambiguous_cursor_reset(tmp_path, reset, next_cursor):
    store = ThreadsStore(tmp_path / "cursor.sqlite3")
    with pytest.raises(ValueError, match="cursor reset"):
        store.commit_poll_page(
            correlation_id="poll-1",
            account_ref="account-1",
            stream=InboundKind.MENTION,
            verified_source_ref="meta-threads-v1",
            expected_cursor=None,
            next_cursor=next_cursor,
            reset_cursor=reset,
            events=(),
            now=NOW,
            precommit_check=lambda: None,
        )


@pytest.mark.parametrize("crash_stage", ["container", "publish"])
def test_crash_during_account_preflight_recovers_without_unknown(
    tmp_path, monkeypatch, crash_stage
):
    fake = FakeHTTP(response(200, {"id": "container-1"}))
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth)
    original = svc.simulator.verify_account
    calls = 0

    def interrupt_preflight():
        nonlocal calls
        calls += 1
        if crash_stage == "container" or calls == 2:
            raise SystemExit("simulated abrupt exit")
        original()

    monkeypatch.setattr(svc.simulator, "verify_account", interrupt_preflight)
    with pytest.raises(SystemExit):
        svc.dispatch("operation-1", approval, now=NOW)
    sending = svc.store.get("operation-1")
    assert sending.dispatch_evidence in {
        "pre_dispatch_intent",
        "container_created_publish_not_started",
    }
    restarted, _ = service(tmp_path, fake, authorization=auth)
    recovered = restarted.recover_sending("operation-1", now=NOW)
    assert recovered.state is OutboxState.READY
    assert recovered.provider_container_id == (
        "container-1" if crash_stage == "publish" else None
    )


def test_live_pages_stop_at_event_budget_and_resume_without_loss(tmp_path):
    def mention(number):
        return {
            "id": f"mention-{number}",
            "text": "Привет",
            "timestamp": NOW.isoformat(),
        }

    fake = FakeHTTP(
        response(
            200,
            {
                "data": [mention(i) for i in range(10)],
                "paging": {"next": "next-page", "cursors": {"after": "page-1"}},
            },
        ),
        response(
            200,
            {
                "data": [mention(10)],
                "paging": {},
            },
        ),
    )
    svc, _ = service(tmp_path, fake)
    first = svc.poll(
        InboundKind.MENTION, now=NOW, correlation_id="poll-1", max_events=10
    )
    assert (first.pages, len(first.new_events), len(fake.calls)) == (1, 10, 1)
    assert first.final_cursor is not None
    second = svc.poll(InboundKind.MENTION, now=NOW, correlation_id="poll-2")
    assert [event.event_id for event in second.new_events] == ["mention-10"]
    assert svc.store.cursor("account-1", InboundKind.MENTION) is None
    with sqlite3.connect(svc.store.database) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM threads_inbound_events"
            ).fetchone()[0]
            == 11
        )


def test_owned_reply_is_skipped_and_cursor_advances(tmp_path):
    def reply(name, owned):
        return {
            "id": name,
            "text": "Привет",
            "timestamp": NOW.isoformat(),
            "root_post": {"id": "root-1"},
            "replied_to": {"id": "parent-1"},
            "is_reply_owned_by_me": owned,
        }

    fake = FakeHTTP(
        response(
            200,
            {
                "data": [reply("owned-1", True)],
                "paging": {"next": "next-page", "cursors": {"after": "page-1"}},
            },
        ),
        response(200, {"data": [reply("reply-1", False)], "paging": {}}),
    )
    svc, _ = service(tmp_path, fake)
    first = svc.poll(InboundKind.REPLY, now=NOW, correlation_id="poll-1", max_pages=1)
    assert first.new_events == () and first.final_cursor is not None
    second = svc.poll(InboundKind.REPLY, now=NOW, correlation_id="poll-2")
    assert [event.event_id for event in second.new_events] == ["reply-1"]


@pytest.mark.parametrize("limit", [0, 11, True])
def test_live_page_limit_is_validated_before_http(limit):
    fake = FakeHTTP()
    with pytest.raises(ValueError, match="page limit"):
        transport(fake).poll(InboundKind.MENTION, None, limit=limit)
    assert fake.calls == []


def test_live_page_http_timeout_uses_remaining_poll_budget(tmp_path):
    class RecordingHTTP(FakeHTTP):
        def __init__(self):
            super().__init__(response(200, {"data": [], "paging": {}}))
            self.timeouts = []

        def request(self, method, url, headers, body, timeout):
            self.timeouts.append(timeout)
            return super().request(method, url, headers, body, timeout)

    fake = RecordingHTTP()
    svc, _ = service(tmp_path, fake)
    ticks = iter([0.0, 0.75] + [0.75] * 20)
    svc.clock = lambda: next(ticks)
    result = svc.poll(
        InboundKind.MENTION,
        now=NOW,
        correlation_id="poll-deadline",
        timeout_seconds=1.0,
    )
    assert result.pages == 1
    assert fake.timeouts == [0.25]


def test_live_poll_deadline_expiring_before_request_avoids_http(tmp_path):
    fake = FakeHTTP()
    svc, _ = service(tmp_path, fake)
    ticks = iter([0.0, 0.0, 1.0])
    svc.clock = lambda: next(ticks)
    from app.threads.service import PollingError

    with pytest.raises(PollingError, match="time limit"):
        svc.poll(
            InboundKind.MENTION,
            now=NOW,
            correlation_id="poll-expired",
            timeout_seconds=1.0,
        )
    assert fake.calls == []


@pytest.mark.parametrize("timeout", [0, -1, True, "1", 31])
def test_live_page_rejects_invalid_http_timeout_before_request(timeout):
    fake = FakeHTTP()
    with pytest.raises(ValueError, match="page timeout"):
        transport(fake).poll(InboundKind.MENTION, None, timeout=timeout)
    assert fake.calls == []


def test_provider_page_exceeding_requested_limit_keeps_cursor(tmp_path):
    rows = [
        {"id": f"mention-{i}", "text": "Привет", "timestamp": NOW.isoformat()}
        for i in range(2)
    ]
    fake = FakeHTTP(response(200, {"data": rows}))
    svc, _ = service(tmp_path, fake)
    with pytest.raises(ProviderFailure, match="poll_page_invalid"):
        svc.poll(InboundKind.MENTION, now=NOW, correlation_id="poll-1", max_events=1)
    assert "limit=1" in fake.calls[0][1]
    assert svc.store.cursor("account-1", InboundKind.MENTION) is None


def test_full_page_of_long_valid_text_fits_response_bound_and_advances(tmp_path):
    rows = [
        {
            "id": f"mention-{number}",
            "text": "я" * 8_000,
            "timestamp": NOW.isoformat(),
        }
        for number in range(10)
    ]
    first_page = response(
        200,
        {
            "data": rows,
            "paging": {"next": "next-page", "cursors": {"after": "page-1"}},
        },
    )
    assert 64_000 < len(first_page.body) < _MAX_RESPONSE
    fake = FakeHTTP(first_page, response(200, {"data": [], "paging": {}}))
    svc, _ = service(tmp_path, fake)
    first = svc.poll(
        InboundKind.MENTION, now=NOW, correlation_id="poll-1", max_events=10
    )
    assert len(first.new_events) == 10
    assert first.final_cursor is not None
    second = svc.poll(InboundKind.MENTION, now=NOW, correlation_id="poll-2")
    assert second.final_cursor is None
    assert "limit=10" in fake.calls[0][1]


@pytest.mark.parametrize("stream", [InboundKind.REPLY, InboundKind.MENTION])
def test_media_only_item_is_skipped_and_cursor_advances(tmp_path, stream):
    media = {
        "id": "media-1",
        "media_type": "IMAGE",
        "timestamp": NOW.isoformat(),
    }
    textual = {
        "id": "text-1",
        "text": "Привет",
        "timestamp": NOW.isoformat(),
    }
    if stream is InboundKind.REPLY:
        for row in (media, textual):
            row.update(
                root_post={"id": "root-1"},
                replied_to={"id": "parent-1"},
                is_reply_owned_by_me=False,
            )
    fake = FakeHTTP(
        response(
            200,
            {
                "data": [media, textual],
                "paging": {"next": "next-page", "cursors": {"after": "page-1"}},
            },
        ),
        response(200, {"data": [], "paging": {}}),
    )
    svc, _ = service(tmp_path, fake)
    first = svc.poll(stream, now=NOW, correlation_id="poll-1", max_pages=1)
    assert [event.event_id for event in first.new_events] == ["text-1"]
    assert first.final_cursor is not None
    second = svc.poll(stream, now=NOW, correlation_id="poll-2")
    assert second.new_events == ()
    assert len(fake.calls) == 2


@pytest.mark.parametrize("media_type", [None, "TEXT_POST", "UNRECOGNIZED"])
def test_missing_text_without_supported_media_type_fails_closed(tmp_path, media_type):
    row = {"id": "mention-1", "timestamp": NOW.isoformat()}
    if media_type is not None:
        row["media_type"] = media_type
    fake = FakeHTTP(response(200, {"data": [row], "paging": {}}))
    svc, _ = service(tmp_path, fake)
    with pytest.raises(ProviderFailure, match="poll_page_invalid"):
        svc.poll(InboundKind.MENTION, now=NOW, correlation_id="poll-1")
    assert svc.store.cursor("account-1", InboundKind.MENTION) is None


def test_stop_during_account_preflight_is_rechecked_before_write(tmp_path, monkeypatch):
    fake = FakeHTTP()
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth)
    original = svc.simulator.verify_account

    def stop_after_read():
        original()
        svc.policy.emergency_stop = True

    monkeypatch.setattr(svc.simulator, "verify_account", stop_after_read)
    with pytest.raises(PolicyDeniedError, match="emergency stop"):
        svc.dispatch("operation-1", approval, now=NOW)
    assert svc.store.get("operation-1").state is OutboxState.READY
    assert fake.calls == []
