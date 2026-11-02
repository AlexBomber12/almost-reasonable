"""The ordinary lexical policy reaches the SDK without granting sharing authority."""

from dataclasses import replace
import json

import pytest

from app.cognition.ordinary_abstract import _WORDS
from app.cognition.worker_shared import SharedProposalRequest, SharedSuggestion
from app.core.provider_contract import (
    build_worker_shared_request,
    default_openai_provider_config,
)
from app.trusted.openai_worker_protocol import (
    InferenceRequest,
    decode_request,
    encode_request,
)
from test_cognition_pipeline import NOW, principal
from test_worker_shared_proposals import _setup


@pytest.mark.parametrize("enabled", [False, True])
def test_application_policy_survives_ipc_and_reaches_actual_sdk(
    tmp_path, monkeypatch, enabled
):
    _events, _ledger, _gate, shared, runtime, calls = _setup(
        tmp_path, monkeypatch, SharedSuggestion("no_change", None, None, None, None)
    )
    shared._ordinary_enabled = enabled
    runtime.submit(principal("a"), "first", "A private example about doubt.")
    request = next(r for r in calls if type(r) is SharedProposalRequest)
    assert request.ordinary_abstractions is enabled
    wire = encode_request(InferenceRequest("shared-hint", "shared-hint", request))
    restored = decode_request(wire).model_request
    assert restored == request
    sent = build_worker_shared_request(
        restored, default_openai_provider_config()
    ).to_sdk_kwargs()
    assert ("Generality vocabulary" in sent["instructions"]) is enabled
    if enabled:
        assert all(word in sent["instructions"].split() for word in _WORDS)
        assert "never requires a new interest or belief" in sent["instructions"]
    assert shared.context(principal("b")) == ()
    assert sent["tools"] == [] and sent["store"] is False


@pytest.mark.parametrize("invalid", [0, 1, "true", None])
def test_policy_hint_cannot_be_coerced_into_authority(invalid):
    request = SharedProposalRequest("A private example", (), ordinary_abstractions=True)
    with pytest.raises(ValueError):
        replace(request, ordinary_abstractions=invalid).serialize()
    wire = json.loads(encode_request(InferenceRequest("hint", "hint", request)))
    wire["model_request"]["ordinary_abstractions"] = invalid
    with pytest.raises(ValueError):
        decode_request(json.dumps(wire).encode())


def test_old_shared_protocol_remains_identical_when_policy_disabled():
    wire = json.loads(
        encode_request(
            InferenceRequest(
                "old", "old", SharedProposalRequest("A private example", ())
            )
        )
    )
    assert "ordinary_abstractions" not in wire["model_request"]
    assert (
        decode_request(json.dumps(wire).encode()).model_request.ordinary_abstractions
        is False
    )


@pytest.mark.parametrize("marker", [False, True])
def test_rejected_ordinary_idea_retains_only_a_fixed_generality_diagnostic(
    tmp_path, marker
):
    from app.cognition.shared import SharedIntelligence
    from test_reviewed_shared import candidate
    from test_shared_intelligence import shared as shared_fixture
    from test_event_store import private_input
    from app.core.events import EventAudience

    events, _ledger, gate, source, _ = shared_fixture.__wrapped__(tmp_path)
    intelligence = SharedIntelligence(
        events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
    )
    original_source = source
    if marker:
        source = "uncertainty"
        events.append_private_input(
            principal("a"),
            replace(
                private_input("marker", "A synthetic unrelated experience."),
                event_id=source,
                audience=EventAudience.private_for(principal("a").principal_id),
            ),
        )
        first = candidate(source, value="Compare causal models.")
        intelligence.transition(first)
        item = replace(
            first,
            expected_revision=1,
            proposal=replace(
                first.proposal,
                operation_id="marker-second",
                protected_source_event_ids=(original_source,),
                candidate=replace(
                    first.proposal.candidate, value="Consider causal uncertainty."
                ),
            ),
        )
    else:
        item = candidate(source, value="Consider astrophysics hypotheses.")
    with pytest.raises(ValueError, match="privacy screen"):
        intelligence.transition(item)
    with events._open() as db:
        assert db.execute(
            "SELECT reason,screen_result FROM shared_operations ORDER BY rowid DESC LIMIT 1"
        ).fetchone() == (
            "privacy_rejected",
            "protected_source_marker" if marker else "ordinary_generality_uncertain",
        )
        assert db.execute(
            "SELECT value FROM shared_payloads ORDER BY rowid DESC LIMIT 1"
        ).fetchone() == ("Screened abstract candidate withheld.",)
    visible = intelligence.context(principal("b"))
    assert len(visible) == int(marker)
    if marker:
        assert visible[0].text == "Compare causal models."
