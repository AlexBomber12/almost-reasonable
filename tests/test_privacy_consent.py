from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import stat
import threading

import pytest

from app.privacy import ledger as ledger_module

from app.privacy import (
    AbstractionCandidate,
    AbstractionKind,
    AuditRecord,
    ConsentGrant,
    ConsentPurpose,
    ConsentService,
    DeletionRequest,
    NOTICE_VERSION,
    NoticeService,
    OperationOrigin,
    PrivacyLedger,
    TrustedPrivacyContext,
    validate_abstract_to_shared,
    validate_ordinary_derivation,
)


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def context(
    principal="participant-a", *, actor="actor-a", origin=OperationOrigin.CONTROL
):
    return TrustedPrivacyContext(principal, actor, "auth-event-a", origin)


def grant(**overrides):
    values = {
        "consent_id": "consent-001",
        "operation_id": "grant-operation-001",
        "principal_id": "participant-a",
        "content_reference": "event-001",
        "audience": "public-threads",
        "purpose": ConsentPurpose.IDENTIFIABLE_QUOTATION,
        "granted_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
        "actor_reference": "actor-a",
        "revision": 1,
        "correlation_id": "correlation-001",
        "authentication_event_id": "auth-event-a",
    }
    values.update(overrides)
    return ConsentGrant(**values)


