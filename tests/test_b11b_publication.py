"""Publication contracts exercised through the durable service and runtime."""

from collections import deque
from urllib.parse import parse_qs, urlsplit

import pytest

from app.threads.domain import OutboxState
from app.threads.service import PolicyDeniedError
from app.threads.store import ThreadsStoreError
from test_int21_threads_transport import (
    NOW,
    FakeHTTP,
    details,
    ready,
    response,
    service,
)


class RecordingHTTP(FakeHTTP):
    def __init__(self, *responses, profiles=()):
        super().__init__(*responses)
        self.profiles = deque(profiles)
        self.sequence = []

    def request(self, method, url, headers, body, timeout):
        parsed = urlsplit(url)
        self.sequence.append((method, parsed.path, parse_qs(parsed.query)))
        if parsed.path == "/v1.0/me" and self.profiles:
            value = self.profiles.popleft()
            if isinstance(value, Exception):
                raise value
            return value
        return super().request(method, url, headers, body, timeout)


def readback(parent):
    fields = "id,is_reply_owned_by_me,replied_to" if parent else "id,owner,replied_to"
    account = [("GET", "/v1.0/me", {"fields": ["id"]})] if parent else []
    return account + [("GET", "/v1.0/post-1", {"fields": [fields]})]


def dispatch_sequence(parent):
    create = {"media_type": ["TEXT"], "text": ["Проверяем публикацию"]}
    if parent:
        create["reply_to_id"] = [parent]
    return [
        ("GET", "/v1.0/me", {"fields": ["id"]}),
        ("POST", "/v1.0/user-1/threads", create),
        ("GET", "/v1.0/me", {"fields": ["id"]}),
        ("POST", "/v1.0/user-1/threads_publish", {"creation_id": ["container-1"]}),
    ] + readback(parent)


@pytest.mark.parametrize("parent", [None, "parent-1"])
def test_exact_draft_publication_and_account_parent_evidence(tmp_path, parent):
    fake = RecordingHTTP(
        response(200, {"id": "container-1"}),
        response(200, {"id": "post-1"}),
        response(200, details(parent)),
    )
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth, parent=parent)
    result = svc.dispatch("operation-1", approval, now=NOW)
    restarted, _ = service(tmp_path, fake, authorization=auth)
    assert restarted.store.get("operation-1") == result
    assert result.state is OutboxState.CONFIRMED
    assert result.provider_container_id == "container-1"
    assert result.provider_publication_id == "post-1"
    assert result.parent_message_id == parent
    assert result.attempt_count == 1
    assert fake.sequence == dispatch_sequence(parent)
    assert not fake.responses


