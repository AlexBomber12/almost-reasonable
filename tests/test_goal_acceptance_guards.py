"""Mapped connected regressions need Unix IPC while live dependencies stay blocked."""

from pathlib import Path
from types import SimpleNamespace
import http.client
import socket
import urllib.request

import pytest

from acceptance import plugin


@pytest.mark.parametrize(
    "node",
    [
        "tests/test_private_worker_serialization.py::test_private_turns_share_one_worker_and_recheck_stop_after_wait[False-False]",
        "tests/test_b11c_window.py::test_window_discovers_comments_on_a_later_confirmed_own_post",
        "tests/test_goal_completion_journeys.py::test_new_owned_discussion_reply_and_reason_reach_both_private_chats_after_restart",
        "tests/test_historical_public_context.py::test_three_public_revisions_keep_complete_distinct_private_context_receipts",
        "tests/test_public_dialogue_continuity.py::test_nested_response_inherits_own_parent_ancestry_and_withdrawal[False]",
        "tests/test_intellectual_recorded_reasons.py::test_retaining_fields_with_new_evidence_preserves_the_new_recorded_reason",
        "tests/test_deferred_memory_maintenance.py::test_response_precedes_auxiliary_inference_and_next_turn_uses_persisted_memory",
        "tests/test_owner_public_development.py::test_owner_distinguishes_null_proposals_from_rejected_and_recorded_changes",
        "tests/test_private_request_timing.py::test_request_timings_separate_model_context_and_history_without_payload[False]",
        "tests/test_private_turn_delivery.py::test_a_page_bound_to_another_account_cannot_read_or_submit_in_the_new_session",
        "tests/test_private_turn_browser.py::test_old_history_cannot_overwrite_a_later_completed_post[True]",
    ],
)
def test_mapped_goal_regressions_allow_only_local_unix_ipc(monkeypatch, node):
    request = SimpleNamespace(node=SimpleNamespace(nodeid=node))
    plugin.acceptance_offline.__wrapped__(request, monkeypatch)
    with socket.socket(socket.AF_UNIX):
        pass
    for forbidden in (
        lambda: socket.socket(socket.AF_INET),
        lambda: socket.socket(socket.AF_INET6),
        lambda: socket.getaddrinfo("example.invalid", 443),
        lambda: http.client.HTTPSConnection("example.invalid").connect(),
        lambda: urllib.request.urlopen("https://example.invalid"),
    ):
        with pytest.raises(AssertionError, match="acceptance forbids"):
            forbidden()
    from app.trusted.openai_transport import OpenAIResponsesTransport
    from app.trusted.openai_credential_files import read_protected_worker_file

    with pytest.raises(AssertionError, match="acceptance forbids"):
        OpenAIResponsesTransport.from_environment()
    with pytest.raises(AssertionError, match="acceptance forbids"):
        read_protected_worker_file(Path("/synthetic/credential"), maximum_bytes=100)


def test_pure_mapped_scenario_keeps_its_no_socket_guard(monkeypatch):
    request = SimpleNamespace(
        node=SimpleNamespace(
            nodeid=(
                "tests/test_stage0_acceptance_journeys.py::"
                "test_authenticated_memory_shared_restart_delete_and_old_quarantine"
            )
        )
    )
    plugin.acceptance_offline.__wrapped__(request, monkeypatch)
    with pytest.raises(AssertionError, match="acceptance forbids"):
        socket.socket(socket.AF_UNIX)