@pytest.fixture
def service(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    return ConsentService(ledger, clock=lambda: NOW + timedelta(minutes=1)), ledger


def test_exact_consent_use_and_idempotent_authenticated_revocation(service):
    consent, ledger = service
    dispatched = []
    assert consent.grant(context(), grant()).created is True
    assert consent.grant(context(), grant()).created is False
    assert (
        consent.authorize(
            context(),
            principal_id="participant-a",
            content_reference="event-001",
            audience="public-threads",
            purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
            correlation_id="use-correlation-001",
            dispatch=lambda: dispatched.append("sent"),
        ).allowed
        is True
    )
    assert dispatched == ["sent"]
    repeated = consent.authorize(
        context(),
        principal_id="participant-a",
        content_reference="event-001",
        audience="public-threads",
        purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
        correlation_id="use-correlation-001",
        dispatch=lambda: dispatched.append("duplicate-send"),
    )
    assert (repeated.allowed, repeated.reason) == (False, "already_dispatched")
    assert dispatched == ["sent"]
    assert ledger.audit_records()[-1].result_code == "already_dispatched"

    first = consent.revoke(
        context(),
        consent_id="consent-001",
        operation_id="revoke-operation-001",
        revoked_at=NOW + timedelta(minutes=2),
        correlation_id="revoke-correlation-001",
    )
    second = consent.revoke(
        context(),
        consent_id="consent-001",
        operation_id="revoke-operation-001",
        revoked_at=NOW + timedelta(minutes=2),
        correlation_id="revoke-correlation-001",
    )
    assert (first.created, second.created) == (True, False)
    consent._clock = lambda: NOW + timedelta(minutes=3)
    denied = consent.authorize(
        context(),
        principal_id="participant-a",
        content_reference="event-001",
        audience="public-threads",
        purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
        correlation_id="use-correlation-002",
        dispatch=lambda: dispatched.append("revoked-send"),
    )
    assert (denied.allowed, denied.reason) == (False, "revoked")
    assert dispatched == ["sent"]
    assert all(
        "synthetic-private-canary" not in repr(row) for row in ledger.audit_records()
    )


def test_future_revocation_does_not_take_effect_early(service):
    consent, ledger = service
    consent.grant(context(), grant())
    consent.revoke(
        context(),
        consent_id="consent-001",
        operation_id="revoke-future",
        revoked_at=NOW + timedelta(minutes=30),
        correlation_id="revoke-future-correlation",
    )
    request = {
        "principal_id": "participant-a",
        "content_reference": "event-001",
        "audience": "public-threads",
        "purpose": ConsentPurpose.IDENTIFIABLE_QUOTATION,
        "correlation_id": "future-revocation-use",
        "dispatch": lambda: None,
    }
    consent._clock = lambda: NOW + timedelta(minutes=29)
    assert consent.authorize(context(), **request).allowed
    consent._clock = lambda: NOW + timedelta(minutes=30)
    request["correlation_id"] = "future-revocation-use-after"
    assert consent.authorize(context(), **request).reason == "revoked"
    assert ledger.revocations() == (("consent-001", NOW + timedelta(minutes=30)),)


def test_consent_dispatch_fences_concurrent_revocation(service):
    consent, _ = service
    consent.grant(context(), grant())
    revocation_started = threading.Event()
    revocation_finished = threading.Event()
    effects = []

    def revoke():
        revocation_started.set()
        consent.revoke(
            context(),
            consent_id="consent-001",
            operation_id="revoke-during-dispatch",
            revoked_at=NOW + timedelta(minutes=1),
            correlation_id="revoke-during-dispatch-correlation",
        )
        revocation_finished.set()

    thread = threading.Thread(target=revoke)

    def dispatch():
        thread.start()
        assert revocation_started.wait(timeout=1)
        assert not revocation_finished.wait(timeout=0.1)
        effects.append("sent")

    result = consent.authorize(
        context(),
        principal_id="participant-a",
        content_reference="event-001",
        audience="public-threads",
        purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
        correlation_id="fenced-dispatch",
        dispatch=dispatch,
    )
    thread.join(timeout=2)

    assert result.allowed is True
    assert effects == ["sent"]
    assert revocation_finished.is_set()


def test_consent_dispatch_failure_is_durable_and_cannot_be_retried(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    times = iter(
        (
            NOW + timedelta(minutes=1),
            NOW + timedelta(minutes=2),
            NOW + timedelta(minutes=3),
            NOW + timedelta(minutes=4),
        )
    )
    consent = ConsentService(ledger, clock=lambda: next(times))
    consent.grant(context(), grant())
    effects = []

    def dispatch():
        effects.append("sent")
        raise RuntimeError("lost dispatch acknowledgement")

    request = {
        "principal_id": "participant-a",
        "content_reference": "event-001",
        "audience": "public-threads",
        "purpose": ConsentPurpose.IDENTIFIABLE_QUOTATION,
        "correlation_id": "uncertain-dispatch",
    }
    with pytest.raises(RuntimeError, match="lost dispatch acknowledgement"):
        consent.authorize(context(), dispatch=dispatch, **request)

    repeated = consent.authorize(
        context(), dispatch=lambda: effects.append("duplicate-send"), **request
    )
    assert (repeated.allowed, repeated.reason) == (False, "dispatch_uncertain")
    assert effects == ["sent"]
    audits = ledger.audit_records()
    assert [(row.result_code, row.occurred_at) for row in audits[-2:]] == [
        ("dispatch_uncertain", NOW + timedelta(minutes=3)),
        ("dispatch_uncertain", NOW + timedelta(minutes=4)),
    ]
    with pytest.raises(ValueError, match="consent use operation identity conflict"):
        consent.authorize(
            context(),
            dispatch=lambda: None,
            **{**request, "content_reference": "event-002"},
        )


def test_interrupted_consent_dispatch_remains_pending_and_cannot_be_retried(service):
    class SimulatedProcessExit(BaseException):
        pass

    consent, _ = service
    consent.grant(context(), grant())
    request = {
        "principal_id": "participant-a",
        "content_reference": "event-001",
        "audience": "public-threads",
        "purpose": ConsentPurpose.IDENTIFIABLE_QUOTATION,
        "correlation_id": "interrupted-dispatch",
    }
    with pytest.raises(SimulatedProcessExit):
        consent.authorize(
            context(),
            dispatch=lambda: (_ for _ in ()).throw(SimulatedProcessExit()),
            **request,
        )
    result = consent.authorize(context(), dispatch=lambda: None, **request)
    assert (result.allowed, result.reason) == (False, "dispatch_uncertain")


def test_failure_before_consent_dispatch_is_audited_and_can_be_retried(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    times = iter(
        (
            NOW + timedelta(minutes=1),
            datetime(2026, 10, 10),
            NOW + timedelta(minutes=2),
            NOW + timedelta(minutes=3),
            NOW + timedelta(minutes=4),
        )
    )
    consent = ConsentService(ledger, clock=lambda: next(times))
    consent.grant(context(), grant())
    effects = []
    request = {
        "principal_id": "participant-a",
        "content_reference": "event-001",
        "audience": "public-threads",
        "purpose": ConsentPurpose.IDENTIFIABLE_QUOTATION,
        "correlation_id": "pre-dispatch-failure",
        "dispatch": lambda: effects.append("sent"),
    }

    with pytest.raises(ValueError, match="consent use timestamp"):
        consent.authorize(context(), **request)
    assert effects == []
    assert [row.result_code for row in ledger.audit_records()[-2:]] == [
        "dispatch_pending",
        "pre_dispatch_failure",
    ]

    result = consent.authorize(context(), **request)
    assert (result.allowed, result.reason) == (True, "authorized")
    assert effects == ["sent"]
    assert [row.result_code for row in ledger.audit_records()[-2:]] == [
        "dispatch_pending",
        "authorized",
    ]


@pytest.mark.parametrize(
    ("overrides", "authorized_at", "reason"),
    [
        (
            {"principal_id": "participant-b"},
            NOW + timedelta(minutes=1),
            "invalid_authorization",
        ),
        (
            {"audience": "participant-b"},
            NOW + timedelta(minutes=1),
            "no_matching_consent",
        ),
        (
            {"purpose": ConsentPurpose.IDENTIFIABLE_DISCLOSURE},
            NOW + timedelta(minutes=1),
            "no_matching_consent",
        ),
        ({}, NOW - timedelta(microseconds=1), "not_yet_valid"),
        ({}, NOW + timedelta(hours=1), "expired"),
    ],
)
def test_consent_fails_closed_for_scope_or_expiry(
    service, overrides, authorized_at, reason
):
    consent, _ = service
    consent._clock = lambda: authorized_at
    consent.grant(context(), grant())
    request = {
        "principal_id": "participant-a",
        "content_reference": "event-001",
        "audience": "public-threads",
        "purpose": ConsentPurpose.IDENTIFIABLE_QUOTATION,
        "correlation_id": "use-correlation-003",
        "dispatch": lambda: None,
    }
    request.update(overrides)
    result = consent.authorize(context(), **request)
    assert (result.allowed, result.reason) == (False, reason)
    repeated = consent.authorize(context(), **request)
    assert (repeated.allowed, repeated.reason) == (False, reason)


def test_chat_model_and_owner_impersonation_cannot_grant_or_revoke(service):
    consent, ledger = service
    for origin in (OperationOrigin.CHAT, OperationOrigin.MODEL):
        result = consent.grant(context(origin=origin), grant())
        assert (result.created, result.reason) == (False, "invalid_authentication")

    owner = context("participant-owner", actor="owner-actor")
    result = consent.grant(owner, grant())
    assert (result.created, result.reason) == (False, "invalid_authorization")
    assert (
        consent.revoke(
            owner,
            consent_id="consent-001",
            operation_id="revoke-operation-owner",
            revoked_at=NOW,
            correlation_id="revoke-correlation-owner",
        ).reason
        == "invalid_authorization"
    )
    consent.grant(
        context(), grant(consent_id="consent-actor", operation_id="grant-actor")
    )
    assert (
        consent.grant(
            context(),
            grant(
                consent_id="consent-auth-mismatch",
                operation_id="grant-auth-mismatch",
                authentication_event_id="other-auth",
            ),
        ).reason
        == "invalid_authentication"
    )
    assert ledger.audit_records()[-1].occurred_at == NOW + timedelta(minutes=1)
    spoofed = consent.grant(
        context(),
        grant(
            consent_id="consent-spoofed-actor",
            operation_id="grant-spoofed-actor",
            actor_reference="administrator",
        ),
    )
    assert spoofed.reason == "invalid_authentication"
    assert ledger.audit_records()[-1].actor_reference == "actor-a"
    assert ledger.audit_records()[-1].occurred_at == NOW + timedelta(minutes=1)
    assert (
        consent.revoke(
            context(origin=OperationOrigin.CHAT),
            consent_id="consent-actor",
            operation_id="revoke-chat",
            revoked_at=NOW,
            correlation_id="revoke-chat-correlation",
        ).reason
        == "invalid_authentication"
    )


def test_unauthenticated_consent_operations_reject_before_validation_or_reads(
    service, monkeypatch
):
    consent, ledger = service

    def unexpected_ledger_access(*_args, **_kwargs):
        raise AssertionError("unauthenticated request accessed consent state")

    monkeypatch.setattr(ledger, "authorize_consent_use", unexpected_ledger_access)
    use = consent.authorize(
        context(origin=OperationOrigin.MODEL),
        principal_id="bad principal",
        content_reference="bad content",
        audience="bad audience",
        purpose="bad purpose",
        correlation_id="unauthenticated use",
        dispatch=unexpected_ledger_access,
    )

    assert (use.allowed, use.reason) == (False, "invalid_authentication")
    assert ledger.audit_records()[-1].result_code == "invalid_authentication"
    assert ledger.audit_records()[-1].correlation_id == "auth-event-a"

    monkeypatch.setattr(ledger, "consent_principal", unexpected_ledger_access)
    revocation = consent.revoke(
        context(origin=OperationOrigin.CHAT),
        consent_id="bad consent",
        operation_id="bad operation",
        revoked_at=datetime(2026, 10, 10),
        correlation_id="unauthenticated revocation",
    )

    assert (revocation.created, revocation.reason) == (
        False,
        "invalid_authentication",
    )
    assert ledger.audit_records()[-1].result_code == "invalid_authentication"
    assert ledger.audit_records()[-1].correlation_id == "auth-event-a"


def test_consent_use_cannot_borrow_another_principals_grant(service):
    consent, _ = service
    consent.grant(
        context("participant-b", actor="actor-b"),
        grant(principal_id="participant-b", actor_reference="actor-b"),
    )
    result = consent.authorize(
        context(),
        principal_id="participant-b",
        content_reference="event-001",
        audience="public-threads",
        purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
        correlation_id="cross-principal-use",
        dispatch=lambda: None,
    )
    assert (result.allowed, result.reason) == (False, "invalid_authorization")


def test_consent_use_time_cannot_be_supplied_by_the_caller(service):
    consent, _ = service
    consent.grant(context(), grant())
    with pytest.raises(TypeError, match="used_at"):
        consent.authorize(
            context(),
            principal_id="participant-a",
            content_reference="event-001",
            audience="public-threads",
            purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
            correlation_id="backdated-use",
            dispatch=lambda: None,
            used_at=NOW,
        )


def test_consent_rejects_privilege_purpose_and_conflicting_operation(service):
    consent, _ = service
    with pytest.raises(ValueError, match="purpose"):
        grant(purpose="administrative_tools")
    consent.grant(context(), grant())
    with pytest.raises(ValueError, match="operation identity conflict"):
        consent.grant(context(), grant(content_reference="event-002"))
    with pytest.raises(FrozenInstanceError):
        grant().revision = 2
    with pytest.raises(ValueError, match="purpose"):
        consent.authorize(
            context(),
            principal_id="participant-a",
            content_reference="event-001",
            audience="public-threads",
            purpose="administrative_tools",
            correlation_id="bad-purpose",
            dispatch=lambda: None,
        )
    consent.revoke(
        context(),
        consent_id="consent-001",
        operation_id="revoke-operation-conflict",
        revoked_at=NOW,
        correlation_id="revoke-conflict",
    )
    with pytest.raises(ValueError, match="revocation operation identity conflict"):
        consent.revoke(
            context(),
            consent_id="consent-001",
            operation_id="different-revoke-operation",
            revoked_at=NOW,
            correlation_id="revoke-conflict",
        )


def test_consent_rejects_duplicate_scope_revision_and_uses_newer_revision(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    consent = ConsentService(ledger, clock=lambda: NOW + timedelta(minutes=3))
    consent.grant(context(), grant(expires_at=NOW + timedelta(minutes=1)))
    duplicate_revision = grant(
        consent_id="consent-002",
        operation_id="grant-operation-002",
        granted_at=NOW + timedelta(minutes=2),
        expires_at=NOW + timedelta(hours=1),
        correlation_id="correlation-002",
    )

    with pytest.raises(ValueError, match="consent revision conflict"):
        consent.grant(context(), duplicate_revision)

    renewed = consent.grant(
        context(),
        grant(
            consent_id="consent-003",
            operation_id="grant-operation-003",
            granted_at=NOW + timedelta(minutes=2),
            expires_at=NOW + timedelta(hours=1),
            revision=2,
            correlation_id="correlation-003",
        ),
    )
    result = consent.authorize(
        context(),
        principal_id="participant-a",
        content_reference="event-001",
        audience="public-threads",
        purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
        correlation_id="renewed-use",
        dispatch=lambda: None,
    )

    assert renewed.created is True
    assert (result.allowed, result.consent_id) == (True, "consent-003")


def test_ledger_rejects_cross_principal_direct_revocation(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    ledger.insert_consent(grant())
    audit_before = ledger.audit_records()
    generation_before = ledger.control_generation()

    with pytest.raises(ValueError, match="ownership"):
        ledger.insert_revocation(
            consent_id="consent-001",
            operation_id="revoke-direct",
            principal_id="participant-b",
            actor_reference="actor-b",
            authentication_event_id="auth-event-b",
            revoked_at=NOW,
            correlation_id="revoke-direct-correlation",
        )

    assert ledger.revocations() == ()
    assert ledger.control_generation() == generation_before
    assert ledger.audit_records() == audit_before


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"consent_id": "private consent"}, "consent id"),
        ({"operation_id": "private operation"}, "operation id"),
        ({"principal_id": "private principal"}, "principal id"),
        ({"actor_reference": "private actor"}, "actor reference"),
        ({"authentication_event_id": "private auth"}, "authentication event"),
        ({"revoked_at": datetime(2026, 10, 10)}, "timestamp"),
        ({"correlation_id": "private correlation"}, "correlation id"),
    ],
)
def test_ledger_rejects_malformed_direct_revocations(tmp_path, changes, message):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    ledger.insert_consent(grant())
    audit_before = ledger.audit_records()
    values = {
        "consent_id": "consent-001",
        "operation_id": "revoke-direct",
        "principal_id": "participant-a",
        "actor_reference": "actor-a",
        "authentication_event_id": "auth-event-a",
        "revoked_at": NOW,
        "correlation_id": "revoke-direct-correlation",
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        ledger.insert_revocation(**values)

    assert ledger.revocations() == ()
    assert ledger.audit_records() == audit_before


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"principal_id": "private principal"}, "principal id"),
        ({"content_reference": "private content"}, "content reference"),
        ({"audience": "private audience"}, "audience"),
        ({"purpose": "private_purpose"}, "purpose"),
        ({"correlation_id": "private correlation"}, "correlation id"),
        ({"actor_reference": "private actor"}, "actor reference"),
        ({"authentication_event_id": "private auth"}, "authentication event"),
    ],
)
def test_ledger_rejects_malformed_direct_consent_use_fields(tmp_path, changes, message):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    values = {
        "principal_id": "participant-a",
        "content_reference": "event-001",
        "audience": "public-threads",
        "purpose": ConsentPurpose.IDENTIFIABLE_QUOTATION,
        "correlation_id": "direct-use-correlation",
        "actor_reference": "actor-a",
        "authentication_event_id": "auth-event-a",
        "clock": lambda: NOW,
        "dispatch": lambda: None,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        ledger.authorize_consent_use(**values)

    assert ledger.audit_records() == ()
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM consent_use_attempts"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"correlation_id": "private audit text"}, "correlation id"),
        ({"actor_reference": "private actor text"}, "actor reference"),
        ({"action_type": "arbitrary_action"}, "audit shape"),
        ({"record_category": "arbitrary_category"}, "audit shape"),
        ({"occurred_at": datetime(2026, 10, 10)}, "audit timestamp"),
        ({"result_code": "synthetic-private-canary"}, "audit shape"),
        ({"redaction_status": "contains_payload"}, "audit shape"),
        (
            {
                "action_type": "consent_grant",
                "record_category": "privacy_notice",
                "result_code": "applied:erasure_applied",
                "redaction_status": "content_free_tombstone",
            },
            "audit shape",
        ),
    ],
)
def test_audit_records_reject_unconstrained_or_non_utc_values(changes, message):
    values = {
        "correlation_id": "audit-correlation",
        "actor_reference": "actor-a",
        "action_type": "consent_use",
        "record_category": "consent",
        "occurred_at": NOW,
        "result_code": "authorized",
        "redaction_status": "payload_free",
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        AuditRecord(**values)


def test_abstraction_boundary_is_closed_and_preserves_hidden_provenance():
    safe = AbstractionCandidate(
        candidate_id="candidate-001",
        kind=AbstractionKind.RESEARCH_QUESTION,
        text="Как люди формируют устойчивые интеллектуальные привычки?",
        source_audience="private:participant-a",
        protected_source_references=("event-001",),
        semantic_screen_passed=True,
    )
    accepted = validate_abstract_to_shared(safe)
    assert accepted.accepted is True
    assert accepted.activation_allowed is False
    assert accepted.protected_source_references == ("event-001",)

    assert validate_ordinary_derivation(
        ("private:participant-a",), "private:participant-a"
    ).accepted
    assert not validate_ordinary_derivation(
        ("private:participant-a",), "shared"
    ).accepted


@pytest.mark.parametrize(
    ("source_audiences", "target_audience"),
    [
        ([], "private:participant-a"),
        (("",), ""),
        (("bad audience",), "bad audience"),
        (("private:participant-a",), ""),
    ],
)
def test_ordinary_derivation_rejects_malformed_audiences(
    source_audiences, target_audience
):
    result = validate_ordinary_derivation(source_audiences, target_audience)
    assert (result.accepted, result.reason) == (False, "invalid_audience")


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"kind": "memory"}, "unsupported_abstraction_kind"),
        ({"text": "  "}, "empty_abstraction"),
        ({"source_audience": "shared"}, "invalid_source_audience"),
        ({"source_audience": "private"}, "invalid_source_audience"),
        ({"source_audience": "private:"}, "invalid_source_audience"),
        ({"source_audience": "private::participant-a"}, "invalid_source_audience"),
        ({"source_audience": "private:bad audience"}, "invalid_source_audience"),
        ({"source_audience": ["private:participant-a"]}, "invalid_source_audience"),
        ({"max_source_overlap": 1.5}, "invalid_source_overlap"),
        ({"direct_identifiers": ("alice@example.invalid",)}, "direct_identifier"),
        ({"direct_identifiers": []}, "direct_identifier"),
        ({"private_canaries": ("CANARY-A-77",)}, "private_canary"),
        ({"private_canaries": None}, "private_canary"),
        ({"contains_personal_information": True}, "personal_information"),
        ({"contains_personal_information": None}, "personal_information"),
        ({"contains_quotation": True}, "exact_or_attributable_quotation"),
        ({"contains_quotation": 0}, "exact_or_attributable_quotation"),
        ({"recognizable_paraphrase": True}, "recognizable_paraphrase"),
        ({"recognizable_paraphrase": "false"}, "recognizable_paraphrase"),
        ({"contains_relationship_detail": True}, "relationship_detail"),
        ({"contains_relationship_detail": None}, "relationship_detail"),
        ({"contains_attribution": True}, "attribution"),
        ({"contains_attribution": 0}, "attribution"),
        ({"semantic_screen_passed": False}, "semantic_screen_failed_or_uncertain"),
        ({"semantic_screen_passed": "false"}, "semantic_screen_failed_or_uncertain"),
        ({"protected_source_references": ()}, "missing_or_invalid_source_provenance"),
        (
            {"protected_source_references": True},
            "missing_or_invalid_source_provenance",
        ),
        (
            {"protected_source_references": ("bad reference",)},
            "missing_or_invalid_source_provenance",
        ),
        ({"max_source_overlap": 0.5}, "excessive_source_overlap"),
    ],
)
def test_abstraction_rejects_identifiable_or_untraceable_candidates(changes, reason):
    values = {
        "candidate_id": "candidate-002",
        "kind": AbstractionKind.BELIEF,
        "text": "Общая абстрактная идея без идентификаторов.",
        "source_audience": "private:participant-a",
        "protected_source_references": ("event-002",),
        "semantic_screen_passed": True,
    }
    values.update(changes)
    result = validate_abstract_to_shared(AbstractionCandidate(**values))
    assert (result.accepted, result.reason) == (False, reason)


