"""Offline Sol request bounds and profiles that cannot activate a public worker."""

import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from app.core.public_behavior import PublicBehaviorRequest
from app.ops import int24_public_worker, int24_threads_runtime
from app.trusted.public_behavior_worker import PUBLIC_PROPOSAL_FORMAT, PublicSDKAdapter
from test_int24_worker_boundaries import SDK, GOOD


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-luna"])
def test_public_sol_request_is_bounded_without_tools_storage_or_extra_calls(model):
    sdk = SDK(
        json.dumps(
            {
                "decision": "no_action",
                "content": None,
                "draft": None,
                "reason_category": "other",
            }
        )
    )
    request = PublicBehaviorRequest(
        "synthetic-sol-operation",
        "synthetic-sol-operation",
        "autonomous_wakeup",
        (),
        (),
        5,
    )
    assert PublicSDKAdapter(sdk, model=model).complete(request) == GOOD
    assert sdk.calls == 1 and sdk.kwargs["model"] == model
    assert sdk.kwargs["timeout"] == 5
    assert sdk.kwargs["max_output_tokens"] == (2048 if model == "gpt-6.1-sol" else 512)
    if model == "gpt-6.1-sol":
        assert sdk.kwargs["reasoning"] == {"effort": "low"}
    else:
        assert "reasoning" not in sdk.kwargs
    assert sdk.kwargs["tools"] == [] and sdk.kwargs["store"] is False
    assert sdk.kwargs["text"] == {"format": PUBLIC_PROPOSAL_FORMAT}
    assert (
        not {"temperature", "top_p", "previous_response_id", "conversation"}
        & sdk.kwargs.keys()
    )


def candidate(tmp_path, name):
    directory = tmp_path / "protected"
    directory.mkdir(mode=0o700)
    path = directory / name
    path.write_bytes((ROOT / "ops" / name).read_bytes())
    path.chmod(0o400)
    return path


@pytest.mark.parametrize("toggle_enabled", [False, True])
def test_prepared_worker_profile_cannot_read_credentials_or_start(
    tmp_path, monkeypatch, toggle_enabled
):
    path = candidate(tmp_path, "public-sol-worker.disabled.json")
    value = json.loads(path.read_text())
    assert value == {"worker_enabled": False, "model": "gpt-6.1-sol"}
    if toggle_enabled:
        value["worker_enabled"] = True
        path.chmod(0o600)
        path.write_text(json.dumps(value))
        path.chmod(0o400)
    original = int24_public_worker.read_protected_worker_file

    def configuration_only(selected, **kwargs):
        assert selected == path, "credential access attempted"
        return original(selected, **kwargs)

    def forbidden(*_args, **_kwargs):
        pytest.fail("SDK construction or worker startup attempted")

    monkeypatch.setattr(
        int24_public_worker, "read_protected_worker_file", configuration_only
    )
    monkeypatch.setattr(int24_public_worker, "PublicWorkerServer", forbidden)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=forbidden))
    with pytest.raises(
        ValueError, match="billing scope" if toggle_enabled else "disabled"
    ):
        int24_public_worker.main(["--config", str(path)])
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_prepared_operator_profile_rejects_even_explicit_live_flag(
    tmp_path, monkeypatch
):
    path = candidate(tmp_path, "public-sol-operator.disabled.json")
    assert json.loads(path.read_text())["activation_reviewed"] is False

    def forbidden(*_args, **_kwargs):
        pytest.fail("runtime construction or HTTP attempted")

    monkeypatch.setattr(int24_threads_runtime, "_runtime", forbidden)
    monkeypatch.setattr(int24_threads_runtime, "HTTPSClient", forbidden)
    with pytest.raises(ValueError, match="reviewed activation is absent"):
        int24_threads_runtime.main(
            ["readiness", "--activate-live", "--config", str(path)]
        )
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]
