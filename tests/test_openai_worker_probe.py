import os
import runpy
import sys
from threading import Thread

import pytest

from app.ops import openai_worker_probe
from app.trusted.openai_worker_server import build_offline_worker
from tests.test_openai_worker_server import secure_directory


def test_probe_reaches_offline_worker_and_reports_only_disabled_status(
    tmp_path, capsys, monkeypatch
):
    directory = secure_directory(tmp_path)
    socket_path = directory / "worker.sock"
    canary = "TEST-ONLY-OPENAI-CREDENTIAL-CANARY"
    monkeypatch.setenv("OPENAI_API_KEY", canary)
    server = build_offline_worker(
        socket_path, directory / "provider.sqlite3", frozenset((os.getuid(),))
    )
    server.start()
    thread = Thread(target=server.serve_once)
    thread.start()
    try:
        assert openai_worker_probe.main(["--socket", str(socket_path)]) == 0
    finally:
        thread.join(timeout=2)
        server.close()

    output = capsys.readouterr()
    assert output.out == "Stage 0 probe succeeded: paid inference is disabled.\n"
    assert output.err == ""
    assert canary not in output.out
    assert thread.is_alive() is False


def test_probe_failure_is_sanitized_and_nonzero(tmp_path, capsys):
    assert openai_worker_probe.main(["--socket", str(tmp_path / "missing.sock")]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Stage 0 probe failed: disabled outcome was not verified.\n"


def test_probe_builds_fixed_secretless_synthetic_request():
    request = openai_worker_probe.build_synthetic_request()
    structured = request.to_structured_data()

    assert structured["core_self"]["identity"] == "Almost Reasonable"
    assert structured["conversation"] == [
        {
            "role": "participant",
            "trust": "untrusted_conversational_data",
            "content": "Синтетическая проверка изоляции Stage 0.",
        }
    ]
    serialized = repr(structured)
    assert "OPENAI_API_KEY" not in serialized
    assert "credential" not in serialized.lower()


def test_probe_module_entrypoint_returns_failure_for_missing_socket(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        ["openai_worker_probe", "--socket", str(tmp_path / "missing.sock")],
    )
    with pytest.warns(RuntimeWarning), pytest.raises(SystemExit) as captured:
        runpy.run_module("app.ops.openai_worker_probe", run_name="__main__")

    assert captured.value.code == 1
    assert capsys.readouterr().err == (
        "Stage 0 probe failed: disabled outcome was not verified.\n"
    )