def test_privacy_ledger_survives_restart_and_rejects_bad_schema(tmp_path):
    path = tmp_path / "privacy.sqlite3"
    ConsentService(PrivacyLedger(path)).grant(context(), grant())
    assert (
        ConsentService(PrivacyLedger(path), clock=lambda: NOW)
        .authorize(
            context(),
            principal_id="participant-a",
            content_reference="event-001",
            audience="public-threads",
            purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
            correlation_id="restart-use",
            dispatch=lambda: None,
        )
        .allowed
    )

    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE privacy_schema_metadata SET version = 999")
    with pytest.raises(RuntimeError, match="unsupported privacy ledger schema"):
        PrivacyLedger(path)

    malformed = tmp_path / "malformed-ledger.sqlite3"
    malformed.write_text("not sqlite")
    with pytest.raises(RuntimeError, match="validate existing"):
        PrivacyLedger(malformed)


def test_privacy_ledger_fsyncs_parent_after_first_schema_commit(tmp_path, monkeypatch):
    path = tmp_path / "privacy.sqlite3"
    real_fsync = os.fsync
    fsync_observations = []

    def observe_fsync(descriptor):
        with sqlite3.connect(path) as connection:
            version = connection.execute(
                "SELECT version FROM privacy_schema_metadata WHERE singleton=1"
            ).fetchone()
        identity = os.fstat(descriptor)
        fsync_observations.append((identity.st_dev, identity.st_ino, version))
        real_fsync(descriptor)

    monkeypatch.setattr(ledger_module.os, "fsync", observe_fsync)

    PrivacyLedger(path)
    PrivacyLedger(path)

    parent_identity = tmp_path.stat()
    assert fsync_observations == [
        (
            parent_identity.st_dev,
            parent_identity.st_ino,
            (ledger_module.PRIVACY_LEDGER_SCHEMA_VERSION,),
        )
    ]


