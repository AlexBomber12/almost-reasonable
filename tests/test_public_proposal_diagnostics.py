"""Real public rejection categories are bounded observations, never model text."""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from app.core.public_behavior import PublicBehaviorRequest
from app.trusted.public_behavior_worker import PublicSDKAdapter
from app.trusted.worker_telemetry import _diagnostics
from app.worker_telemetry.protocol import DIAGNOSTICS_VERSION, decode_diagnostics
from test_int24_runtime import setup, run_once
from test_worker_telemetry import attempt, sources
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore

CANARY = "PRIVATE-CANARY-MUST-NOT-BE-A-DIAGNOSTIC"
GOOD = {
    "decision": "no_action",
    "content": None,
    "draft": None,
    "reason_category": "other",
    "topic_id": None,
}


class RawSDK:
    def __init__(self, value=GOOD, **changes):
        self.responses = self
        self.calls = 0
        self.response = SimpleNamespace(
            status="completed",
            output=(),
            output_text=json.dumps(value),
            id="resp-synthetic",
            usage=SimpleNamespace(input_tokens=3, output_tokens=1, total_tokens=4),
        )
        self.response.__dict__.update(changes)

    def create(self, **_kwargs):
        self.calls += 1
        return self.response


@pytest.mark.parametrize(
    "stage",
    [
        "usage",
        "incomplete",
        "refusal",
        "output",
        "json",
        "proposal",
        "mission",
        "research",
    ],
)
def test_adapter_classifies_local_rejection_without_retaining_rejected_text(stage):
    request = PublicBehaviorRequest(
        "operation-1", "operation-1", "autonomous_wakeup", (), (), 5
    )
    sdk = RawSDK()
    code = {
        "usage": "public_usage_invalid",
        "incomplete": "public_response_incomplete",
        "refusal": "public_response_refused",
        "output": "public_output_invalid",
        "json": "public_json_invalid",
        "proposal": "public_proposal_invalid",
        "mission": "public_mission_invalid",
        "research": "public_research_invalid",
    }[stage]
    if stage == "usage":
        sdk.response.usage.input_tokens = True
    elif stage == "incomplete":
        sdk.response.status = "incomplete"
    elif stage == "refusal":
        sdk.response.output = [
            SimpleNamespace(content=[SimpleNamespace(type="refusal", refusal=CANARY)])
        ]
    elif stage == "output":
        sdk.response.output_text = CANARY * 200
    elif stage == "json":
        sdk.response.output_text = "{" + CANARY
    elif stage == "proposal":
        sdk.response.output_text = json.dumps({**GOOD, "content": CANARY})
    elif stage == "mission":
        request = replace(
            request, mission_evolution=(("mission_version", "synthetic"),)
        )
        sdk.response.output_text = json.dumps(
            {**GOOD, "mission_action": "propose", "mission_purpose": "general_inquiry"}
        )
    elif stage == "research":
        sdk.response.output_text = json.dumps(
            {
                **GOOD,
                "decision": "research",
                "reason_category": None,
                "topic_id": CANARY,
            }
        )
    adapter = PublicSDKAdapter(sdk, model="reviewed-model")
    with pytest.raises(ValueError) as raised:
        adapter.complete(request)
    assert getattr(raised.value, "code", None) == code
    assert CANARY not in str(raised.value)
    assert sdk.calls == 1
    if stage != "usage":
        assert adapter.last_usage == {
            "input_tokens": 3,
            "output_tokens": 1,
            "total_tokens": 4,
        }


@pytest.mark.parametrize(
    "code",
    [
        None,
        "PRIVATE-CANARY-MUST-NOT-BE-A-DIAGNOSTIC",
        "public_usage_invalid",
        "public_response_incomplete",
        "public_response_refused",
        "public_output_invalid",
        "public_json_invalid",
        "public_proposal_invalid",
        "public_mission_invalid",
        "public_research_invalid",
    ],
)
def test_public_provider_error_has_a_real_safe_owner_category(tmp_path, code):
    _source, _stop, _socket = sources(tmp_path)
    provider = ProviderStateStore(EventStore(tmp_path / "events.sqlite3"))
    provider.record_attempt(
        replace(
            attempt("diagnostic"), outcome="provider_error", provider_error_code=code
        )
    )
    diagnostic = _diagnostics(tmp_path / "events.sqlite3")
    row = diagnostic["rows"][0]
    known = code is not None and code.startswith("public_")
    assert row["outcome"] == ("rejected" if known else "uncertain")
    assert row["reason"] == (code if known else "provider_error")
    assert CANARY not in json.dumps(diagnostic)
    assert (
        decode_diagnostics(
            json.dumps({"version": DIAGNOSTICS_VERSION, **diagnostic}).encode()
        )
        == diagnostic
    )


def test_connected_public_failure_preserves_single_attempt_and_safe_stage(tmp_path):
    app, worker, sdk, http, _clock = setup(
        tmp_path,
        {"decision": "no_action", "reason_category": "other", "content": CANARY},
    )
    try:
        run_once(app, worker)
        assert len(sdk.calls) == worker.provider_store.usage_attempt_count() == 1
        assert not http.calls
        with worker.provider_store._event_store._open() as db:
            row = db.execute(
                "SELECT outcome,provider_error_code FROM provider_usage_attempts"
            ).fetchone()
        assert tuple(row) == ("provider_error", "public_proposal_invalid")
        path = worker.provider_store._event_store.database_path
        diagnostic = _diagnostics(path)
        assert diagnostic["rows"][0]["reason"] == "public_proposal_invalid"
        assert CANARY not in json.dumps(diagnostic)
    finally:
        worker.close()


@pytest.mark.parametrize("code", [None, [], "private-arbitrary-code"])
def test_public_rejection_code_is_closed(code):
    from app.core.public_rejection import PublicOutputRejected

    with pytest.raises(ValueError, match="unsupported public rejection code"):
        PublicOutputRejected(code, "synthetic message")
