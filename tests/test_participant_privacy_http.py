"""Connected synthetic participant privacy controls through authenticated WSGI."""

from datetime import timedelta
from dataclasses import replace
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from app.core.store import EventStore
from app.cognition.shared import SharedIntelligence
from app.behavior.mission import ConfirmationOutcome, Mission, MissionPurpose
from app.behavior.shared_mission import (
    SharedMissionAdapter,
    SyntheticMissionConfirmationApproval,
)
from app.cognition.contracts import CognitionCapabilities, ExtractorIdentity
from app.cognition.generic_store import GenericMemoryStore
from app.cognition.pipeline import CognitionEngine
from app.cognition.synthetic import SyntheticMemoryExtractor
from app.core.self_state_store import SelfStateStore
from app.private_web.auth import AuthStore
from app.private_web.runtime import Stage0ConversationRuntime
from app.private_web.wsgi import PrivateWebApplication, PrivateWebConfig
from app.private_web.privacy import ParticipantPrivacyControls
from app.privacy.application import PrivacyReadGate, EventStoreRestoreAdapter
from app.privacy.ledger import PrivacyLedger
from app.privacy.models import (
    ConsentGrant,
    ConsentPurpose,
    ContentCategory,
    DeletionDisposition,
    DeletionRequest,
)
from app.privacy.deletion import DeletionCoordinator
from app.privacy.application import EventStoreErasureAdapter
from app.privacy.models import ErasureReport
from app.privacy.recovery import create_sqlite_backup, QuarantinedRestore
from app.trusted.openai_transport import FakeOpenAITransport
from test_private_conversation_runtime import response
from test_private_memory_runtime import extractor
from test_shared_intelligence import approval
from test_behavior_mission import invariants
from test_generic_memory import proposal as generic_proposal
from test_private_web_http import Browser, Clock
from test_event_store import private_input


@pytest.fixture
def journey(tmp_path):
    clock = Clock()
    auth = AuthStore(tmp_path / "auth.db", clock=clock)
    auth.provision_account("alice", "synthetic-password-a", "participant-a")
    auth.provision_account("bob", "synthetic-password-b", "participant-b")
    store = EventStore(tmp_path / "events.db")
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, store)
    assert gate.reconcile()
    fake = FakeOpenAITransport([response() for _ in range(4)])
    runtime = Stage0ConversationRuntime(store, gate, fake, clock=clock)
    controls = ParticipantPrivacyControls(auth, store, ledger, gate, clock=clock)
    config = PrivateWebConfig(
        frozenset({"chat.test"}), frozenset({"https://chat.test"})
    )
    app = PrivateWebApplication(auth, runtime, config, privacy=controls)
    a, b = Browser(app), Browser(app)
    a.login("alice", "synthetic-password-a")
    b.login("bob", "synthetic-password-b")
    return clock, auth, store, ledger, gate, fake, config, a, b


def post(browser, path, payload, **kwargs):
    return browser.request(
        "POST",
        path,
        body=json.dumps(payload).encode(),
        content_type="application/json",
        extra_environ={"HTTP_X_CSRF_TOKEN": browser.cookies.get("ar_csrf", "")},
        **kwargs,
    )


def acknowledge(browser):
    return post(browser, "/api/privacy/notice", {"version": "privacy-notice-ru-v1"})


