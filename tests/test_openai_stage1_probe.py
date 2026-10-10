import runpy
import sys
from types import SimpleNamespace

import pytest

from app.ops import openai_stage1_probe as probe


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("completed", True),
        ("refused", False),
        ("authorization_rejected", False),
        ("duplicate_operation", False),
        ("outcome_uncertain", False),
    ],
)
def test_probe_reports_only_first_attempt_provider_outcomes(
    tmp_path, monkeypatch, capsys, outcome, expected
):
    calls = []

    class Client:
        def __init__(self, socket_path):
            calls.append(socket_path)

        def infer(self, request, *, operation_id, correlation_id):
            calls.append((request, operation_id, correlation_id))
            validation = SimpleNamespace(valid=True) if outcome == "completed" else None
            return SimpleNamespace(outcome=outcome, attempts=1, validation=validation)

    monkeypatch.setattr(probe, "OpenAIWorkerClient", Client)
    socket_path = tmp_path / "worker.sock"

    assert probe.run_probe(socket_path) is expected
    assert calls[0] == socket_path
    assert calls[1][1] == probe.STAGE1_OPERATION_ID
    assert calls[1][2] == probe.STAGE1_CORRELATION_ID
    assert capsys.readouterr().out == (
        f"Stage 1 sanitized outcome: outcome={outcome} attempts=1\n"
    )


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(
            outcome="completed", attempts=0, validation=SimpleNamespace(valid=True)
        ),
        SimpleNamespace(outcome="completed", attempts=1, validation=None),
        SimpleNamespace(
            outcome="completed", attempts=1, validation=SimpleNamespace(valid=False)
        ),
    ],
)
def test_probe_rejects_inconsistent_completed_result(tmp_path, monkeypatch, result):
    class Client:
        def __init__(self, _socket_path):
            pass

        def infer(self, *_args, **_kwargs):
            return result

    monkeypatch.setattr(probe, "OpenAIWorkerClient", Client)
    assert probe.run_probe(tmp_path / "worker.sock") is False


def test_probe_main_has_distinct_success_and_failure_exit(
    tmp_path, monkeypatch, capsys
):
    socket_path = tmp_path / "worker.sock"
    monkeypatch.setattr(probe, "run_probe", lambda path: path == socket_path)

    assert probe.main(["--socket", str(socket_path)]) == 0
    assert probe.main(["--socket", str(tmp_path / "other.sock")]) == 1
    assert "did not reach" in capsys.readouterr().err


def test_probe_module_entrypoint_is_nonzero_without_a_worker(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        ["openai_stage1_probe", "--socket", str(tmp_path / "missing.sock")],
    )
    with pytest.warns(RuntimeWarning), pytest.raises(SystemExit) as caught:
        runpy.run_module("app.ops.openai_stage1_probe", run_name="__main__")

    assert caught.value.code == 1
    assert "did not reach" in capsys.readouterr().err
