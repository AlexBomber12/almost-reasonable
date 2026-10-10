from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
import os
import sqlite3
import stat
import threading

import pytest

from app.privacy import ledger as ledger_module

from app.privacy import (
    AbstractionCandidate,
    AbstractionKind,
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
        ("dispatch_pending", NOW + timedelta(minutes=1)),
        ("dispatch_uncertain", NOW + timedelta(minutes=3)),
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


def test_privacy_ledger_rejects_path_replacement_before_mutation(tmp_path, monkeypatch):
    path = tmp_path / "privacy.sqlite3"
    path.touch(mode=0o640)
    replacement = tmp_path / "replacement.sqlite3"
    replacement.touch(mode=0o644)
    real_connect = sqlite3.connect

    def replace_after_open(database, *args, **kwargs):
        connection = real_connect(database, *args, **kwargs)
        path.unlink()
        replacement.rename(path)
        return connection

    monkeypatch.setattr(ledger_module.sqlite3, "connect", replace_after_open)
    with pytest.raises(RuntimeError, match="identity changed"):
        PrivacyLedger(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


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