def test_privacy_ledger_rejects_path_replacement_before_mutation(tmp_path, monkeypatch):
    path = tmp_path / "privacy.sqlite3"
    path.touch(mode=0o640)
    replacement = tmp_path / "replacement.sqlite3"
    replacement.touch(mode=0o644)
    real_connect = sqlite3.connect
    replaced = False

    def replace_after_open(database, *args, **kwargs):
        nonlocal replaced
        connection = real_connect(database, *args, **kwargs)
        if not replaced and str(database).startswith("file:/proc/self/fd/"):
            replaced = True
            path.unlink()
            replacement.rename(path)
        return connection

    monkeypatch.setattr(ledger_module.sqlite3, "connect", replace_after_open)
    with pytest.raises(RuntimeError, match="identity changed"):
        PrivacyLedger(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


@pytest.mark.parametrize("mutation", ["remove", "replace"])
def test_privacy_ledger_operations_reject_path_identity_changes(tmp_path, mutation):
    path = tmp_path / "privacy.sqlite3"
    ledger = PrivacyLedger(path)
    if mutation == "remove":
        path.unlink()
    else:
        substitute_path = tmp_path / "substitute.sqlite3"
        PrivacyLedger(substitute_path)
        path.unlink()
        substitute_path.rename(path)

    with pytest.raises(RuntimeError, match="path identity changed"):
        ledger.control_generation()


def test_privacy_ledger_rejects_a_connection_to_a_substitute_inode(
    tmp_path, monkeypatch
):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    substitute_path = tmp_path / "substitute.sqlite3"
    PrivacyLedger(substitute_path)
    real_connect = sqlite3.connect
    monkeypatch.setattr(
        ledger,
        "_connect_descriptor",
        lambda _descriptor: real_connect(substitute_path),
    )

    with pytest.raises(RuntimeError, match="connection identity changed"):
        ledger.control_generation()


def test_privacy_ledger_rejects_aba_replacement_during_connection(
    tmp_path, monkeypatch
):
    path = tmp_path / "privacy.sqlite3"
    ledger = PrivacyLedger(path)
    substitute_path = tmp_path / "substitute.sqlite3"
    PrivacyLedger(substitute_path)
    held_path = tmp_path / "held.sqlite3"
    real_connect = sqlite3.connect
    replaced = False

    def replace_during_open(database, *args, **kwargs):
        nonlocal replaced
        if not replaced and str(database).startswith("file:/proc/self/fd/"):
            replaced = True
            path.rename(held_path)
            substitute_path.rename(path)
            connection = real_connect(database, *args, **kwargs)
            path.rename(substitute_path)
            held_path.rename(path)
            return connection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(ledger_module.sqlite3, "connect", replace_during_open)
    with pytest.raises(RuntimeError, match="connection identity changed"):
        ledger.control_generation()


def test_privacy_ledger_normalizes_relative_path_before_working_directory_change(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    ledger = PrivacyLedger("privacy.sqlite3")
    other_directory = tmp_path / "other"
    other_directory.mkdir()
    monkeypatch.chdir(other_directory)

    assert ledger.path == tmp_path / "privacy.sqlite3"
    assert ledger.control_generation() == 0


def test_privacy_ledger_rejects_an_unrelated_existing_database(tmp_path):
    path = tmp_path / "application.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE application_events(event_id TEXT PRIMARY KEY)")
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    path.chmod(0o640)

    with pytest.raises(RuntimeError, match="unrelated tables"):
        PrivacyLedger(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert tables == {"application_events"}


@pytest.mark.parametrize(
    "schema",
    [
        ledger_module._SCHEMA[:-1],
        tuple(
            statement.replace(
                "generation INTEGER NOT NULL CHECK(generation >= 0)",
                "generation INTEGER",
            )
            for statement in ledger_module._SCHEMA
        ),
    ],
)
def test_privacy_ledger_rejects_tampered_schema_without_mutation(tmp_path, schema):
    path = tmp_path / "spoofed-privacy.sqlite3"
    with sqlite3.connect(path) as connection:
        for statement in schema:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO privacy_schema_metadata VALUES(1, ?)",
            (ledger_module.PRIVACY_LEDGER_SCHEMA_VERSION,),
        )
        original_schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    path.chmod(0o640)

    with pytest.raises(RuntimeError, match="invalid privacy ledger schema"):
        PrivacyLedger(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert (
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name"
            ).fetchall()
            == original_schema
        )


def test_privacy_ledger_configuration_requires_wal_mode(tmp_path):
    class JournalModeCursor:
        @staticmethod
        def fetchone():
            return ("delete",)

    class WalUnavailableConnection:
        @staticmethod
        def execute(statement):
            if statement == "PRAGMA foreign_keys=ON":
                return None
            assert statement == "PRAGMA journal_mode=WAL"
            return JournalModeCursor()

    ledger = object.__new__(PrivacyLedger)
    ledger.path = tmp_path / "privacy.sqlite3"
    with pytest.raises(RuntimeError, match="requires WAL"):
        ledger._configure(WalUnavailableConnection())


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TrustedPrivacyContext(
            "bad space", "actor", "auth", OperationOrigin.CONTROL
        ),
        lambda: TrustedPrivacyContext("participant", "actor", "auth", "ordinary_chat"),
        lambda: grant(expires_at=NOW),
        lambda: grant(revision=True),
        lambda: grant(revision=0),
        lambda: DeletionRequest(
            "delete",
            "participant",
            "event",
            "private_payload",
            NOW,
            "actor",
            "corr",
            "auth",
        ),
    ],
)
def test_typed_values_reject_malformed_inputs(factory):
    with pytest.raises(ValueError):
        factory()