def test_notice_gate_acknowledgement_and_authenticated_restart(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    assert a.request("GET", "/privacy")[0] == "200 OK"
    notice = a.request("GET", "/privacy")[2]
    assert "OpenAI".encode() in notice and "Cloudflare".encode() in notice
    assert a.request("GET", "/chat")[0] == "303 See Other"
    assert a.submit("one", "Секрет А")[0] == "403 Forbidden"
    assert not fake.calls
    assert (
        post(a, "/api/privacy/notice", {"version": "obsolete"})[0] == "400 Bad Request"
    )
    assert acknowledge(a)[0] == "200 OK"
    assert a.submit("one", "Секрет А")[0] == "201 Created"
    assert b.request("GET", "/api/history")[0] == "403 Forbidden"
    reopened_auth = AuthStore(auth.database_path, clock=clock)
    reopened = PrivateWebApplication(
        reopened_auth,
        Stage0ConversationRuntime(store, gate, fake, clock=clock),
        config,
        privacy=ParticipantPrivacyControls(
            reopened_auth, store, ledger, gate, clock=clock
        ),
    )
    a.app = reopened
    assert a.request("GET", "/chat")[0] == "200 OK"


def test_exact_grant_revocation_and_deletion_with_isolation(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    acknowledge(b)
    assert a.submit("source", "<script>Секрет А</script>")[0] == "201 Created"
    source = json.loads(a.request("GET", "/api/history")[2])["messages"][0]["event_id"]
    query = {"QUERY_STRING": f"ref={source}"}
    assert (
        b.request("GET", "/privacy/source", extra_environ=query)[0] == "404 Not Found"
    )
    view = a.request("GET", "/privacy/source", extra_environ=query)[2]
    assert b"&lt;script&gt;" in view and b"<script>\xd0" not in view
    grant = {
        "operation_id": "grant-1",
        "source": source,
        "audience": "public",
        "purpose": "identifiable_quotation",
        "validity_hours": 1,
    }
    assert post(b, "/api/privacy/grants", grant)[0] == "404 Not Found"
    assert post(a, "/api/privacy/grants", grant)[0] == "201 Created"
    assert post(a, "/api/privacy/grants", grant)[0] == "200 OK"
    assert (
        post(a, "/api/privacy/grants", {**grant, "purpose": "identifiable_disclosure"})[
            0
        ]
        == "409 Conflict"
    )
    controls = a.app._privacy
    dispatched = []
    assert controls.consent.authorize(
        controls.context(auth.authenticate(a.cookies["ar_session"])),
        principal_id="participant-a",
        content_reference=source,
        audience="public",
        purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
        correlation_id="use-1",
        dispatch=lambda: dispatched.append("sent"),
    ).allowed
    assert dispatched == ["sent"]
    assert (
        post(
            a,
            "/api/privacy/revocations",
            {"operation_id": "revoke-1", "consent_id": "consent:grant-1"},
        )[0]
        == "201 Created"
    )
    assert not controls.consent.authorize(
        controls.context(auth.authenticate(a.cookies["ar_session"])),
        principal_id="participant-a",
        content_reference=source,
        audience="public",
        purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
        correlation_id="use-2",
        dispatch=lambda: dispatched.append("bad"),
    ).allowed
    assert (
        post(
            a,
            "/api/privacy/deletions",
            {"operation_id": "delete-1", "source": source, "confirm": "delete"},
        )[0]
        == "200 OK"
    )
    assert gate.reconcile()
    assert source not in json.dumps(json.loads(a.request("GET", "/api/history")[2]))
    assert b.request("GET", "/privacy")[0] == "200 OK"


def test_request_guards_and_pending_status_access(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    assert (
        post(
            a,
            "/api/privacy/notice",
            {"version": "privacy-notice-ru-v1", "principal_id": "participant-b"},
        )[0]
        == "400 Bad Request"
    )
    assert (
        post(
            a, "/api/privacy/notice", {"version": "privacy-notice-ru-v1"}, origin=False
        )[0]
        == "403 Forbidden"
    )
    gate._generation = None
    assert a.request("GET", "/privacy")[0] == "200 OK"
    assert a.request("GET", "/api/privacy/status")[0] == "200 OK"
    assert acknowledge(a)[0] == "200 OK"
    auth.revoke(a.cookies["ar_session"])
    assert a.request("GET", "/privacy")[0] == "401 Unauthorized"
    clock.timestamp += int(timedelta(hours=1).total_seconds())


def source_for(browser, text="Синтетический источник"):
    assert browser.submit("seed", text)[0] == "201 Created"
    return json.loads(browser.request("GET", "/api/history")[2])["messages"][0][
        "event_id"
    ]


def grant_for(source, operation_id="grant-1"):
    return {
        "operation_id": operation_id,
        "source": source,
        "audience": "public",
        "purpose": "identifiable_quotation",
        "validity_hours": 1,
    }


def test_named_recipient_without_identity_cannot_be_granted(journey):
    *_, a, b = journey
    acknowledge(a)
    source = source_for(a)
    assert (
        b'value="named-recipient"'
        not in a.request(
            "GET", "/privacy/source", extra_environ={"QUERY_STRING": f"ref={source}"}
        )[2]
    )
    assert (
        post(
            a,
            "/api/privacy/grants",
            grant_for(source) | {"audience": "named-recipient"},
        )[0]
        == "400 Bad Request"
    )


def test_expired_grant_can_be_renewed_but_revoked_scope_stays_blocked(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    source = source_for(a)
    first = grant_for(source, "grant-first")
    first["audience"] = "public"
    assert post(a, "/api/privacy/grants", first)[0] == "201 Created"
    assert (
        post(
            a,
            "/api/privacy/grants",
            grant_for(source, "too-early") | {"audience": "public"},
        )[0]
        == "409 Conflict"
    )
    clock.timestamp += 3600
    a.login("alice", "synthetic-password-a")
    renewed = grant_for(source, "grant-renewed") | {"audience": "public"}
    assert post(a, "/api/privacy/grants", renewed)[0] == "201 Created"
    with ledger._open() as db:
        assert db.execute(
            "SELECT revision FROM consent_grants WHERE principal_id=? AND content_reference=? "
            "ORDER BY revision",
            ("participant-a", source),
        ).fetchall() == [(1,), (2,)]
    result = a.app._privacy.consent.authorize(
        a.app._privacy.context(auth.authenticate(a.cookies["ar_session"])),
        principal_id="participant-a",
        content_reference=source,
        audience="public",
        purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
        correlation_id="renewed-use",
        dispatch=lambda: None,
    )
    assert result.allowed and result.consent_id == "consent:grant-renewed"
    assert (
        post(
            a,
            "/api/privacy/revocations",
            {"operation_id": "revoke-renewed", "consent_id": "consent:grant-renewed"},
        )[0]
        == "201 Created"
    )
    clock.timestamp += 3600
    a.login("alice", "synthetic-password-a")
    assert (
        post(
            a,
            "/api/privacy/grants",
            grant_for(source, "after-revoke") | {"audience": "public"},
        )[0]
        == "409 Conflict"
    )


def test_older_active_grant_remains_reachable_for_revocation(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    session = auth.authenticate(a.cookies["ar_session"])
    context = a.app._privacy.context(session)
    for number in range(21):
        op = f"older-{number:02d}"
        ledger.insert_consent(
            context,
            ConsentGrant(
                "consent:" + op,
                op,
                context.principal_id,
                "synthetic-source:" + op,
                "public",
                ConsentPurpose.IDENTIFIABLE_QUOTATION,
                clock() + timedelta(seconds=number),
                clock() + timedelta(days=7),
                context.actor_reference,
                1,
                "grant:" + op,
                context.authentication_event_id,
            ),
        )
    first = a.request("GET", "/privacy")[2]
    assert b"consent:older-00" not in first
    assert b"/privacy?before=" in first
    second = a.request(
        "GET", "/privacy", extra_environ={"QUERY_STRING": "before=consent:older-01"}
    )[2]
    assert b"consent:older-00" in second
    assert (
        post(
            a,
            "/api/privacy/revocations",
            {"operation_id": "revoke-older", "consent_id": "consent:older-00"},
        )[0]
        == "201 Created"
    )
    assert (
        a.request(
            "GET", "/privacy", extra_environ={"QUERY_STRING": "before=consent:foreign"}
        )[0]
        == "404 Not Found"
    )
    for query in (
        "before=bad%20id",
        "before=",
        "before=consent:older-01&before=consent:older-02",
        "unexpected=value",
        "invalid",
        "before=" + "x" * 513,
    ):
        assert (
            a.request("GET", "/privacy", extra_environ={"QUERY_STRING": query})[0]
            == "400 Bad Request"
        )


def test_old_source_beyond_history_limit_remains_selectable(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    session = auth.authenticate(a.cookies["ar_session"])
    for number in range(101):
        store.append_private_input(
            session.trusted_context,
            private_input(
                f"page-{number:03d}",
                f"Синтетический источник {number}",
                occurred_at=clock() + timedelta(seconds=number),
            ),
        )
    history = json.loads(a.request("GET", "/api/history")[2])["messages"]
    assert len(history) == 100
    assert all(item["event_id"] != "event-page-000" for item in history)
    page = a.request("GET", "/privacy")[2]
    for _ in range(6):
        if b"event-page-000" in page:
            break
        match = re.search(rb'/privacy\?source_before=([^"&]+)', page)
        assert match is not None
        page = a.request(
            "GET",
            "/privacy",
            extra_environ={"QUERY_STRING": "source_before=" + match[1].decode()},
        )[2]
    assert b"event-page-000" in page
    assert (
        a.request(
            "GET",
            "/privacy/source",
            extra_environ={"QUERY_STRING": "ref=event-page-000"},
        )[0]
        == "200 OK"
    )
    for query, expected in (
        ("source_before=event-foreign", "404 Not Found"),
        ("source_before=bad%20id", "400 Bad Request"),
        (
            "source_before=event-page-001&source_before=event-page-002",
            "400 Bad Request",
        ),
    ):
        assert (
            a.request("GET", "/privacy", extra_environ={"QUERY_STRING": query})[0]
            == expected
        )
    assert (
        post(
            a,
            "/api/privacy/deletions",
            {
                "operation_id": "delete-old-source",
                "source": "event-page-000",
                "confirm": "delete",
            },
        )[0]
        == "200 OK"
    )


@pytest.mark.parametrize(
    "mutation,status",
    [
        ({"audience": "arbitrary"}, "400 Bad Request"),
        ({"purpose": "publication"}, "400 Bad Request"),
        ({"validity_hours": 0}, "400 Bad Request"),
        ({"validity_hours": 999}, "400 Bad Request"),
        ({"validity_hours": True}, "400 Bad Request"),
        ({"audience": []}, "400 Bad Request"),
        ({"purpose": []}, "400 Bad Request"),
        ({"operation_id": "x" * 121}, "400 Bad Request"),
        ({"operation_id": "bad id"}, "400 Bad Request"),
        ({"source": "bad id"}, "400 Bad Request"),
        ({"principal_id": "participant-b"}, "400 Bad Request"),
    ],
)
def test_grant_rejects_untrusted_or_unbounded_fields(journey, mutation, status):
    *_, a, b = journey
    acknowledge(a)
    source = source_for(a)
    assert (
        post(a, "/api/privacy/grants", {**grant_for(source), **mutation})[0] == status
    )


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/privacy/notice", {"version": "privacy-notice-ru-v1"}),
        ("/api/privacy/grants", grant_for("source-missing")),
        (
            "/api/privacy/revocations",
            {"operation_id": "revoke", "consent_id": "consent:missing"},
        ),
        (
            "/api/privacy/deletions",
            {"operation_id": "delete", "source": "source-missing", "confirm": "delete"},
        ),
        ("/api/privacy/reconcile", {}),
    ],
)
def test_mutations_require_csrf_origin_and_session(journey, path, payload):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    raw = json.dumps(payload).encode()
    assert (
        a.request("POST", path, body=raw, content_type="application/json")[0]
        == "403 Forbidden"
    )
    assert post(a, path, payload, origin=False)[0] == "403 Forbidden"
    a.host = "hostile.test"
    assert post(a, path, payload)[0] == "400 Bad Request"
    a.host = "chat.test"
    auth.revoke(a.cookies["ar_session"])
    assert post(a, path, payload)[0] == "401 Unauthorized"


def test_unsupported_methods_media_and_malformed_requests(journey):
    *_, a, b = journey
    assert a.request("GET", "/api/privacy/notice")[0] == "405 Method Not Allowed"
    assert a.request("POST", "/privacy")[0] == "405 Method Not Allowed"
    assert (
        a.request(
            "GET", "/privacy/source", extra_environ={"QUERY_STRING": "ref=a&ref=b"}
        )[0]
        == "400 Bad Request"
    )
    assert (
        a.request("GET", "/privacy/source", extra_environ={"QUERY_STRING": "bad"})[0]
        == "400 Bad Request"
    )
    assert (
        a.request(
            "GET", "/privacy/source", extra_environ={"QUERY_STRING": "ref=bad%20id"}
        )[0]
        == "400 Bad Request"
    )
    csrf = {"HTTP_X_CSRF_TOKEN": a.cookies["ar_csrf"]}
    assert (
        a.request(
            "POST",
            "/api/privacy/notice",
            body=b"{}",
            content_type="text/plain",
            extra_environ=csrf,
        )[0]
        == "415 Unsupported Media Type"
    )
    for body in (
        b"not json",
        b'"text"',
        b'{"version":"a","version":"b"}',
        b'{"version":"a","extra":1}',
    ):
        assert (
            a.request(
                "POST",
                "/api/privacy/notice",
                body=body,
                content_type="application/json",
                extra_environ=csrf,
            )[0]
            == "400 Bad Request"
        )
    assert a.request("GET", "/assets/privacy.js")[0] == "200 OK"


def test_restart_retries_expiry_and_foreign_operation_ids(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    acknowledge(b)
    source = source_for(a)
    grant = grant_for(source)
    assert post(a, "/api/privacy/grants", grant)[0] == "201 Created"
    assert post(b, "/api/privacy/grants", grant)[0] == "404 Not Found"
    assert (
        post(a, "/api/privacy/grants", {**grant, "validity_hours": 24})[0]
        == "409 Conflict"
    )
    reopened_auth = AuthStore(auth.database_path, clock=clock)
    reopened_store = EventStore(store.database_path)
    reopened_ledger = PrivacyLedger(ledger.path)
    reopened_gate = PrivacyReadGate(reopened_ledger, reopened_store)
    assert reopened_gate.reconcile()
    reopened = PrivateWebApplication(
        reopened_auth,
        Stage0ConversationRuntime(reopened_store, reopened_gate, fake, clock=clock),
        config,
        privacy=ParticipantPrivacyControls(
            reopened_auth, reopened_store, reopened_ledger, reopened_gate, clock=clock
        ),
    )
    a.app = b.app = reopened
    a.login("alice", "synthetic-password-a")
    assert post(a, "/api/privacy/grants", grant)[0] == "200 OK"
    assert "Действует до срока".encode() in a.request("GET", "/privacy")[2]
    clock.timestamp += 3600
    a.login("alice", "synthetic-password-a")
    assert "Срок истёк".encode() in a.request("GET", "/privacy")[2]
    controls = reopened._privacy
    called = []
    result = controls.consent.authorize(
        controls.context(reopened_auth.authenticate(a.cookies["ar_session"])),
        principal_id="participant-a",
        content_reference=source,
        audience="public",
        purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
        correlation_id="after-expiry",
        dispatch=lambda: called.append("bad"),
    )
    assert result.reason == "expired" and called == []
    assert (
        post(
            a,
            "/api/privacy/revocations",
            {"operation_id": "rev-1", "consent_id": "consent:grant-1"},
        )[0]
        == "201 Created"
    )
    assert (
        post(
            a,
            "/api/privacy/revocations",
            {"operation_id": "rev-1", "consent_id": "consent:grant-1"},
        )[0]
        == "200 OK"
    )
    assert (
        post(
            b,
            "/api/privacy/revocations",
            {"operation_id": "rev-1", "consent_id": "consent:grant-1"},
        )[0]
        == "404 Not Found"
    )
    assert "Отозвано".encode() in a.request("GET", "/privacy")[2]


def test_interrupted_deletion_status_and_reconciliation(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    source = source_for(a)
    controls = a.app._privacy
    original = controls.deletion.adapter.erase
    controls.deletion.adapter.erase = lambda request: ErasureReport(
        False, False, frozenset(), ("synthetic_block",)
    )
    payload = {"operation_id": "delete-blocked", "source": source, "confirm": "delete"}
    status, _, body = post(a, "/api/privacy/deletions", payload)
    assert status == "200 OK" and json.loads(body)["status"] == "incomplete_blocked"
    assert a.request("GET", "/api/privacy/status")[0] == "200 OK"
    assert b"incomplete_blocked" in a.request("GET", "/api/privacy/status")[2]
    privacy_page = a.request("GET", "/privacy")
    assert privacy_page[0] == "200 OK"
    assert "Удаление не завершено".encode() in privacy_page[2]
    assert post(a, "/api/privacy/deletions", payload)[0] == "200 OK"
    real_reconcile = gate.reconcile
    gate.reconcile = lambda: False
    assert post(a, "/api/privacy/reconcile", {})[2] == b'{"status":"blocked"}'
    gate.reconcile = real_reconcile
    controls.deletion.adapter.erase = original
    assert post(a, "/api/privacy/reconcile", {})[2] == b'{"status":"reconciled"}'
    assert b'"status":"applied"' in a.request("GET", "/api/privacy/status")[2]
    assert "Локальное удаление завершено".encode() in a.request("GET", "/privacy")[2]
    assert a.request("GET", "/api/history")[0] == "200 OK"


def test_revocation_and_grant_status_remain_usable_during_blocked_deletion(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    source = source_for(a)
    assert post(a, "/api/privacy/grants", grant_for(source))[0] == "201 Created"
    controls = a.app._privacy
    controls.deletion.adapter.erase = lambda request: ErasureReport(
        False, False, frozenset(), ("synthetic_block",)
    )
    payload = {
        "operation_id": "delete-blocked-grant",
        "source": source,
        "confirm": "delete",
    }
    assert (
        json.loads(post(a, "/api/privacy/deletions", payload)[2])["status"]
        == "incomplete_blocked"
    )
    assert (
        "Использование заблокировано удалением".encode()
        in a.request("GET", "/privacy")[2]
    )
    gate.reconcile = lambda: False
    assert (
        post(
            a,
            "/api/privacy/revocations",
            {"operation_id": "revoke-during-pending", "consent_id": "consent:grant-1"},
        )[0]
        == "201 Created"
    )
    assert b"incomplete_blocked" in a.request("GET", "/api/privacy/status")[2]


def test_final_grant_boundary_rejects_revoked_session(journey, monkeypatch):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    source = source_for(a)
    original = ledger.insert_consent

    def revoked(context, grant, **kwargs):
        auth.revoke(a.cookies["ar_session"])
        return original(context, grant, **kwargs)

    monkeypatch.setattr(ledger, "insert_consent", revoked)
    assert post(a, "/api/privacy/grants", grant_for(source))[0] == "401 Unauthorized"
    assert ledger.consent_principal("consent:grant-1") is None


def test_grant_scope_and_deletion_intent_remove_current_use(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    source = source_for(a)
    grant = grant_for(source)
    assert post(a, "/api/privacy/grants", grant)[0] == "201 Created"
    controls = a.app._privacy
    context = controls.context(auth.authenticate(a.cookies["ar_session"]))
    for reference, audience, purpose in (
        (source, "named-recipient", ConsentPurpose.IDENTIFIABLE_QUOTATION),
        (source, "public", ConsentPurpose.IDENTIFIABLE_DISCLOSURE),
        ("web-event:other", "public", ConsentPurpose.IDENTIFIABLE_QUOTATION),
    ):
        outcome = controls.consent.authorize(
            context,
            principal_id="participant-a",
            content_reference=reference,
            audience=audience,
            purpose=purpose,
            correlation_id="mismatch:"
            + str(len(fake.calls))
            + ":"
            + audience
            + ":"
            + purpose,
            dispatch=lambda: pytest.fail("mismatched use dispatched"),
        )
        assert outcome.reason == "no_matching_consent"
    deletion = {"operation_id": "delete-current", "source": source, "confirm": "delete"}
    assert (
        json.loads(post(a, "/api/privacy/deletions", deletion)[2])["status"]
        == "applied"
    )
    assert (
        json.loads(post(a, "/api/privacy/deletions", deletion)[2])["status"]
        == "applied"
    )
    assert post(b, "/api/privacy/deletions", deletion)[0] == "404 Not Found"
    assert (
        post(a, "/api/privacy/deletions", {**deletion, "source": "web-event:other"})[0]
        == "409 Conflict"
    )
    assert post(a, "/api/privacy/grants", grant_for(source, "new-grant"))[0] in {
        "404 Not Found",
        "503 Service Unavailable",
    }
    outcome = controls.consent.authorize(
        context,
        principal_id="participant-a",
        content_reference=source,
        audience="public",
        purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
        correlation_id="after-deletion",
        dispatch=lambda: pytest.fail("deleted use dispatched"),
    )
    assert outcome.reason == "source_deleted"
    assert post(a, "/api/privacy/reconcile", {})[2] == b'{"status":"reconciled"}'
    assert (
        "Использование заблокировано удалением".encode()
        in a.request("GET", "/privacy")[2]
    )
    assert a.request("GET", "/api/history")[0] == "200 OK"
    assert a.request("GET", "/chat")[0] == "200 OK"
    assert "Управление источником".encode() not in a.request("GET", "/chat")[2]


def test_controls_remain_accessible_during_stop_and_spend_suspension(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    runtime = a.app._conversations
    runtime._stop = lambda: True
    runtime._provider.is_spend_suspended = lambda: True
    assert a.request("GET", "/privacy")[0] == "200 OK"
    assert acknowledge(a)[0] == "200 OK"
    assert a.request("GET", "/api/privacy/status")[0] == "200 OK"
    assert post(a, "/api/privacy/reconcile", {})[0] == "200 OK"


def test_notice_replay_and_no_implicit_consent(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    assert acknowledge(a)[0] == "200 OK"
    assert json.loads(acknowledge(a)[2])["status"] == "already_acknowledged"
    with ledger._open() as db:
        assert db.execute("SELECT count(*) FROM consent_grants").fetchone()[0] == 0
    assert post(a, "/api/privacy/notice", {"version": 12})[0] == "400 Bad Request"
    assert (
        post(a, "/api/privacy/notice", {"version": "old-version"})[0]
        == "400 Bad Request"
    )
    assert a.request("GET", "/privacy")[0] == "200 OK"
    assert a.request("GET", "/api/history")[0] == "200 OK"


def test_source_selection_and_logout_stay_available_when_notice_unacknowledged(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    source = source_for(a)
    with ledger._open() as db:
        db.execute(
            "DELETE FROM notice_acknowledgements WHERE principal_id=?",
            ("participant-a",),
        )
    assert a.request("GET", "/chat")[0] == "303 See Other"
    page = a.request("GET", "/privacy")[2]
    assert quote(source, safe="").encode() in page and "Мои источники".encode() in page
    assert "Выйти".encode() in page
    assert (
        a.request(
            "GET", "/privacy/source", extra_environ={"QUERY_STRING": f"ref={source}"}
        )[0]
        == "200 OK"
    )
    assert (
        a.request(
            "POST", "/logout", extra_environ={"HTTP_X_CSRF_TOKEN": a.cookies["ar_csrf"]}
        )[0]
        == "303 See Other"
    )
    assert a.request("GET", "/privacy")[0] == "401 Unauthorized"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_privacy_browser_reports_rejected_mutations_and_failed_logout(journey):
    a = journey[-2]
    status, _, script = a.request("GET", "/assets/privacy.js")
    assert status == "200 OK"
    harness = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync(0, 'utf8');

async function scenario(endpoint, response, expectedAlert, expectedLocation) {
  const listeners = {};
  const alerts = [];
  const form = {
    dataset: {endpoint},
    addEventListener(name, callback) { listeners.submit = callback; }
  };
  const logout = {
    addEventListener(name, callback) { listeners.logout = callback; }
  };
  const location = {href: '/privacy', reload() { this.href = 'reloaded'; }};
  const context = {
    document: {
      querySelectorAll() { return [form]; },
      querySelector() { return {dataset: {csrf: 'csrf'}}; },
      getElementById() { return logout; }
    },
    FormData: class { *[Symbol.iterator]() { yield ['version', 'v1']; } },
    crypto: {randomUUID() { return 'operation'; }},
    fetch: async () => response,
    alert(message) { alerts.push(message); },
    location
  };
  vm.runInNewContext(script, context);
  if (endpoint === '/logout') await listeners.logout();
  else await listeners.submit({preventDefault() {}});
  if (alerts.length !== (expectedAlert === undefined ? 0 : 1) ||
      (expectedAlert !== undefined && alerts[0] !== expectedAlert) ||
      location.href !== expectedLocation) {
    throw Error(JSON.stringify({endpoint, alerts, location: location.href}));
  }
}

(async () => {
  await scenario('/api/privacy/grants',
    {ok: false, json() { throw Error('HTML error body'); }},
    'Операция отклонена', '/privacy');
  await scenario('/api/privacy/revocations',
    {ok: false, json() { throw Error('HTML error body'); }},
    'Операция отклонена', '/privacy');
  await scenario('/api/privacy/deletions',
    {ok: false, json() { throw Error('HTML error body'); }},
    'Операция отклонена', '/privacy');
  await scenario('/logout', {ok: false},
    'Не удалось выйти. Сессия может быть активна.', '/privacy');
  await scenario('/logout', {ok: true},
    undefined, '/login');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        ["node", "-e", harness], input=script, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode()


def test_concurrent_same_grant_and_revocation_retry(journey, monkeypatch):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    source = source_for(a)
    controls = a.app._privacy
    real_grant = controls.consent.grant

    def commit_then_lose(context, grant, **kwargs):
        real_grant(context, grant, **kwargs)
        raise ValueError("synthetic lost response")

    monkeypatch.setattr(controls.consent, "grant", commit_then_lose)
    assert post(a, "/api/privacy/grants", grant_for(source))[0] == "200 OK"
    monkeypatch.setattr(controls.consent, "grant", real_grant)
    real_revoke = controls.consent.revoke

    def revoke_then_lose(context, **kwargs):
        real_revoke(context, **kwargs)
        raise ValueError("synthetic lost response")

    monkeypatch.setattr(controls.consent, "revoke", revoke_then_lose)
    payload = {"operation_id": "revoke-race", "consent_id": "consent:grant-1"}
    assert post(a, "/api/privacy/revocations", payload)[0] == "200 OK"
    assert post(a, "/api/privacy/revocations", payload)[0] == "200 OK"
    assert (
        post(a, "/api/privacy/revocations", {**payload, "consent_id": "consent:other"})[
            0
        ]
        == "409 Conflict"
    )


def test_concurrent_deletion_retry_and_final_source_check(journey, monkeypatch):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    source = source_for(a)
    controls = a.app._privacy
    real_delete = controls.deletion.delete

    def delete_then_lose(context, request, **kwargs):
        real_delete(context, request, **kwargs)
        raise ValueError("synthetic lost response")

    monkeypatch.setattr(controls.deletion, "delete", delete_then_lose)
    payload = {"operation_id": "delete-race", "source": source, "confirm": "delete"}
    assert (
        json.loads(post(a, "/api/privacy/deletions", payload)[2])["status"] == "applied"
    )
    assert post(a, "/api/privacy/deletions", payload)[0] == "200 OK"
    assert (
        post(a, "/api/privacy/deletions", {**payload, "source": "web-event:other"})[0]
        == "409 Conflict"
    )
    assert post(b, "/api/privacy/deletions", payload)[0] == "404 Not Found"


def test_final_deletion_boundary_rejects_revoked_session(journey, monkeypatch):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    source = source_for(a)
    real_insert = ledger.insert_deletion

    def revoke_before_intent(context, request, disposition, reason, **kwargs):
        auth.revoke(a.cookies["ar_session"])
        return real_insert(context, request, disposition, reason, **kwargs)

    monkeypatch.setattr(ledger, "insert_deletion", revoke_before_intent)
    assert (
        post(
            a,
            "/api/privacy/deletions",
            {"operation_id": "delete-denied", "source": source, "confirm": "delete"},
        )[0]
        == "401 Unauthorized"
    )
    with pytest.raises(KeyError):
        ledger.deletion("delete-denied")


@pytest.mark.parametrize("action", ["grant", "delete"])
def test_revocation_during_final_source_check_prevents_control_write(
    journey, monkeypatch, action
):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    source = source_for(a)
    controls = a.app._privacy
    original = controls._source_unfenced
    calls = 0

    def revoke_after_read(session, reference):
        nonlocal calls
        value = original(session, reference)
        calls += 1
        if calls == 2:
            auth.revoke(a.cookies["ar_session"])
        return value

    monkeypatch.setattr(controls, "_source_unfenced", revoke_after_read)
    if action == "grant":
        assert (
            post(a, "/api/privacy/grants", grant_for(source, "revoked-after-source"))[0]
            == "401 Unauthorized"
        )
        assert ledger.consent_principal("consent:revoked-after-source") is None
    else:
        assert (
            post(
                a,
                "/api/privacy/deletions",
                {
                    "operation_id": "revoked-after-source",
                    "source": source,
                    "confirm": "delete",
                },
            )[0]
            == "401 Unauthorized"
        )
        with pytest.raises(KeyError):
            ledger.deletion("revoked-after-source")
        with store._open() as db:
            assert db.execute(
                "SELECT count(*) FROM private_payloads WHERE content=?",
                ("Синтетический источник",),
            ).fetchone() == (1,)
    assert calls == 2


def test_final_notice_and_revocation_boundaries_reject_revoked_session(
    journey, monkeypatch
):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    real_ack = ledger.acknowledge_notice

    def revoke_before_notice(*args, **kwargs):
        auth.revoke(a.cookies["ar_session"])
        return real_ack(*args, **kwargs)

    monkeypatch.setattr(ledger, "acknowledge_notice", revoke_before_notice)
    assert acknowledge(a)[0] == "401 Unauthorized"
    assert not ledger.has_notice("participant-a", "privacy-notice-ru-v1")
    monkeypatch.setattr(ledger, "acknowledge_notice", real_ack)
    a.login("alice", "synthetic-password-a")
    acknowledge(a)
    source = source_for(a)
    post(a, "/api/privacy/grants", grant_for(source))
    real_revoke = ledger.insert_revocation

    def revoke_before_write(**kwargs):
        auth.revoke(a.cookies["ar_session"])
        return real_revoke(**kwargs)

    monkeypatch.setattr(ledger, "insert_revocation", revoke_before_write)
    assert (
        post(
            a,
            "/api/privacy/revocations",
            {"operation_id": "revoke-denied", "consent_id": "consent:grant-1"},
        )[0]
        == "401 Unauthorized"
    )


def test_http_deletion_cascades_memory_reply_and_old_backup(journey, tmp_path):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    runtime = Stage0ConversationRuntime(
        store, gate, fake, clock=clock, memory_extractor=extractor()
    )
    a.app = b.app = PrivateWebApplication(
        auth,
        runtime,
        config,
        privacy=ParticipantPrivacyControls(auth, store, ledger, gate, clock=clock),
    )
    acknowledge(a)
    acknowledge(b)
    source = source_for(a, "Астрономия интересна. CANARY-A-PRIVACY-13")
    assert source_for(b, "CANARY-B-PRIVACY-13")
    approved = approval(source)
    shared = SharedIntelligence(store, gate, approvals=(approved,), clock=clock)
    shared.commit(approved)
    runtime = Stage0ConversationRuntime(
        store,
        gate,
        fake,
        clock=clock,
        memory_extractor=extractor(),
        shared_intelligence=shared,
    )
    a.app = b.app = PrivateWebApplication(
        auth,
        runtime,
        config,
        privacy=ParticipantPrivacyControls(auth, store, ledger, gate, clock=clock),
    )
    assert b.submit("influenced", "Что стоит обсудить?")[0] == "201 Created"
    assert shared.context(auth.authenticate(b.cookies["ar_session"]).trusted_context)
    assert gate.retrieve_private_candidates(
        auth.authenticate(a.cookies["ar_session"]).trusted_context
    )
    backup = tmp_path / "before-delete.db"
    create_sqlite_backup(store.database_path, backup, 7, clock())
    payload = {"operation_id": "delete-backed", "source": source, "confirm": "delete"}
    assert (
        json.loads(post(a, "/api/privacy/deletions", payload)[2])["status"] == "applied"
    )
    assert post(a, "/api/privacy/reconcile", {})[2] == b'{"status":"reconciled"}'
    assert (
        gate.retrieve_private_candidates(
            auth.authenticate(a.cookies["ar_session"]).trusted_context
        )
        == ()
    )
    assert (
        shared.context(auth.authenticate(b.cookies["ar_session"]).trusted_context) == ()
    )
    assert "CANARY-A-PRIVACY-13" not in a.request("GET", "/api/history")[2].decode()
    assert "CANARY-B-PRIVACY-13" in b.request("GET", "/api/history")[2].decode()
    a.submit("later", "О чём поговорим дальше?")
    assert "CANARY-A-PRIVACY-13" not in fake.calls[-1].request.input_text
    quarantine = tmp_path / "quarantine.db"
    result = QuarantinedRestore(ledger, 7, EventStoreRestoreAdapter).restore(
        backup, quarantine, clock()
    )
    assert result.activation_eligible
    with sqlite3.connect(f"file:{quarantine}?immutable=1", uri=True) as db:
        dump = "\n".join(db.iterdump())
    assert "CANARY-A-PRIVACY-13" not in dump
    assert "CANARY-B-PRIVACY-13" in dump


def test_http_deletion_cascades_generic_contextual_descendants(journey, tmp_path):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    acknowledge(b)
    sources = []
    for index in range(1, 4):
        assert (
            a.submit(f"generic-{index}", f"CANARY-GENERIC-{index}-13")[0]
            == "201 Created"
        )
        sources.append(
            next(
                item["event_id"]
                for item in json.loads(a.request("GET", "/api/history")[2])["messages"]
                if item["content"] == f"CANARY-GENERIC-{index}-13"
            )
        )
    principal = auth.authenticate(a.cookies["ar_session"]).trusted_context
    capabilities = CognitionCapabilities(
        generic_memory_write=True, private_consolidation=True
    )
    for index, source in enumerate(sources, 1):
        parents = () if index == 1 else ((f"m{index - 1}", 1),)
        candidate = generic_proposal(
            source, f"m{index}", f"generic-op-{index}", parents=parents
        )
        candidate = replace(
            candidate,
            context=replace(candidate.context, proposed_at=clock(), review_at=clock()),
        )
        engine = CognitionEngine(
            store,
            SelfStateStore(store),
            extractor=SyntheticMemoryExtractor(
                lambda _request, value=candidate: value,
                ExtractorIdentity("synthetic", "v1"),
            ),
            capabilities=capabilities,
            clock=clock,
            authorize=gate.authorized,
        )
        assert engine.process(principal, source).status.value == "persisted"
    assert len(GenericMemoryStore(store).current(principal)) == 3
    backup = tmp_path / "generic-before.db"
    create_sqlite_backup(store.database_path, backup, 7, clock())
    payload = {
        "operation_id": "delete-generic",
        "source": sources[0],
        "confirm": "delete",
    }
    assert (
        json.loads(post(a, "/api/privacy/deletions", payload)[2])["status"] == "applied"
    )
    assert post(a, "/api/privacy/reconcile", {})[0] == "200 OK"
    assert GenericMemoryStore(store).current(principal) == ()
    quarantine = tmp_path / "generic-quarantine.db"
    assert (
        QuarantinedRestore(ledger, 7, EventStoreRestoreAdapter)
        .restore(backup, quarantine, clock())
        .activation_eligible
    )
    with sqlite3.connect(f"file:{quarantine}?immutable=1", uri=True) as db:
        dump = "\n".join(db.iterdump())
    assert "CANARY-GENERIC-1-13" not in dump
    assert "Synthetic memory m3" not in dump


def test_http_deletion_requires_mission_reassessment(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    acknowledge(b)
    source_a = source_for(a, "Миссия и сомнение. CANARY-MISSION-A-13")
    first = replace(
        approval(source_a, op="mission-shared-a", item="mission-a"),
        allow_mission_evidence=True,
    )
    clock.timestamp += 1
    shared = SharedIntelligence(store, gate, approvals=(first,), clock=clock)
    assert shared.commit(first) == "mission-shared-a"
    initial = Mission("mission-v1", "Обсуждать проверяемые идеи", clock(), None)
    confirmation = SyntheticMissionConfirmationApproval(
        "mission-proposal-13", "mission-shared-b", ConfirmationOutcome.CONFIRMED
    )
    mission = SharedMissionAdapter(
        store,
        gate,
        initial=initial,
        invariants=invariants(),
        clock=clock,
        confirmations=(confirmation,),
    )
    assert (
        mission.propose(
            operation_id="mission-proposal-13",
            shared_operation="mission-shared-a",
            parent_version="mission-v1",
            proposed_version="mission-v2",
            correlation_id="mission-correlation-13",
            purpose=MissionPurpose.REFLECTION,
            counterevidence_checked=True,
        ).status.value
        == "pending"
    )
    clock.timestamp += 1
    source_b = source_for(b, "Независимое наблюдение Б")
    second = replace(
        approval(
            source_b,
            op="mission-shared-b",
            item="mission-b",
            author="b",
            counter=(source_b,),
            value="Consider competing explanations of uncertainty.",
        ),
        allow_mission_evidence=True,
    )
    shared = SharedIntelligence(store, gate, approvals=(second,), clock=clock)
    assert shared.commit(second) == "mission-shared-b"
    assert (
        mission.confirm(
            confirmation,
            correlation_id="mission-confirm-13",
            counterevidence_checked=True,
        ).status.value
        == "confirmed"
    )
    assert mission.current_mission().version == "mission-v2"
    payload = {
        "operation_id": "delete-mission",
        "source": source_a,
        "confirm": "delete",
    }
    assert (
        json.loads(post(a, "/api/privacy/deletions", payload)[2])["status"] == "applied"
    )
    assert post(a, "/api/privacy/reconcile", {})[0] == "200 OK"
    with pytest.raises(ValueError, match="reassessment"):
        mission.current_mission()
    with store._open() as db:
        assert db.execute(
            "SELECT status,document FROM shared_mission_document"
        ).fetchone() == (
            "reassessment",
            None,
        )


def test_status_listing_is_bounded_and_principal_scoped_while_reads_fenced(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    context = a.app._privacy.context(auth.authenticate(a.cookies["ar_session"]))
    for index in range(25):
        request = DeletionRequest(
            f"bounded-{index:02d}",
            context.principal_id,
            f"synthetic-source-{index:02d}",
            ContentCategory.PRIVATE_PAYLOAD,
            clock() + timedelta(seconds=index),
            context.actor_reference,
            f"bounded-correlation-{index:02d}",
            context.authentication_event_id,
        )
        ledger.insert_deletion(
            context, request, DeletionDisposition.PENDING, "awaiting_erasure"
        )
    statuses = json.loads(a.request("GET", "/api/privacy/status")[2])["deletions"]
    assert len(statuses) == 20
    assert statuses[0]["operation_id"] == "bounded-24"
    assert statuses[-1]["operation_id"] == "bounded-05"
    assert json.loads(b.request("GET", "/api/privacy/status")[2])["deletions"] == []
    page = a.request("GET", "/privacy")[2]
    assert b"bounded-24" in page and b"bounded-00" not in page


def test_dependency_composition_rejects_mismatch(journey, tmp_path):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    other_store = EventStore(tmp_path / "other.db")
    with pytest.raises(ValueError, match="privacy dependencies"):
        ParticipantPrivacyControls(auth, other_store, ledger, gate, clock=clock)
    other_auth = AuthStore(tmp_path / "other-auth.db", clock=clock)
    with pytest.raises(ValueError, match="privacy composition"):
        PrivateWebApplication(
            other_auth, a.app._conversations, config, privacy=a.app._privacy
        )


def test_ledger_fences_new_web_grant_and_second_deletion_intent(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    source = source_for(a)
    controls = a.app._privacy
    session = auth.authenticate(a.cookies["ar_session"])
    context = controls.context(session)
    payload = {"operation_id": "delete-first", "source": source, "confirm": "delete"}
    assert (
        json.loads(post(a, "/api/privacy/deletions", payload)[2])["status"] == "applied"
    )
    now = clock()
    grant = ConsentGrant(
        "consent:late",
        "late",
        context.principal_id,
        source,
        "public",
        ConsentPurpose.IDENTIFIABLE_QUOTATION,
        now,
        now + timedelta(hours=1),
        context.actor_reference,
        1,
        "late",
        context.authentication_event_id,
    )
    with pytest.raises(ValueError, match="source deletion prevents consent"):
        controls.consent.grant(context, grant, source_check=lambda: None)
    request = DeletionRequest(
        "delete-second",
        context.principal_id,
        source,
        ContentCategory.PRIVATE_PAYLOAD,
        now,
        context.actor_reference,
        "delete-second",
        context.authentication_event_id,
    )
    with pytest.raises(ValueError, match="source deletion already requested"):
        DeletionCoordinator(
            ledger, EventStoreErasureAdapter(store), clock=clock
        ).delete(
            context,
            request,
            source_check=lambda: None,
        )


def test_privacy_page_and_history_revalidate_before_response(journey, monkeypatch):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    controls = a.app._privacy
    real_status = controls.status

    def revoke_during_status(session):
        result = real_status(session)
        auth.revoke(a.cookies["ar_session"])
        return result

    monkeypatch.setattr(controls, "status", revoke_during_status)
    assert a.request("GET", "/api/privacy/status")[0] == "401 Unauthorized"
    a.login("alice", "synthetic-password-a")
    monkeypatch.setattr(controls, "status", real_status)
    assert acknowledge(a)[0] == "200 OK"
    real_history = a.app._conversations.history

    def revoke_during_history(principal):
        result = real_history(principal)
        auth.revoke(a.cookies["ar_session"])
        return result

    monkeypatch.setattr(a.app._conversations, "history", revoke_during_history)
    assert a.request("GET", "/api/history")[0] == "401 Unauthorized"
    a.login("alice", "synthetic-password-a")
    a.cookies.pop("ar_csrf")
    assert a.request("GET", "/privacy")[0] == "401 Unauthorized"


def test_initial_session_recheck_precedes_privacy_and_history_reads(
    journey, monkeypatch
):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    monkeypatch.setattr(a.app, "_session_current", lambda *_: False)
    assert a.request("GET", "/api/privacy/status")[0] == "401 Unauthorized"
    assert a.request("GET", "/api/history")[0] == "401 Unauthorized"


def test_grant_races_fail_closed_for_missing_foreign_and_changed_binding(
    journey, monkeypatch
):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    acknowledge(b)
    source_a = source_for(a)
    source_b = source_for(b)
    controls = a.app._privacy
    original = controls.consent.grant

    def no_commit(*args, **kwargs):
        raise ValueError("synthetic concurrent conflict")

    monkeypatch.setattr(controls.consent, "grant", no_commit)
    assert (
        post(a, "/api/privacy/grants", grant_for(source_a, "missing-op"))[0]
        == "409 Conflict"
    )

    def foreign_commit(context, grant, **kwargs):
        session_b = auth.authenticate(b.cookies["ar_session"])
        context_b = controls.context(session_b)
        original(
            context_b,
            replace(
                grant,
                principal_id=context_b.principal_id,
                content_reference=source_b,
                actor_reference=context_b.actor_reference,
                authentication_event_id=context_b.authentication_event_id,
            ),
            source_check=lambda: controls._source_unfenced(session_b, source_b),
        )
        raise ValueError("synthetic competing principal")

    monkeypatch.setattr(controls.consent, "grant", foreign_commit)
    assert (
        post(a, "/api/privacy/grants", grant_for(source_a, "foreign-op"))[0]
        == "404 Not Found"
    )

    def changed_commit(context, grant, **kwargs):
        original(
            context,
            replace(grant, purpose=ConsentPurpose.IDENTIFIABLE_DISCLOSURE),
            **kwargs,
        )
        raise ValueError("synthetic changed request")

    monkeypatch.setattr(controls.consent, "grant", changed_commit)
    assert (
        post(a, "/api/privacy/grants", grant_for(source_a, "changed-op"))[0]
        == "409 Conflict"
    )


def test_revocation_denies_foreign_and_second_operation(journey):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    acknowledge(b)
    source = source_for(b)
    assert (
        post(b, "/api/privacy/grants", grant_for(source, "b-grant"))[0] == "201 Created"
    )
    assert (
        post(
            a,
            "/api/privacy/revocations",
            {"operation_id": "foreign-revoke", "consent_id": "consent:b-grant"},
        )[0]
        == "404 Not Found"
    )
    assert (
        post(
            b,
            "/api/privacy/revocations",
            {"operation_id": "first-revoke", "consent_id": "consent:b-grant"},
        )[0]
        == "201 Created"
    )
    assert (
        post(
            b,
            "/api/privacy/revocations",
            {"operation_id": "second-revoke", "consent_id": "consent:b-grant"},
        )[0]
        == "409 Conflict"
    )


def test_deletion_race_bindings_and_explicit_confirmation(journey, monkeypatch):
    clock, auth, store, ledger, gate, fake, config, a, b = journey
    acknowledge(a)
    acknowledge(b)
    source_a = source_for(a)
    source_b = source_for(b)
    assert a.submit("second", "Другой источник")[0] == "201 Created"
    source_a2 = next(
        item["event_id"]
        for item in json.loads(a.request("GET", "/api/history")[2])["messages"]
        if item["content"] == "Другой источник"
    )
    payload = {"operation_id": "no-commit", "source": source_a, "confirm": "delete"}
    assert (
        post(a, "/api/privacy/deletions", {**payload, "confirm": "no"})[0]
        == "400 Bad Request"
    )
    controls = a.app._privacy

    def no_commit(*args, **kwargs):
        raise ValueError("synthetic conflict")

    monkeypatch.setattr(controls.deletion, "delete", no_commit)
    assert post(a, "/api/privacy/deletions", payload)[0] == "409 Conflict"

    def foreign_commit(context, request, **kwargs):
        session_b = auth.authenticate(b.cookies["ar_session"])
        context_b = controls.context(session_b)
        ledger.insert_deletion(
            context_b,
            replace(
                request,
                principal_id=context_b.principal_id,
                content_reference=source_b,
                actor_reference=context_b.actor_reference,
                authentication_event_id=context_b.authentication_event_id,
            ),
            DeletionDisposition.PENDING,
            "awaiting_erasure",
        )
        raise ValueError("synthetic competing principal")

    monkeypatch.setattr(controls.deletion, "delete", foreign_commit)
    assert (
        post(
            a, "/api/privacy/deletions", {**payload, "operation_id": "foreign-delete"}
        )[0]
        == "404 Not Found"
    )
    assert gate.reconcile()

    def changed_commit(context, request, **kwargs):
        ledger.insert_deletion(
            context,
            replace(request, content_reference=source_a2),
            DeletionDisposition.PENDING,
            "awaiting_erasure",
        )
        raise ValueError("synthetic changed source")

    monkeypatch.setattr(controls.deletion, "delete", changed_commit)
    assert (
        post(
            a, "/api/privacy/deletions", {**payload, "operation_id": "changed-delete"}
        )[0]
        == "409 Conflict"
    )


def privacy_http_crash_child(directory, phase):
    import os
    from acceptance.guards import install

    guard = pytest.MonkeyPatch()
    install(guard)
    folder = Path(directory)
    clock = Clock()
    auth = AuthStore(folder / "auth.db", clock=clock)
    auth.provision_account("alice", "synthetic-password-a", "participant-a")
    store = EventStore(folder / "events.db")
    ledger = PrivacyLedger(folder / "ledger.db")
    gate = PrivacyReadGate(ledger, store)
    assert gate.reconcile()
    controls = ParticipantPrivacyControls(auth, store, ledger, gate, clock=clock)
    runtime = Stage0ConversationRuntime(
        store, gate, FakeOpenAITransport([response()]), clock=clock
    )
    app = PrivateWebApplication(
        auth,
        runtime,
        PrivateWebConfig(frozenset({"chat.test"}), frozenset({"https://chat.test"})),
        privacy=controls,
    )
    browser = Browser(app)
    browser.login("alice", "synthetic-password-a")
    acknowledge(browser)
    source = source_for(browser, "CANARY-HTTP-EXIT-13")
    actual_erase = controls.deletion.adapter.erase

    def crash(request):
        if phase == "after_application":
            actual_erase(request)
        os._exit(23)

    controls.deletion.adapter.erase = crash
    post(
        browser,
        "/api/privacy/deletions",
        {"operation_id": "http-exit", "source": source, "confirm": "delete"},
    )
    raise AssertionError("exit boundary not reached")


@pytest.mark.parametrize("phase", ["after_intent", "after_application"])
def test_authenticated_http_deletion_process_exit_reconciles_before_reads(
    tmp_path, phase
):
    code = (
        "import sys; sys.path.insert(0, 'tests'); "
        "from test_participant_privacy_http import privacy_http_crash_child; "
        "privacy_http_crash_child(sys.argv[1], sys.argv[2])"
    )
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), phase],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert child.returncode == 23, child.stderr.decode()
    clock = Clock()
    auth = AuthStore(tmp_path / "auth.db", clock=clock)
    store = EventStore(tmp_path / "events.db")
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, store)
    controls = ParticipantPrivacyControls(auth, store, ledger, gate, clock=clock)
    app = PrivateWebApplication(
        auth,
        Stage0ConversationRuntime(store, gate, FakeOpenAITransport([]), clock=clock),
        PrivateWebConfig(frozenset({"chat.test"}), frozenset({"https://chat.test"})),
        privacy=controls,
    )
    browser = Browser(app)
    browser.login("alice", "synthetic-password-a")
    assert browser.request("GET", "/api/history")[0] == "503 Service Unavailable"
    assert b"pending" in browser.request("GET", "/api/privacy/status")[2]
    assert "Ожидает удаления".encode() in browser.request("GET", "/privacy")[2]
    assert post(browser, "/api/privacy/reconcile", {})[2] == b'{"status":"reconciled"}'
    assert b"applied" in browser.request("GET", "/api/privacy/status")[2]
    assert b"CANARY-HTTP-EXIT-13" not in browser.request("GET", "/api/history")[2]
