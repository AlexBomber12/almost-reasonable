"""A real single-active IPC worker must not receive overlapping private turns."""

import json
from threading import Event, Thread
from time import monotonic, sleep

import pytest

from acceptance.guards import install
from app.privacy.application import PrivacyReadBlocked
from test_agent_intellectual_state import private_setup
from test_cognition_pipeline import principal


def wait_for_two_turns(runtime, completed):
    deadline = monotonic() + 3
    while monotonic() < deadline:
        with runtime._foreground_lock:
            if runtime._foreground_active == 2:
                return
        assert not completed.is_set(), "second turn finished while worker was busy"
        sleep(0.001)
    raise AssertionError("second turn did not reach the application")


@pytest.mark.parametrize("stop_while_queued", [False, True])
@pytest.mark.parametrize("same_message", [False, True])
def test_private_turns_share_one_worker_and_recheck_stop_after_wait(
    tmp_path, monkeypatch, stop_while_queued, same_message
):
    install(monkeypatch, local_ipc=True)
    app, a, b, _events, _ledger, _gate, sdk, server = private_setup(tmp_path)
    entered, release, stop, completed = Event(), Event(), Event(), Event()
    stopped = {"value": False}
    app._conversations._stop = lambda: stopped["value"]
    results = {}

    def hold():
        if len(sdk.calls) == 1:
            entered.set()
            assert release.wait(10)

    sdk.before_result = hold
    worker = Thread(target=lambda: server.serve(should_stop=stop.is_set))
    first = Thread(
        target=lambda: results.update(a=a.submit("queue-a", "QUEUE_A_CANARY"))
    )

    def second():
        try:
            results["b"] = (
                a.submit("queue-a", "QUEUE_A_CANARY")
                if same_message
                else b.submit("queue-b", "Обсудим основания.")
            )
        finally:
            completed.set()

    other = Thread(target=second)
    worker.start()
    first.start()
    try:
        assert entered.wait(5)
        other.start()
        wait_for_two_turns(app._conversations, completed)
        assert not completed.wait(0.25)
        assert len(sdk.calls) == 1
        stopped["value"] = stop_while_queued
    finally:
        release.set()
        first.join(5)
        if other.ident is not None:
            other.join(5)
        stop.set()
        worker.join(5)
        server.close()
    assert not first.is_alive() and not other.is_alive() and not worker.is_alive()
    assert results["b"][0] == ("200 OK" if same_message else "201 Created")
    assert json.loads(results["b"][2])["model_state"] == (
        "stopped" if stop_while_queued else "completed"
    )
    assert len(sdk.calls) == (1 if stop_while_queued or same_message else 2)
    if not stop_while_queued and not same_message:
        assert "QUEUE_A_CANARY" not in sdk.calls[-1]["input"]


def test_revoked_queued_request_is_rejected_before_input_persistence(
    tmp_path, monkeypatch
):
    install(monkeypatch, local_ipc=True)
    app, a, _b, events, _ledger, _gate, sdk, server = private_setup(tmp_path)
    runtime = app._conversations
    entered, release, stop, completed = Event(), Event(), Event(), Event()
    enabled = {"value": True}
    errors = []

    def hold():
        entered.set()
        assert release.wait(10)

    sdk.before_result = hold
    worker = Thread(target=lambda: server.serve(should_stop=stop.is_set))
    first = Thread(target=lambda: a.submit("active", "Обсудим объяснения."))

    def second():
        try:
            with runtime.authenticated_request(lambda: enabled["value"]):
                runtime.submit(
                    principal("b"), "queued-revoked", "PRIVATE_QUEUED_CANARY"
                )
        except PrivacyReadBlocked:
            errors.append("authority_unavailable")
        finally:
            completed.set()

    other = Thread(target=second)
    worker.start()
    first.start()
    try:
        assert entered.wait(5)
        other.start()
        wait_for_two_turns(runtime, completed)
        assert not completed.wait(0.25)
        enabled["value"] = False
    finally:
        release.set()
        first.join(5)
        if other.ident is not None:
            other.join(5)
        stop.set()
        worker.join(5)
        server.close()
    assert not first.is_alive() and not other.is_alive() and not worker.is_alive()
    assert errors == ["authority_unavailable"] and len(sdk.calls) == 1
    with events._open() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM event_envelopes WHERE source_message_id='queued-revoked'"
            ).fetchone()[0]
            == 0
        )