@pytest.mark.parametrize("parent", [None, "parent-1"])
@pytest.mark.parametrize(
    "failure", [TimeoutError(), response(200, {}), response(503, {})]
)
def test_uncertain_readback_restarts_and_reconciles_original_without_post(
    tmp_path, parent, failure
):
    fake = RecordingHTTP(
        response(200, {"id": "container-1"}),
        response(200, {"id": "post-1"}),
        failure,
        response(200, details(parent)),
    )
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth, parent=parent)
    result = svc.dispatch("operation-1", approval, now=NOW)
    assert result.state is OutboxState.UNKNOWN
    assert result.provider_container_id == "container-1"
    assert result.provider_publication_id == "post-1"
    assert result.reconciliation_status == "required"
    restarted, _ = service(tmp_path, fake, authorization=auth)
    assert restarted.store.unresolved_for_account("account-1") == (result,)
    with pytest.raises(ThreadsStoreError, match="only a ready"):
        restarted.dispatch("operation-1", approval, now=NOW)
    reconciled = restarted.reconcile(
        "operation-1", correlation_id="reconcile-1", now=NOW
    )
    assert restarted.store.get("operation-1") == reconciled
    assert reconciled.state is OutboxState.CONFIRMED
    assert reconciled.provider_publication_id == result.provider_publication_id
    assert reconciled.attempt_count == 1
    assert reconciled.reconciliation_status == "verified_success"
    assert fake.sequence == dispatch_sequence(parent) + readback(parent)


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "other", "is_reply_owned_by_me": True, "replied_to": {"id": "parent-1"}},
        {
            "id": "post-1",
            "is_reply_owned_by_me": False,
            "replied_to": {"id": "parent-1"},
        },
        {"id": "post-1", "is_reply_owned_by_me": 1, "replied_to": {"id": "parent-1"}},
        {
            "id": "post-1",
            "is_reply_owned_by_me": "true",
            "replied_to": {"id": "parent-1"},
        },
        {"id": "post-1", "owner": {"id": "user-1"}, "replied_to": {"id": "parent-1"}},
        {"id": "post-1", "is_reply_owned_by_me": True, "replied_to": {"id": "other"}},
        {"id": "post-1", "is_reply_owned_by_me": True},
        {"id": "post-1", "is_reply_owned_by_me": True, "replied_to": {}},
        {"id": "post-1", "is_reply_owned_by_me": True, "replied_to": "parent-1"},
        {"id": "post-1", "is_reply_owned_by_me": True, "replied_to": {"id": "../bad"}},
    ],
)
def test_invalid_reply_evidence_remains_durably_unknown_after_restart(tmp_path, bad):
    fake = RecordingHTTP(
        response(200, {"id": "container-1"}),
        response(200, {"id": "post-1"}),
        response(200, bad),
        response(200, bad),
    )
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth, parent="parent-1")
    assert svc.dispatch("operation-1", approval, now=NOW).state is OutboxState.UNKNOWN
    restarted, _ = service(tmp_path, fake, authorization=auth)
    result = restarted.reconcile("operation-1", correlation_id="reconcile-1", now=NOW)
    assert result.state is OutboxState.UNKNOWN
    assert result.provider_publication_id == "post-1"
    assert result.reconciliation_status == "unresolved"
    assert restarted.store.unresolved_for_account("account-1") == (result,)
    assert fake.sequence == dispatch_sequence("parent-1") + readback("parent-1")


@pytest.mark.parametrize("profile", [{"id": "other"}, {}, {"id": 1}])
def test_reply_readback_account_mismatch_is_uncertain_and_stops_details(
    tmp_path, profile
):
    fake = RecordingHTTP(
        response(200, {"id": "container-1"}),
        response(200, {"id": "post-1"}),
        profiles=[response(200, {"id": "user-1"})] * 2 + [response(200, profile)] * 2,
    )
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth, parent="parent-1")
    result = svc.dispatch("operation-1", approval, now=NOW)
    assert result.state is OutboxState.UNKNOWN
    assert result.provider_publication_id == "post-1"
    assert result.provider_container_id == "container-1"
    restarted, _ = service(tmp_path, fake, authorization=auth)
    result = restarted.reconcile("operation-1", correlation_id="reconcile-1", now=NOW)
    assert result.state is OutboxState.UNKNOWN
    assert result.reconciliation_status == "unresolved"
    assert (
        fake.sequence == dispatch_sequence("parent-1")[:-1] + readback("parent-1")[:1]
    )


def test_reply_scope_revocation_blocks_before_any_http(tmp_path):
    fake = RecordingHTTP()
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth, parent="parent-1")
    svc.policy.scopes -= {"threads_manage_replies"}
    with pytest.raises(PolicyDeniedError, match="reply publication scope"):
        svc.dispatch("operation-1", approval, now=NOW)
    assert svc.store.get("operation-1").state is OutboxState.READY
    assert fake.sequence == []


@pytest.mark.parametrize(
    "profile_failure", [TimeoutError(), response(503, {}), response(200, {})]
)
def test_interrupted_reply_account_read_keeps_original_ids(tmp_path, profile_failure):
    fake = RecordingHTTP(
        response(200, {"id": "container-1"}),
        response(200, {"id": "post-1"}),
        response(200, details("parent-1")),
        profiles=[response(200, {"id": "user-1"})] * 2 + [profile_failure],
    )
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth, parent="parent-1")
    result = svc.dispatch("operation-1", approval, now=NOW)
    assert result.state is OutboxState.UNKNOWN
    assert result.provider_publication_id == "post-1"
    assert result.provider_container_id == "container-1"
    restarted, _ = service(tmp_path, fake, authorization=auth)
    assert (
        restarted.reconcile("operation-1", correlation_id="reconcile-1", now=NOW).state
        is OutboxState.CONFIRMED
    )
    assert fake.sequence == dispatch_sequence("parent-1")[:-1] + readback("parent-1")


