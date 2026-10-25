"""Presentation regressions exercise the real security and privacy boundaries."""

from html.parser import HTMLParser
from urllib.parse import urlencode
import json

import pytest

from app.privacy.models import ConsentPurpose
from test_private_web_http import Browser, header_values, web as web_fixture
from test_participant_privacy_http import (
    acknowledge,
    grant_for,
    journey as journey_fixture,
    post,
    source_for,
)


@pytest.fixture
def web(tmp_path):
    return web_fixture.__wrapped__(tmp_path)


@pytest.fixture
def journey(tmp_path):
    return journey_fixture.__wrapped__(tmp_path)


class Elements(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.elements = []
        self.text = []
        self.feed(body.decode())

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def assert_document(body):
    dom = Elements(body)
    assert ("html", {"lang": "ru"}) in dom.elements
    assert any(
        tag == "meta" and "viewport-fit=cover" in attrs.get("content", "")
        for tag, attrs in dom.elements
    )
    assert any(
        tag == "link" and attrs.get("href") == "/assets/chat.css"
        for tag, attrs in dom.elements
    )
    assert not any(
        "style" in attrs or any(key.startswith("on") for key in attrs)
        for _, attrs in dom.elements
    )
    return dom


def test_login_failure_rotates_challenge_and_never_retains_password(web):
    browser = Browser(web[-1])
    browser.request("GET", "/login")
    prior = browser.cookies["ar_login_csrf"]
    password = "PASSWORD-CANARY-<>&"
    status, headers, body = browser.request(
        "POST",
        "/login",
        body=urlencode(
            {"username": "<alice>", "password": password, "csrf": prior}
        ).encode(),
        content_type="application/x-www-form-urlencoded",
    )
    assert status == "401 Unauthorized"
    dom = assert_document(body)
    assert "Неверное имя или пароль" in "".join(dom.text)
    assert password.encode() not in body
    assert any(
        tag == "input"
        and attrs.get("name") == "username"
        and attrs.get("value") == "<alice>"
        for tag, attrs in dom.elements
    )
    assert all(
        not attrs.get("value")
        for tag, attrs in dom.elements
        if tag == "input" and attrs.get("name") == "password"
    )
    assert browser.cookies["ar_login_csrf"] != prior
    assert header_values(headers, "Referrer-Policy") == ["same-origin"]
    status, _, body = browser.request(
        "POST",
        "/login",
        body=urlencode(
            {"username": "alice", "password": password, "csrf": prior}
        ).encode(),
        content_type="application/x-www-form-urlencoded",
    )
    assert status == "403 Forbidden"
    assert "Сессия входа устарела" in body.decode()
    assert browser.login("alice", "correct horse battery")[0] == "303 See Other"


@pytest.mark.parametrize("path", ["/missing", "/login"])
def test_html_error_shell_and_api_contract(web, path):
    browser = Browser(web[-1])
    status, _, body = browser.request("DELETE", path)
    assert status in {"404 Not Found", "405 Method Not Allowed"}
    assert_document(body)
    assert "Вернуться в чат" in body.decode()
    assert json.loads(browser.request("GET", "/api/missing")[2]) == {
        "error": "Not found."
    }


def test_privacy_previews_are_escaped_scoped_and_erased(journey):
    *_, a, b = journey
    acknowledge(a)
    acknowledge(b)
    ref = source_for(a, "<script>PREVIEW-A</script>")
    source_for(b, "PREVIEW-B-PRIVATE")
    post(a, "/api/privacy/grants", grant_for(ref))
    body = a.request("GET", "/privacy")[2]
    dom = assert_document(body)
    assert b"&lt;script&gt;PREVIEW-A&lt;/script&gt;" in body
    assert b"PREVIEW-B-PRIVATE" not in body
    assert ref not in "".join(dom.text)
    assert "consent:grant-1" not in "".join(dom.text)
    assert "Действует" in "".join(dom.text)
    assert "Подтверждено" in "".join(dom.text)
    assert "Подтвердить уведомление" not in "".join(dom.text)
    assert_document(
        a.request(
            "GET", "/privacy/source", extra_environ={"QUERY_STRING": f"ref={ref}"}
        )[2]
    )
    post(
        a,
        "/api/privacy/deletions",
        {"operation_id": "erase-preview", "source": ref, "confirm": "delete"},
    )
    body = a.request("GET", "/privacy")[2]
    assert b"PREVIEW-A" not in body
    assert "Заблокировано удалением" in body.decode()
    assert "Удалено" in body.decode()


@pytest.mark.parametrize(
    ("purpose", "label"),
    [
        (ConsentPurpose.IDENTIFIABLE_QUOTATION, "Дословно"),
        (ConsentPurpose.IDENTIFIABLE_DISCLOSURE, "С указанием автора"),
        (ConsentPurpose.SHARED_ABSTRACTION, "Обобщение"),
    ],
)
def test_privacy_renders_supported_grants_and_keeps_recovery_and_revocation(
    journey, monkeypatch, purpose, label
):
    *_, a, _b = journey
    acknowledge(a)
    source = source_for(a, "SUPPORTED-PURPOSE-PREVIEW")
    assert (
        post(a, "/api/privacy/grants", {**grant_for(source), "purpose": purpose})[0]
        == "201 Created"
    )
    status, _, body = a.request("GET", "/privacy")
    assert status == "200 OK"
    assert label in body.decode()
    assert "Отозвать" in body.decode()
    assert b"SUPPORTED-PURPOSE-PREVIEW" in body
    monkeypatch.setattr(a.app._privacy.gate, "_generation", None)
    status, _, body = a.request("GET", "/privacy")
    assert status == "200 OK"
    assert label in body.decode()
    assert "Повторить проверку" in body.decode()
    assert "Выйти" in body.decode()
    assert b"SUPPORTED-PURPOSE-PREVIEW" not in body
    assert (
        post(
            a,
            "/api/privacy/revocations",
            {"operation_id": "revoke-supported", "consent_id": "consent:grant-1"},
        )[0]
        == "201 Created"
    )
    status, _, body = a.request("GET", "/privacy")
    assert status == "200 OK"
    assert "Отозвано" in body.decode()


def test_onboarding_is_concise_and_keeps_complete_notice(journey):
    a = journey[-2]
    body = a.request("GET", "/privacy")[2]
    assert_document(body)
    assert "Прежде чем начать" in body.decode()
    assert "Понятно, к разговору" in body.decode()
    assert "оператор инфраструктуры" in body.decode()
    assert "<pre>" not in body.decode()
    assert "Мои сообщения" not in body.decode()


def test_preview_join_requires_matching_principal_on_payload_and_envelope(journey):
    *_, a, b = journey
    acknowledge(a)
    acknowledge(b)
    own = source_for(a, "OWN-SAFE-PREVIEW")
    foreign = source_for(b, "FOREIGN-SECRET-PREVIEW")
    post(a, "/api/privacy/grants", grant_for(own))
    ledger = a.app._privacy.ledger
    with ledger._open() as db:
        db.execute(
            "UPDATE consent_grants SET content_reference=? WHERE principal_id=?",
            (foreign, "participant-a"),
        )
    body = a.request("GET", "/privacy")[2]
    assert b"FOREIGN-SECRET-PREVIEW" not in body
    assert "Текст сообщения недоступен".encode() in body
    with a.app._privacy.store._open() as db:
        db.execute(
            "UPDATE private_payloads SET audience_principal_id=? WHERE payload_id=(SELECT payload_id FROM event_envelopes WHERE event_id=?)",
            ("participant-b", own),
        )
    body = a.request("GET", "/privacy")[2]
    assert b"OWN-SAFE-PREVIEW" not in body
    assert b"FOREIGN-SECRET-PREVIEW" not in body


def test_missing_acknowledgement_date_is_not_invented(journey, monkeypatch):
    a = journey[-2]
    monkeypatch.setattr(a.app._privacy, "acknowledged", lambda session: True)
    body = a.request("GET", "/privacy")[2].decode()
    assert "Подтверждено</span>" in body
    assert "Подтверждено 25" not in body


def test_revocation_failure_offers_recovery_and_logout_without_deletions(
    journey, monkeypatch
):
    _clock, auth, _store, _ledger, gate, _fake, _config, a, _b = journey
    acknowledge(a)
    source = source_for(a, "REVOCATION-BLOCKED-CONTENT")
    assert post(a, "/api/privacy/grants", grant_for(source))[0] == "201 Created"
    reconcile = gate.reconcile
    monkeypatch.setattr(gate, "reconcile", lambda: False)
    assert (
        post(
            a,
            "/api/privacy/revocations",
            {"operation_id": "blocked-revoke", "consent_id": "consent:grant-1"},
        )[0]
        == "201 Created"
    )
    assert json.loads(a.request("GET", "/api/privacy/status")[2])["deletions"] == []
    status, _, error = a.request("GET", "/chat")
    assert status == "503 Service Unavailable"
    assert b'href="/privacy"' in error
    body = a.request("GET", "/privacy")[2]
    assert "Повторить проверку".encode() in body
    assert b'id="privacy-logout"' in body
    assert b"REVOCATION-BLOCKED-CONTENT" not in body
    assert "удаление ещё не завершено".encode() not in body
    assert b'{"status":"blocked"}' == post(a, "/api/privacy/reconcile", {})[2]
    monkeypatch.setattr(gate, "reconcile", reconcile)
    assert b'{"status":"reconciled"}' == post(a, "/api/privacy/reconcile", {})[2]
    assert a.request("GET", "/chat")[0] == "200 OK"
    assert "Повторить проверку".encode() not in a.request("GET", "/privacy")[2]
    monkeypatch.setattr(gate, "_generation", None)
    token = a.cookies["ar_session"]
    assert (
        a.request("POST", "/logout", extra_environ={"HTTP_X_CSRF_TOKEN": "wrong"})[0]
        == "403 Forbidden"
    )
    assert auth.authenticate(token) is not None
    assert (
        a.request(
            "POST", "/logout", extra_environ={"HTTP_X_CSRF_TOKEN": a.cookies["ar_csrf"]}
        )[0]
        == "303 See Other"
    )
    assert auth.authenticate(token) is None
