"""Public failure stages reach the owner without exposing rejected material."""

from dataclasses import replace
import json
import os

import pytest

from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.trusted.worker_telemetry import _diagnostics
from app.owner_control.worker_registry import WorkerRegistry
from app.owner_control.worker_telemetry import WorkerTelemetryClient
from app.worker_telemetry.protocol import (
    DIAGNOSTICS_VERSION,
    decode_diagnostics,
    unavailable,
)
from test_worker_telemetry import attempt, sources
from test_owner_public_runtime import view
import test_finite_public_grant as grant_cases

pending_public = grant_cases.pending_public


@pytest.mark.parametrize(
    "code",
    [
        "research_response_invalid",
        "research_usage_invalid",
        "research_output_truncated",
        "research_output_filtered",
        "research_output_incomplete",
        "research_provenance_invalid",
        "research_citation_budget",
        "research_annotation_budget",
        "research_citation_conflict",
        "research_assessment_json",
        "research_assessment_fields",
        "research_assessment_source",
        "research_source_rejected",
        "research_privacy_rejected",
        "research_assessment_invalid",
        "research_uncited_url",
        "research_language_rejected",
        "research_report_invalid",
        "research_request_invalid",
        "PRIVATE-CANARY-DO-NOT-SHOW",
    ],
)
def test_native_rejection_diagnostic_is_allowlisted_end_to_end(tmp_path, code):
    path, _stop, _socket = sources(tmp_path)
    store = ProviderStateStore(EventStore(path))
    store.record_attempt(
        replace(
            attempt("rejected"),
            outcome="output_gate_rejected"
            if code == "research_language_rejected"
            else "invalid_response",
            provider_error_code=code,
        )
    )
    result = _diagnostics(path)
    assert result["available"]
    row = result["rows"][0]
    assert row["outcome"] == "rejected"
    assert row["reason"] == ("invalid_response" if code.startswith("PRIVATE") else code)
    restored = decode_diagnostics(
        json.dumps({"version": DIAGNOSTICS_VERSION, **result}).encode()
    )
    assert restored == result
    assert "PRIVATE-CANARY" not in json.dumps(restored)


@pytest.mark.parametrize(
    "available,failure", [(False, False), (True, False), (True, True)]
)
def test_registry_observes_each_workers_diagnostics_without_inference(
    tmp_path, monkeypatch, available, failure
):
    registry = WorkerRegistry(
        [
            {
                "name": "public-research",
                "telemetry_socket": str(tmp_path / "status.sock"),
                "stop_socket": str(tmp_path / "stop.sock"),
                "uid": os.getuid(),
            }
        ]
    )
    observed = unavailable() | {"available": available}
    monkeypatch.setattr(WorkerTelemetryClient, "read", lambda self: observed)
    calls = []

    def diagnostics(self):
        calls.append(True)
        if failure:
            raise RuntimeError("PRIVATE-CANARY")
        return {"available": True, "rows": []}

    monkeypatch.setattr(WorkerTelemetryClient, "read_diagnostics", diagnostics)
    result = registry.read()[0]
    assert result["diagnostics"] == {"available": available and not failure, "rows": []}
    assert len(calls) == int(available)


def test_owner_distinguishes_accepted_research_from_uncertain_operations(
    pending_public,
):
    app = pending_public[0]
    with app.journal.transaction() as db:
        for index, phase in enumerate(("completed", "uncertain", "uncertain")):
            db.execute(
                "INSERT INTO public_research_ops VALUES(?,?,?,?,?,?)",
                (
                    f"research-{index}",
                    f"id-{index}",
                    "PRIVATE-CANARY",
                    "digest",
                    phase,
                    "PRIVATE-CANARY",
                ),
            )
    result = view(app).owner_status()
    assert result["research"] == {
        "available": True,
        "completed_reports": 1,
        "uncertain_operations": 2,
        "recorded_operations": 3,
        "failed_operations": 0,
        "held_operations": 0,
        "inflight_operations": 0,
        "allowance_exhausted_operations": 0,
    }
    assert "PRIVATE-CANARY" not in json.dumps(result)
    assert result["billing_authority"] is False


def test_missing_research_store_is_unobserved_not_zero_successes(pending_public):
    app = pending_public[0]
    with app.journal.transaction() as db:
        db.execute("DROP TABLE public_research_ops")
    assert view(app).owner_status()["research"] == {"available": False}
