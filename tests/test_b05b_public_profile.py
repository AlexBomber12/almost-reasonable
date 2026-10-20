"""Public catalog authority is distinct from private conversation authority."""

from datetime import UTC, datetime, timedelta
import json
import os

import pytest

from app.core.public_behavior import PublicBehaviorRequest, decode_public_request
from app.core.public_research_profile import (
    PUBLIC_PURPOSE,
    load_research_profile,
    provider_configuration,
)


def test_public_profile_requires_account_and_public_purpose(tmp_path):
    now = datetime(2030, 1, 1, tzinfo=UTC)
    value = {
        "version": "public-research-profile-v1",
        "role": "application",
        "purpose": PUBLIC_PURPOSE,
        "configuration": "public-research-v1",
        "provider_configuration": provider_configuration(),
        "profile_id": "a" * 64,
        "authorization": "b" * 64,
        "application_scope": "public-test",
        "account_ref": "account-1",
        "peer_uid": os.geteuid(),
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "reviewer": "test-operator",
        "reviewed_at": now.isoformat(),
        "aggregate_limit": 1,
        "topics": [
            {"id": "ocean", "question": "How do ocean currents form?", "limit": 1}
        ],
    }
    path = tmp_path / "profile.json"

    def load(data):
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return load_research_profile(path, role="application")

    profile = load(value)
    assert profile.account_ref == "account-1"
    assert profile.purpose == PUBLIC_PURPOSE
    for invalid in (
        [],
        value | {"purpose": "private-conversation-public-research-v1"},
        value | {"account_ref": ""},
        value | {"principal_id": "private-person"},
        value | {"topics": [{"id": "ocean", "question": "x" * 513, "limit": 1}]},
    ):
        with pytest.raises(ValueError):
            load(invalid)


def test_public_worker_catalog_frame_is_bounded_and_closed():
    request = PublicBehaviorRequest(
        "operation-1",
        "operation-1",
        "autonomous_wakeup",
        (),
        (),
        5,
        catalog=(("ocean", "How do ocean currents form?"),),
        evidence=("Untrusted public finding",),
    )
    assert decode_public_request(request.encode()) == request
    from dataclasses import replace

    for catalog in (
        (("bad choice", "question"),),
        (("ocean", "question"), ("ocean", "another")),
        (("ocean", "x" * 513),),
    ):
        with pytest.raises(ValueError):
            replace(request, catalog=catalog).encode()


def test_public_model_selection_requires_exact_catalog_id():
    from app.trusted.public_behavior_worker import PublicSDKAdapter
    from test_int24_worker_boundaries import SDK

    request = PublicBehaviorRequest(
        "operation-1",
        "operation-1",
        "autonomous_wakeup",
        (),
        (),
        5,
        catalog=(("ocean", "How do ocean currents form?"),),
    )
    base = {
        "decision": "research",
        "content": None,
        "draft": None,
        "reason_category": None,
        "topic_id": "ocean",
    }
    assert PublicSDKAdapter(SDK(json.dumps(base)), model="fake").complete(request) == {
        "decision": "research",
        "brief": "ocean",
    }
    for invalid in (
        base | {"topic_id": "ocean: extra"},
        base | {"topic_id": "How do ocean currents form?"},
        base | {"authorization": "forged"},
        base | {"draft": "extra"},
    ):
        with pytest.raises(ValueError):
            PublicSDKAdapter(SDK(json.dumps(invalid)), model="fake").complete(request)