def test_narrative_reconciliation_without_a_reply_cannot_bypass_busy_inference_lane(
    tmp_path, monkeypatch
):
    import os

    from app.core.agent_proposals import ProposalValidation, ReplyProposal
    from app.core.openai_worker_client import ClientResult, OpenAIWorkerClient
    from app.core.provider_spend import ProviderStateStore
    from app.core.store import EventStore
    from app.private_web.narrative_audit import NarrativeAuditStore
    from app.private_web.runtime import WorkerConversationRuntime
    from app.privacy.application import PrivacyReadGate
    from app.privacy.ledger import PrivacyLedger

    install(monkeypatch, local_ipc=True)
    events = EventStore(tmp_path / "events.db")
    gate = PrivacyReadGate(PrivacyLedger(tmp_path / "ledger.db"), events)
    assert gate.reconcile()
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    client = OpenAIWorkerClient(
        tmp_path / "missing.sock", expected_worker_uid=os.getuid()
    )

    def build():
        return WorkerConversationRuntime(
            events,
            gate,
            client,
            provider_state=provider,
            narrative_enabled=True,
            narrative_permission_current=lambda: True,
        )

    runtime, other_runtime = build(), build()
    active, reconciling, release_active, release_reconciliation, completed = (
        Event(),
        Event(),
        Event(),
        Event(),
        Event(),
    )
    calls, results = [], []

    def infer(_client, _request, *, correlation_id, operation_id):
        calls.append(operation_id)
        if len(calls) == 1:
            active.set()
            assert release_active.wait(10)
        return ClientResult(
            operation_id,
            correlation_id,
            "completed",
            1,
            None,
            validation=ProposalValidation(ReplyProposal("Синтетический ответ."), None),
        )

    original_read = NarrativeAuditStore.read

    def read(audit, operation_id, participant_id, event_id, content):
        if (
            participant_id == principal("b").principal_id.value
            and not reconciling.is_set()
        ):
            reconciling.set()
            assert release_reconciliation.wait(10)
        return original_read(audit, operation_id, participant_id, event_id, content)

    monkeypatch.setattr(OpenAIWorkerClient, "infer", infer)
    monkeypatch.setattr(NarrativeAuditStore, "read", read)

    def submit(app, participant, message_id, *, duplicate=False):
        try:
            results.append(
                app.submit(principal(participant), message_id, "Обсудим основания.")
            )
        finally:
            if duplicate:
                completed.set()

    first = Thread(target=lambda: submit(runtime, "a", "active"))
    other = Thread(target=lambda: submit(other_runtime, "b", "unreserved"))
    duplicate = Thread(
        target=lambda: submit(runtime, "b", "unreserved", duplicate=True)
    )
    first.start()
    try:
        assert active.wait(5)
        other.start()
        assert reconciling.wait(5)
        duplicate.start()
        wait_for_two_turns(runtime, completed)
        assert not completed.wait(0.1)
        assert len(calls) == 1
        release_active.set()
        first.join(5)
        duplicate.join(5)
        assert completed.is_set() and len(calls) == 2
    finally:
        release_active.set()
        release_reconciliation.set()
        first.join(5)
        if other.ident is not None:
            other.join(5)
        if duplicate.ident is not None:
            duplicate.join(5)
    assert not any(thread.is_alive() for thread in (first, other, duplicate))
    assert len(results) == 3 and len(calls) == 2