def test_timestamps_must_be_utc_and_ledger_path_is_protected(tmp_path):
    with pytest.raises(ValueError, match="timestamp"):
        grant(granted_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="represented in UTC"):
        grant(granted_at=datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1))))
    with pytest.raises(ValueError, match="in-memory"):
        PrivacyLedger(":memory:")
    target = tmp_path / "target.sqlite3"
    target.touch()
    link = tmp_path / "ledger.sqlite3"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic"):
        PrivacyLedger(link)

    path = tmp_path / "protected.sqlite3"
    path.touch(mode=0o644)
    PrivacyLedger(path)
    assert os.stat(path).st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mode", [0o620, 0o602])
def test_privacy_ledger_rejects_existing_files_writable_by_other_accounts(
    tmp_path, mode
):
    path = tmp_path / "privacy.sqlite3"
    path.touch()
    path.chmod(mode)

    with pytest.raises(ValueError, match="file is unsafe"):
        PrivacyLedger(path)

    assert stat.S_IMODE(path.stat().st_mode) == mode


def test_privacy_ledger_rejects_an_existing_file_owned_by_another_account(
    tmp_path, monkeypatch
):
    path = tmp_path / "privacy.sqlite3"
    path.touch(mode=0o600)
    target_identity = path.stat()
    real_fstat = os.fstat

    def report_foreign_owner(descriptor):
        identity = real_fstat(descriptor)
        if (identity.st_dev, identity.st_ino) == (
            target_identity.st_dev,
            target_identity.st_ino,
        ):
            values = list(identity)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return identity

    monkeypatch.setattr(ledger_module.os, "fstat", report_foreign_owner)

    with pytest.raises(ValueError, match="file is unsafe"):
        PrivacyLedger(path)


