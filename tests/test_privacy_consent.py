from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
import os
import sqlite3

import pytest

from app.privacy import (
    AbstractionCandidate,
    AbstractionKind,
    ConsentGrant,
    ConsentPurpose,
    ConsentService,
    DeletionRequest,
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
    return ConsentService(ledger), ledger


def test_exact_consent_use_and_idempotent_authenticated_revocation(service):
    consent, ledger = service
    assert consent.grant(context(), grant()).created is True
    assert consent.grant(context(), grant()).created is False
    assert (
        consent.authorize(
            principal_id="participant-a",
            content_reference="event-001",
            audience="public-threads",
            purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
            used_at=NOW + timedelta(minutes=1),
            correlation_id="use-correlation-001",
        ).allowed
        is True
    )

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
    denied = consent.authorize(
        principal_id="participant-a",
        content_reference="event-001",
        audience="public-threads",
        purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
        used_at=NOW + timedelta(minutes=3),
        correlation_id="use-correlation-002",
    )
    assert (denied.allowed, denied.reason) == (False, "revoked")
    assert all(
        "synthetic-private-canary" not in repr(row) for row in ledger.audit_records()
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"principal_id": "participant-b"}, "no_matching_consent"),
        ({"audience": "participant-b"}, "no_matching_consent"),
        ({"purpose": ConsentPurpose.IDENTIFIABLE_DISCLOSURE}, "no_matching_consent"),
        ({"used_at": NOW - timedelta(microseconds=1)}, "not_yet_valid"),
        ({"used_at": NOW + timedelta(hours=1)}, "expired"),
    ],
)
def test_consent_fails_closed_for_scope_or_expiry(service, overrides, reason):
    consent, _ = service
    consent.grant(context(), grant())
    request = {
        "principal_id": "participant-a",
        "content_reference": "event-001",
        "audience": "public-threads",
        "purpose": ConsentPurpose.IDENTIFIABLE_QUOTATION,
        "used_at": NOW + timedelta(minutes=1),
        "correlation_id": "use-correlation-003",
    }
    request.update(overrides)
    result = consent.authorize(**request)
    assert (result.allowed, result.reason) == (False, reason)


def test_chat_model_and_owner_impersonation_cannot_grant_or_revoke(service):
    consent, _ = service
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
            principal_id="participant-a",
            content_reference="event-001",
            audience="public-threads",
            purpose="administrative_tools",
            used_at=NOW,
            correlation_id="bad-purpose",
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
    ("changes", "reason"),
    [
        ({"kind": "memory"}, "unsupported_abstraction_kind"),
        ({"text": "  "}, "empty_abstraction"),
        ({"source_audience": "shared"}, "invalid_source_audience"),
        ({"max_source_overlap": 1.5}, "invalid_source_overlap"),
        ({"direct_identifiers": ("alice@example.invalid",)}, "direct_identifier"),
        ({"private_canaries": ("CANARY-A-77",)}, "private_canary"),
        ({"contains_personal_information": True}, "personal_information"),
        ({"contains_quotation": True}, "exact_or_attributable_quotation"),
        ({"recognizable_paraphrase": True}, "recognizable_paraphrase"),
        ({"contains_relationship_detail": True}, "relationship_detail"),
        ({"contains_attribution": True}, "attribution"),
        ({"semantic_screen_passed": False}, "semantic_screen_failed_or_uncertain"),
        ({"semantic_screen_passed": "false"}, "semantic_screen_failed_or_uncertain"),
        ({"protected_source_references": ()}, "missing_source_provenance"),
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
        ConsentService(PrivacyLedger(path))
        .authorize(
            principal_id="participant-a",
            content_reference="event-001",
            audience="public-threads",
            purpose=ConsentPurpose.IDENTIFIABLE_QUOTATION,
            used_at=NOW,
            correlation_id="restart-use",
        )
        .allowed
    )

    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE privacy_schema_metadata SET version = 999")
    with pytest.raises(RuntimeError, match="unsupported privacy ledger schema"):
        PrivacyLedger(path)


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
