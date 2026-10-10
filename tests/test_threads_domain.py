from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.threads.domain import (
    MAX_CONTENT_BYTES,
    MAX_INBOUND_CONTENT_BYTES,
    AccountPolicy,
    PublicationType,
    SourceProvenance,
    Stage,
    content_fingerprint,
    issue_approval,
    require_aware_time,
    require_reference,
    validate_approval,
    validate_content,
)


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
SCOPES = frozenset(
    {
        "threads_basic",
        "threads_content_publish",
        "threads_read_replies",
        "threads_manage_mentions",
    }
)


def policy(**changes):
    value = AccountPolicy(
        account_ref="account-1",
        stage=Stage.SIMULATION,
        simulation_enabled=True,
        account_authorized=True,
        scopes=SCOPES,
        privacy_gate_available=True,
        russian_output_gate_available=True,
        publication_capability_enabled=True,
    )
    for name, replacement in changes.items():
        setattr(value, name, replacement)
    return value


def approval(**changes):
    values = {
        "reference": "approval-1",
        "account_ref": "account-1",
        "operation_id": "operation-1",
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(changes)
    return issue_approval(**values)


def test_policy_reports_each_fail_closed_publication_blocker():
    assert policy().publication_blockers(PublicationType.POST) == ()
    assert policy(stage=Stage.DISABLED).publication_blockers(PublicationType.POST) == (
        "public stage is disabled",
    )
    assert policy(stage=Stage.STAGE_3).publication_blockers(PublicationType.POST) == (
        "live Threads transport is not implemented",
    )
    blocked = policy(
        simulation_enabled=False,
        account_authorized=False,
        scopes=frozenset(),
        privacy_gate_available=False,
        russian_output_gate_available=False,
        publication_capability_enabled=False,
        emergency_stop=True,
    ).publication_blockers(PublicationType.REPLY)
    assert blocked == (
        "simulation capability is disabled",
        "account is not authorized",
        "required publication scopes are unavailable",
        "required reply scopes are unavailable",
        "privacy gate is unavailable",
        "Russian output gate is unavailable",
        "publication capability is disabled",
        "emergency stop is active",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"account_ref": "bad value"},
        {"stage": "simulation"},
        {"simulation_enabled": 1},
        {"scopes": {"threads_basic"}},
        {"scopes": frozenset({1})},
    ],
)
def test_policy_rejects_malformed_trusted_configuration(changes):
    with pytest.raises(ValueError):
        policy(**changes).validate()


@pytest.mark.parametrize(
    "changes",
    [
        {"reference": "bad value"},
        {"account_ref": "bad value"},
        {"operation_id": "bad value"},
        {"expires_at": datetime(2026, 1, 1)},
        {"revoked": 1},
        {"duplicate_risk_acknowledged": 1},
    ],
)
def test_approval_issuer_rejects_malformed_control_plane_evidence(changes):
    with pytest.raises(ValueError):
        approval(**changes)


def test_approval_validation_rejects_forgery_target_status_expiry_and_missing_ack():
    valid = approval()
    validate_approval(
        valid, account_ref="account-1", operation_id="operation-1", now=NOW
    )
    forged = replace(valid, _authority=object())
    cases = [
        object(),
        forged,
        approval(account_ref="account-2"),
        approval(operation_id="operation-2"),
        approval(revoked=True),
        approval(expires_at=NOW),
    ]
    for value in cases:
        with pytest.raises(ValueError):
            validate_approval(
                value,
                account_ref="account-1",
                operation_id="operation-1",
                now=NOW,
            )
    with pytest.raises(ValueError, match="duplicate risk"):
        validate_approval(
            valid,
            account_ref="account-1",
            operation_id="operation-1",
            now=NOW,
            require_duplicate_ack=True,
        )
    with pytest.raises(ValueError, match="current time"):
        validate_approval(
            valid,
            account_ref="account-1",
            operation_id="operation-1",
            now=datetime(2026, 1, 1),
        )


def test_content_validation_and_fingerprint_are_bounded_and_deterministic():
    assert validate_content("Привет") == "Привет"
    assert validate_content("x" * MAX_INBOUND_CONTENT_BYTES, inbound=True)
    assert content_fingerprint("  Привет   МИР ") == content_fingerprint("привет мир")
    for value in (None, "", "a\x00b"):
        with pytest.raises(ValueError, match="invalid content"):
            validate_content(value)
    with pytest.raises(ValueError, match="byte bound"):
        validate_content("я" * (MAX_CONTENT_BYTES // 2 + 1))
    with pytest.raises(ValueError, match="byte bound"):
        validate_content("x" * (MAX_INBOUND_CONTENT_BYTES + 1), inbound=True)
    with pytest.raises(ValueError, match="text bound"):
        validate_content("x" * 501)


def test_reference_and_time_validators_are_closed():
    assert require_reference("field", "valid:reference-1") == "valid:reference-1"
    assert require_aware_time("time", NOW) is NOW
    for value in (None, "", "bad value", "x" * 129):
        with pytest.raises(ValueError):
            require_reference("field", value)
    for value in (None, datetime(2026, 1, 1)):
        with pytest.raises(ValueError):
            require_aware_time("time", value)


def test_source_provenance_has_explicit_private_denial_value():
    assert SourceProvenance.PRIVATE_UNAUTHORIZED.value == "private_unauthorized"