def test_privacy_ledger_rejects_unsafe_parent_chains(tmp_path):
    unsafe = tmp_path / "unsafe"
    nested = unsafe / "nested"
    nested.mkdir(parents=True)
    unsafe.chmod(0o777)

    for path in (unsafe / "direct.sqlite3", nested / "nested.sqlite3"):
        with pytest.raises(ValueError, match="parent is unsafe"):
            PrivacyLedger(path)
        assert not path.exists()

    target = tmp_path / "target"
    target.mkdir()
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="parent is unsafe"):
        PrivacyLedger(parent_link / "linked.sqlite3")


def test_privacy_ledger_file_open_failure_closes_pinned_parent(tmp_path, monkeypatch):
    class SimulatedOpenFailure(BaseException):
        pass

    parent_descriptors = []
    real_open_chain = ledger_module.open_trusted_directory_chain
    real_open = os.open

    def track_parent(directory):
        descriptor = real_open_chain(directory)
        parent_descriptors.append(descriptor)
        return descriptor

    def fail_ledger_open(path, *args, **kwargs):
        if path == "privacy.sqlite3":
            raise SimulatedOpenFailure
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(ledger_module, "open_trusted_directory_chain", track_parent)
    monkeypatch.setattr(ledger_module.os, "open", fail_ledger_open)

    with pytest.raises(SimulatedOpenFailure):
        PrivacyLedger(tmp_path / "privacy.sqlite3")

    with pytest.raises(OSError):
        os.fstat(parent_descriptors[0])


