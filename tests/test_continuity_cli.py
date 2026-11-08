"""The separate operator CLI never opens a production application implicitly."""

import json
import runpy
import sys

import pytest

from app.continuity_cli import main
from tests.test_continuity_experiment import decision


def test_offline_cli_round_trip_and_reopen(tmp_path, capsys):
    root = tmp_path / "lab"
    assert main(["create", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["live_calls"] == 0
    assert (
        main(
            [
                "event",
                str(root),
                "post",
                "public",
                "post",
                "Тестовый пост.",
                "--source",
                "operator:synthetic",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["context", str(root), "post"]) == 0
    assert json.loads(capsys.readouterr().out)["trigger"]["id"] == "post"
    response = tmp_path / "response.json"
    response.write_text(json.dumps(decision()), encoding="utf-8")
    assert main(["step", str(root), "turn", "post", "--response", str(response)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["model_kind"] == "recorded"
    assert result["effect"] == "shadow_only"
    assert main(["inspect", str(root), "public"]) == 0
    assert json.loads(capsys.readouterr().out)["human_acceptance"] == "NOT_RUN"
    assert main(["recover", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["interrupted"] == 0


def test_cli_failure_is_sanitized_and_live_needs_explicit_switch(tmp_path, capsys):
    root = tmp_path / "lab"
    main(["create", str(root)])
    main(["event", str(root), "post", "public", "wake", "Шаг"])
    capsys.readouterr()
    assert (
        main(
            [
                "step",
                str(root),
                "live",
                "post",
                "--live-config",
                str(tmp_path / "not-activated"),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"] == "paid_activation_required"
    assert main(["context", str(root), "missing"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "event_missing"
    assert main(["inspect", str(tmp_path / "missing"), "public"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "unavailable"


def test_cli_invalid_recording_becomes_terminal_failure(tmp_path, capsys):
    root = tmp_path / "lab"
    main(["create", str(root)])
    main(["event", str(root), "post", "public", "wake", "Шаг"])
    capsys.readouterr()
    response = tmp_path / "bad.json"
    response.write_text("not json")
    assert main(["step", str(root), "bad", "post", "--response", str(response)]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_output"


def test_cli_live_factory_is_explicit_and_injectable(tmp_path, monkeypatch, capsys):
    from app.continuity_sol import Sol
    from tests.test_continuity_experiment import Model

    root = tmp_path / "lab"
    main(["create", str(root)])
    main(["event", str(root), "post", "public", "wake", "Шаг"])
    capsys.readouterr()
    seen = []

    def factory(config, path):
        seen.append((config, path))
        return Model()

    monkeypatch.setattr(Sol, "from_config", factory)
    assert (
        main(
            [
                "step",
                str(root),
                "live",
                "post",
                "--live-config",
                "activation.json",
                "--allow-paid",
            ]
        )
        == 0
    )
    assert seen == [("activation.json", str(root))]
    capsys.readouterr()


def test_cli_bad_arguments_are_rejected():
    with pytest.raises(SystemExit):
        main(["step", "root", "operation", "event"])


def test_python_module_entrypoint(tmp_path, monkeypatch, capsys):
    monkeypatch.delitem(sys.modules, "app.continuity_cli")
    monkeypatch.setattr(
        sys, "argv", ["continuity_cli", "create", str(tmp_path / "module")]
    )
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("app.continuity_cli", run_name="__main__")
    assert stopped.value.code == 0
    assert json.loads(capsys.readouterr().out)["live_calls"] == 0
