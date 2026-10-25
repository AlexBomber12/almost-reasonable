"""CI-only collection validation and payload-free actual pytest outcomes."""

import json
import os
from pathlib import Path

import pytest

from acceptance.manifest import load_manifest, validate_manifest

_RESULTS = {}
_COLLECTED = []


def pytest_collection_finish(session):
    global _COLLECTED
    _COLLECTED = [item.nodeid for item in session.items]
    if not session.config.option.collectonly:
        try:
            validate_manifest(load_manifest(), set(_COLLECTED))
        except ValueError as error:
            raise pytest.UsageError(str(error)) from error


def pytest_runtest_logreport(report):
    phases = _RESULTS.setdefault(report.nodeid, {})
    phases[report.when] = report.outcome


def pytest_sessionfinish(session, exitstatus):
    output = os.environ.get("INT10_TEST_RESULTS")
    if output:
        Path(output).write_text(
            json.dumps(
                {
                    "exit_code": int(exitstatus),
                    "collected": _COLLECTED,
                    "results": _RESULTS,
                },
                sort_keys=True,
            )
        )


@pytest.fixture(autouse=True)
def acceptance_offline(request, monkeypatch):
    from acceptance.guards import install, install_spawn_guard

    mapped = {
        node
        for row in load_manifest()["scenarios"]
        for item in row["evidence"]
        for node in item["node_ids"]
    }
    if request.node.nodeid in mapped:
        install(
            monkeypatch,
            local_ipc=request.node.nodeid.startswith(
                (
                    "tests/test_research_worker.py::",
                    "tests/test_private_research_bridge.py::",
                    "tests/test_int19_worker_runtime.py::",
                    "tests/test_int20_public_research.py::",
                    "tests/test_int22_rehearsal.py::",
                    "tests/test_combined_narrative_memory.py::",
                    "tests/test_private_memory_journey.py::",
                    "tests/test_int23_connected.py::test_authenticated_wsgi_two_workers_and_durable_reply",
                    "tests/test_int23_controls.py::test_unapproved_question_never_crosses_boundary",
                    "tests/test_int23_controls.py::test_stop_and_spend_block_contact",
                    "tests/test_int23_controls.py::test_lost_or_crashed_attempt_is_not_retried[",
                    "tests/test_int24_runtime.py::",
                    "tests/test_int24_runtime_guards.py::",
                    "tests/test_int24_worker_boundaries.py::",
                    "tests/test_int25_connected.py::test_private_research_public_draft_stop_and_restart_replay",
                    "tests/test_cross_channel_adversarial.py::test_cross_channel_research_memory_mission_and_public_journey[",
                    "tests/test_cross_channel_adversarial.py::test_scn12_parentless_provenance_requires_contextual_binding",
                )
            ),
        )
        if request.node.nodeid == (
            "tests/test_int23_connected.py::"
            "test_authenticated_wsgi_two_workers_and_durable_reply"
        ):
            install_spawn_guard(monkeypatch)