def test_privacy_ledger_rejects_non_regular_sidecars(tmp_path):
    class WalConnection:
        @staticmethod
        def execute(statement):
            if statement == "PRAGMA foreign_keys=ON":
                return None

            class Cursor:
                @staticmethod
                def fetchone():
                    return ("wal",)

            return Cursor()

    ledger = object.__new__(PrivacyLedger)
    ledger.path = tmp_path / "privacy.sqlite3"
    ledger._parent_descriptor = ledger_module.open_trusted_directory_chain(tmp_path)
    os.mkfifo(tmp_path / "privacy.sqlite3-wal")
    try:
        with pytest.raises(RuntimeError, match="sidecar is unsafe"):
            ledger._configure(WalConnection())
    finally:
        os.close(ledger._parent_descriptor)


@pytest.mark.parametrize("sidecar_kind", ["writable", "symlink"])
def test_privacy_ledger_rejects_unsafe_sidecars_before_connecting(
    tmp_path, monkeypatch, sidecar_kind
):
    path = tmp_path / "privacy.sqlite3"
    PrivacyLedger(path)
    sidecar = Path(f"{path}-wal")
    if sidecar_kind == "writable":
        sidecar.write_bytes(b"untrusted-wal")
        sidecar.chmod(0o666)
    else:
        target = tmp_path / "foreign-wal"
        target.write_bytes(b"untrusted-wal")
        sidecar.symlink_to(target)
    connection_attempted = False

    def reject_connection(_descriptor):
        nonlocal connection_attempted
        connection_attempted = True
        raise AssertionError("connected before validating sidecars")

    monkeypatch.setattr(
        PrivacyLedger, "_connect_descriptor", staticmethod(reject_connection)
    )

    with pytest.raises(RuntimeError, match="sidecar is unsafe"):
        PrivacyLedger(path)

    assert connection_attempted is False
    assert sidecar.read_bytes() == b"untrusted-wal"
    if sidecar_kind == "writable":
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o666
    else:
        assert sidecar.is_symlink()


