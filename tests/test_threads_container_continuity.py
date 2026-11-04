"""Interrupted unpublished containers cannot pause unrelated public work."""

from urllib.parse import parse_qs, urlsplit

import pytest

from app.integration.public_window import PublicOperatingWindow
from app.threads.domain import OutboxState
from app.threads.store import ThreadsStoreError
from app.threads.transport import HTTPResponse
from test_b11b_publication import RecordingHTTP
from test_int21_threads_transport import NOW, ready, response, service


@pytest.mark.parametrize("parent", [None, "parent-1"])
@pytest.mark.parametrize(
    "failure,evidence",
    [
        (TimeoutError(), "provider_transport_interrupted"),
        (response(503, {}), "provider_error_uncertain"),
        (HTTPResponse(200, b"not JSON"), "provider_response_malformed"),
        (response(200, {}), "container_id_missing"),
    ],
)
def test_unpublished_container_failure_is_terminal_without_retry(
    tmp_path, parent, failure, evidence
):
    http = RecordingHTTP(failure)
    svc, auth = service(tmp_path, http)
    approval = ready(svc, auth, parent=parent)
    result = svc.dispatch("operation-1", approval, now=NOW)
    assert result.state is OutboxState.FAILED_TERMINAL
    assert result.dispatch_evidence == evidence
    assert result.reconciliation_status == "not_required"
    assert result.attempt_count == 1
    assert result.provider_container_id is result.provider_publication_id is None
    assert not svc.store.unresolved_for_account("account-1")
    assert [(m, path) for m, path, _ in http.sequence] == [
        ("GET", "/v1.0/me"),
        ("POST", "/v1.0/user-1/threads"),
    ]
    assert http.sequence[-1][2]["auto_publish_text"] == ["false"]
    restarted, _ = service(tmp_path, http, authorization=auth)
    assert restarted.store.get("operation-1") == result
    with pytest.raises(ThreadsStoreError, match="only a ready"):
        restarted.dispatch("operation-1", approval, now=NOW)
    assert len(http.sequence) == 2


@pytest.mark.parametrize("phase", ["publish", "verification"])
def test_possible_publication_still_requires_original_operation_reconciliation(
    tmp_path, phase
):
    replies = [response(200, {"id": "container-1"})]
    if phase == "verification":
        replies.append(response(200, {"id": "post-1"}))
    http = RecordingHTTP(*replies, TimeoutError())
    svc, auth = service(tmp_path, http)
    approval = ready(svc, auth)
    item = svc.dispatch("operation-1", approval, now=NOW)
    assert item.state is OutboxState.UNKNOWN
    assert item.provider_container_id == "container-1"
    assert item.reconciliation_status == "required"
    assert svc.store.unresolved_for_account("account-1") == (item,)
    assert any(path.endswith("threads_publish") for _, path, _ in http.sequence)
    with pytest.raises(ThreadsStoreError):
        svc.dispatch("operation-1", approval, now=NOW)


def test_connected_finite_window_continues_after_container_timeout(tmp_path):
    from test_b11c_window import setup, spec
    from test_finite_public_grant import POST, grant, run_window, varied_posts

    app, worker, sdk, http, clock = setup(
        tmp_path, POST, autonomous_publication_enabled=True
    )
    window = spec(
        publication_grant="finite-public-grant-1",
        max_model_calls=2,
        max_poll_pages=0,
        max_inbound_events=0,
    )
    varied_posts(sdk)
    request = http.request
    failed = []

    def interrupt_first_container(method, url, *args):
        if method == "POST" and urlsplit(url).path.endswith("/threads") and not failed:
            failed.append(url)
            http.calls.append((method, url, args[1]))
            raise TimeoutError()
        return request(method, url, *args)

    http.request = interrupt_first_container
    try:
        app.public_grants.issue(grant(window), window)
        result = run_window(app, worker, clock, window, 2)
        assert result["state"] == "exhausted"
        assert len(sdk.calls) == 2
        assert app.operating_window is None
        first, second = PublicOperatingWindow(app, window).operations()
        assert app.service.store.get(first).state is OutboxState.FAILED_TERMINAL
        assert app.service.store.get(second).state is OutboxState.CONFIRMED
        assert not app.service.store.unresolved_for_account(app.account)
        creates = [
            parse_qs(urlsplit(url).query)
            for method, url, _ in http.calls
            if method == "POST" and urlsplit(url).path.endswith("/threads")
        ]
        assert len(creates) == 2
        assert all(params["auto_publish_text"] == ["false"] for params in creates)
        assert sum("threads_publish" in url for _, url, _ in http.calls) == 1
        usage = app.public_grants.summary(window.publication_grant)
        assert usage["publications_reserved"] == 2
        assert usage["http_requests_reserved"] == len(http.calls) == 7
    finally:
        worker.close()
