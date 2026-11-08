"""Budget authority must be isolated from providers and participant histories."""

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_broker_has_no_egress_credentials_participant_state_or_privilege():
    text = (ROOT / "ops/compose.shared-budget.yaml").read_text()
    assert "network_mode: none" in text
    assert "read_only: true" in text
    assert "no-new-privileges:true" in text
    assert "cap_drop: [ALL]" in text
    assert "create_host_path: false" in text
    for forbidden in (
        "OPENAI_API_KEY",
        "/run/secrets",
        "/var/lib/pilot-app",
        "privileged:",
        "docker.sock",
        "ports:",
    ):
        assert forbidden not in text
    for lane in ("private_sol", "sol", "research"):
        assert ":" + lane in text
    assert "SHARED_BUDGET_IMAGE:?" in text