def run_concurrently(operation):
    barrier = threading.Barrier(3)
    results = []
    errors = []

    def run():
        barrier.wait()
        try:
            results.append(operation())
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert errors == []
    return results


def test_concurrent_privacy_ledger_first_initialization_is_serialized(tmp_path):
    path = tmp_path / "privacy.sqlite3"
    assert run_concurrently(lambda: PrivacyLedger(path).control_generation()) == [0, 0]
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version FROM privacy_schema_metadata WHERE singleton=1"
        ).fetchone() == (ledger_module.PRIVACY_LEDGER_SCHEMA_VERSION,)


def test_concurrent_consent_revocation_and_notice_retries_are_idempotent(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    consent = ConsentService(ledger)
    assert sorted(
        result.created
        for result in run_concurrently(lambda: consent.grant(context(), grant()))
    ) == [False, True]
    assert sorted(
        result.created
        for result in run_concurrently(
            lambda: consent.revoke(
                context(),
                consent_id="consent-001",
                operation_id="revoke-concurrent",
                revoked_at=NOW,
                correlation_id="revoke-concurrent-correlation",
            )
        )
    ) == [False, True]
    notices = NoticeService(ledger)
    assert sorted(
        result.created
        for result in run_concurrently(
            lambda: notices.acknowledge(
                context(), NOTICE_VERSION, NOW, "notice-concurrent"
            )
        )
    ) == [False, True]


def test_consent_use_requires_control_origin_and_audits_decision(service):
    consent, ledger = service
    consent.grant(context(), grant())
    result = consent.authorize(
        context(origin=OperationOrigin.CHAT),
        principal_id="participant-a",
        content_reference="event-001",
        audience="public-threads",
        purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
        correlation_id="chat-consent-use",
        dispatch=lambda: None,
    )
    assert (result.allowed, result.reason) == (False, "invalid_authentication")
    audit = ledger.audit_records()[-1]
    assert (audit.actor_reference, audit.result_code) == (
        "actor-a",
        "invalid_authentication",
    )


def test_grant_rolls_back_if_atomic_audit_write_fails(tmp_path, monkeypatch):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    consent = ConsentService(ledger)
    monkeypatch.setattr(
        ledger,
        "_insert_audit",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("audit failure")),
    )
    with pytest.raises(RuntimeError, match="audit failure"):
        consent.grant(context(), grant())
    assert ledger.consent_principal("consent-001") is None


def test_revocation_rolls_back_if_atomic_audit_write_fails(tmp_path, monkeypatch):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    consent = ConsentService(ledger)
    consent.grant(context(), grant())
    monkeypatch.setattr(
        ledger,
        "_insert_audit",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("audit failure")),
    )
    with pytest.raises(RuntimeError, match="audit failure"):
        consent.revoke(
            context(),
            consent_id="consent-001",
            operation_id="revoke-audit-failure",
            revoked_at=NOW,
            correlation_id="revoke-audit-failure-correlation",
        )
    assert ledger.revocations() == ()


def test_interrupted_first_initialization_rolls_back_complete_schema(
    tmp_path, monkeypatch
):
    path = tmp_path / "privacy.sqlite3"
    schema = ledger_module._SCHEMA
    monkeypatch.setattr(
        ledger_module, "_SCHEMA", (schema[0], "THIS IS NOT VALID SQLITE")
    )
    with pytest.raises(sqlite3.OperationalError):
        PrivacyLedger(path)
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            == []
        )

    monkeypatch.setattr(ledger_module, "_SCHEMA", schema)
    assert PrivacyLedger(path).path == path


def test_missing_control_generation_fails_closed(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("DELETE FROM privacy_control_generation")
    with pytest.raises(RuntimeError, match="generation"):
        ledger.control_generation()
    with pytest.raises(RuntimeError, match="generation"):
        ledger.reconciliation_snapshot()


def test_revocation_rolls_back_when_control_generation_is_missing(tmp_path):
    ledger = PrivacyLedger(tmp_path / "privacy.sqlite3")
    ledger.insert_consent(grant())
    audit_before = ledger.audit_records()
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("DELETE FROM privacy_control_generation")

    with pytest.raises(RuntimeError, match="generation"):
        ledger.insert_revocation(
            consent_id="consent-001",
            operation_id="revoke-operation-001",
            principal_id="participant-a",
            actor_reference="actor-a",
            authentication_event_id="auth-event-a",
            revoked_at=NOW,
            correlation_id="revoke-correlation-001",
        )

    assert ledger.revocations() == ()
    assert ledger.audit_records() == audit_before