@pytest.mark.parametrize("parent", [None, "parent-1"])
def test_lost_publish_response_has_no_sufficient_reconciliation_id(tmp_path, parent):
    fake = RecordingHTTP(response(200, {"id": "container-1"}), TimeoutError())
    svc, auth = service(tmp_path, fake)
    approval = ready(svc, auth, parent=parent)
    result = svc.dispatch("operation-1", approval, now=NOW)
    assert result.state is OutboxState.UNKNOWN
    assert result.provider_container_id == "container-1"
    assert result.provider_publication_id is None
    restarted, _ = service(tmp_path, fake, authorization=auth)
    assert (
        restarted.reconcile("operation-1", correlation_id="reconcile-1", now=NOW).state
        is OutboxState.UNKNOWN
    )
    assert fake.sequence == dispatch_sequence(parent)[:4]


@pytest.mark.parametrize("reply", [False, True])
@pytest.mark.parametrize(
    "evidence", ["valid", "wrong_owner", "wrong_parent", "malformed", "transient"]
)
def test_connected_runtime_restart_reconciles_only_original_publication(
    tmp_path, reply, evidence
):
    from datetime import timedelta
    from threading import Thread

    from app.threads.domain import InboundKind
    from test_int24_runtime import NOW as RUNTIME_NOW, run_once, setup

    proposal = (
        {"decision": "reply", "content": "Проверяем ответ на публичный вопрос."}
        if reply
        else {"decision": "propose_publication", "draft": "Проверяем публичный вывод."}
    )
    app, worker, sdk, http, _ = setup(tmp_path, proposal)
    try:
        if reply:
            app.poll(InboundKind.REPLY, correlation_id="poll-1")
            thread = Thread(target=worker.serve_once)
            thread.start()
            try:
                (operation,) = app.process_inbound(InboundKind.REPLY)
            finally:
                thread.join(timeout=5)
                assert not thread.is_alive()
        else:
            run_once(app, worker)
            operation = app.wakeup_operation("wake-1")
        draft = app.draft(operation)
        assert draft.parent_message_id == ("reply-1" if reply else None)
        app.approve(operation, expires_at=RUNTIME_NOW + timedelta(minutes=5))
        http.fail_readback = True
        result = app.dispatch(operation)
        assert result.state is OutboxState.UNKNOWN
        assert result.provider_publication_id == "post-1"
        assert result.provider_container_id == "container-1"
        assert len(sdk.calls) == 1
        sequence = [
            (m, urlsplit(u).path, parse_qs(urlsplit(u).query)) for m, u, _ in http.calls
        ]
        if reply:
            assert sequence.pop(0)[1] == "/v1.0/root-1/replies"
        expected = dispatch_sequence("reply-1" if reply else None)
        expected[1][2]["text"] = [draft.content]
        assert sequence == expected
    finally:
        worker.close()

    restarted, worker, sdk, http, _ = setup(tmp_path, proposal)
    try:
        assert restarted.draft(operation) == result
        with pytest.raises(ThreadsStoreError):
            restarted.dispatch(operation)
        http.parent = draft.parent_message_id
        value = details(http.parent)
        if evidence == "wrong_owner":
            if reply:
                value["is_reply_owned_by_me"] = False
            else:
                value["owner"] = {"id": "other"}
        elif evidence == "wrong_parent":
            value["replied_to"] = {"id": "other"}
        elif evidence == "malformed":
            value = {"id": "post-1"}
        elif evidence == "transient":
            http.fail_readback = True
        http.readback_value = value
        reconciled = restarted.reconcile(operation, "reconcile-restarted")
        assert reconciled.state is (
            OutboxState.CONFIRMED if evidence == "valid" else OutboxState.UNKNOWN
        )
        assert reconciled.provider_publication_id == result.provider_publication_id
        assert reconciled.provider_container_id == result.provider_container_id
        assert reconciled.attempt_count == 1
        assert restarted.service.store.get(operation) == reconciled
        assert sdk.calls == []
        assert [
            (m, urlsplit(u).path, parse_qs(urlsplit(u).query)) for m, u, _ in http.calls
        ] == readback(draft.parent_message_id)
        if evidence != "valid":
            assert restarted.service.store.unresolved_for_account("account-1") == (
                reconciled,
            )
    finally:
        worker.close()
