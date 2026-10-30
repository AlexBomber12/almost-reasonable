"""Offline operator commands never execute acceptance or grant rollout authority."""

import json
from pathlib import Path
import subprocess
import sys
import runpy

import pytest

from app.ops import real_acceptance as cli
from test_real_acceptance import BINDING, NOW, raw, plan, bundle, approval, ci


def protected(tmp_path, name, value):
    tmp_path.chmod(0o700)
    path = tmp_path / name
    path.write_bytes(raw(value))
    path.chmod(0o400)
    return str(path)


def config(tmp_path):
    return protected(
        tmp_path,
        "runtime.json",
        {
            "version": "real-acceptance-runtime-v1",
            "configuration_digest": BINDING["configuration_digest"],
            "model_digest": BINDING["model_digest"],
            "account_digest": BINDING["account_digest"],
        },
    )


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(
        cli,
        "git",
        lambda *args: BINDING["head"] if args == ("rev-parse", "HEAD") else "",
    )
    monkeypatch.setattr(cli, "source_digest", lambda: BINDING["source_digest"])


def test_cli_creates_preregistered_packet_and_reports_missing_as_blocked(
    tmp_path, capsys, frozen
):
    configuration = config(tmp_path)
    assert (
        cli.main(
            [
                "plan",
                "--runtime",
                configuration,
                "--expires-at",
                "2026-10-31T12:00:00+00:00",
            ],
            clock=lambda: NOW,
        )
        == 2
    )
    packet = json.loads(capsys.readouterr().out)
    assert packet["binding"] == BINDING and packet["trial_count"] is None
    plan_path = protected(tmp_path, "plan.json", packet)
    assert (
        cli.main(
            ["report", "--runtime", configuration, "--plan", plan_path],
            clock=lambda: NOW,
        )
        == 2
    )
    report = json.loads(capsys.readouterr().out)
    assert report["complete_mvp"] == "BLOCKED" and report["rollout_authorized"] is False
    assert len(report["scenarios"]) == 12


def test_cli_refuses_world_readable_approval_without_printing_contents(
    tmp_path, capsys, frozen
):
    path = Path(config(tmp_path))
    path.chmod(0o644)
    assert (
        cli.main(
            ["plan", "--runtime", str(path), "--expires-at", "2026-10-31T12:00:00Z"],
            clock=lambda: NOW,
        )
        == 1
    )
    captured = capsys.readouterr()
    assert (
        not captured.out
        and captured.err
        == "Acceptance input rejected; inspect protected files locally.\n"
    )
    assert BINDING["configuration_digest"] not in captured.err


def test_cli_module_entrypoint_has_offline_help():
    result = subprocess.run(
        [sys.executable, "-m", "app.ops.real_acceptance", "--help"],
        capture_output=True,
        text=True,
    )
    assert (
        result.returncode == 0 and "report" in result.stdout and "plan" in result.stdout
    )


def test_module_main_executes_offline_help(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["real_acceptance", "--help"])
    monkeypatch.delitem(sys.modules, "app.ops.real_acceptance")
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("app.ops.real_acceptance", run_name="__main__")
    assert exited.value.code == 0


def test_source_change_during_offline_assessment_cannot_emit_a_report(
    tmp_path, capsys, frozen, monkeypatch
):
    calls = iter([BINDING["source_digest"], "0" * 64])
    monkeypatch.setattr(cli, "source_digest", lambda: next(calls))
    assert (
        cli.main(
            [
                "plan",
                "--runtime",
                config(tmp_path),
                "--expires-at",
                "2026-10-31T12:00:00Z",
            ],
            clock=lambda: NOW,
        )
        == 1
    )
    assert not capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--plan", "unused"],
        ["--approval", "unused"],
        ["--ci", "unused"],
        ["--evidence", "unused"],
    ],
)
def test_plan_cannot_import_evidence_or_omit_expiry(tmp_path, frozen, arguments):
    assert (
        cli.main(["plan", "--runtime", config(tmp_path), *arguments], clock=lambda: NOW)
        == 1
    )


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--expires-at", "2026-10-31T12:00:00Z"],
        ["--trials", "2"],
        ["--calibration-digest", "f" * 64],
    ],
)
def test_report_cannot_change_preregistered_trial_policy(tmp_path, frozen, arguments):
    assert (
        cli.main(
            ["report", "--runtime", config(tmp_path), *arguments], clock=lambda: NOW
        )
        == 1
    )


def test_unknown_runtime_and_oversize_import_list_fail_closed(tmp_path, frozen):
    path = protected(
        tmp_path,
        "unknown.json",
        {
            "version": "unknown",
            "configuration_digest": "c" * 64,
            "model_digest": "d" * 64,
            "account_digest": "e" * 64,
        },
    )
    assert cli.main(["plan", "--runtime", path], clock=lambda: NOW) == 1
    assert (
        cli.main(
            [
                "report",
                "--runtime",
                config(tmp_path),
                "--plan",
                "unused",
                *[arg for _ in range(513) for arg in ("--evidence", "unused")],
            ],
            clock=lambda: NOW,
        )
        == 1
    )


def test_operator_imports_only_protected_hash_approved_metadata(
    tmp_path, capsys, frozen
):
    packet = plan()
    evidence = bundle(packet)  # Fixture-labelled observations remain nonqualifying.
    plan_path = protected(tmp_path, "plan.json", packet)
    policy_path = protected(tmp_path, "approval.json", approval(packet, evidence))
    ci_path = protected(tmp_path, "ci.json", json.loads(ci()))
    evidence_paths = [
        protected(tmp_path, f"receipt-{index}.json", json.loads(item))
        for index, item in enumerate(evidence)
    ]
    args = [
        "report",
        "--runtime",
        config(tmp_path),
        "--plan",
        plan_path,
        "--approval",
        policy_path,
        "--ci",
        ci_path,
        *[arg for path in evidence_paths for arg in ("--evidence", path)],
    ]
    assert cli.main(args, clock=lambda: NOW) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["complete_mvp"] == "BLOCKED" and len(report["nonqualifying"]) == len(
        evidence
    )
