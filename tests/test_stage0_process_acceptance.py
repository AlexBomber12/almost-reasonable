"""Real process-exit acceptance at durable private and publication boundaries."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from acceptance.guards import install
from app.core.store import EventStore
from app.private_web.auth import AuthStore
from app.private_web.runtime import Stage0ConversationRuntime
from app.private_web.wsgi import PrivateWebApplication, PrivateWebConfig
from app.privacy.application import PrivacyReadGate
from app.privacy.ledger import PrivacyLedger
from app.trusted.openai_transport import FakeOpenAITransport
from test_private_conversation_runtime import response
from test_private_web_http import Browser, Clock


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    install(monkeypatch)


def crash_child(directory, boundary):
    import os
    from test_stage0_acceptance_journeys import journey
    from test_threads_stage0_integration import setup, ready

    # Spawned interpreters install their own guards; no reliance on parent patches.
    guard = pytest.MonkeyPatch()
    install(guard)
    path = Path(directory)
    if boundary.startswith("private:"):
        point = boundary.split(":")[1]
        clock, auth, events, _, gate, fake, config, a, _ = journey.__wrapped__(path)

        def checkpoint(current):
            if current == point:
                os._exit(23)

        a.app = PrivateWebApplication(
            auth,
            Stage0ConversationRuntime(
                events, gate, fake, clock=clock, checkpoint=checkpoint
            ),
            config,
        )
        a.submit("process-crash", "Синтетический запрос CANARY-PROCESS-10")
    else:
        point = boundary.split(":")[1]
        from test_threads_stage0_integration import DRAFT

        app, _, _, operator = setup(
            path, [{"decision": "propose_publication", "draft": DRAFT}]
        )
        operation, approval = ready(app, operator)
        original = app.service.store.update_sending_evidence

        def evidence(operation, marker, **kwargs):
            result = original(operation, marker, **kwargs)
            if marker == point:
                os._exit(23)
            return result

        app.service.store.update_sending_evidence = evidence
        app.service.dispatch(operation, approval, now=app.clock.now())
    raise AssertionError("exit checkpoint was not reached")


def execute_child(tmp_path, boundary):
    code = "import sys; sys.path.insert(0, 'tests'); from test_stage0_process_acceptance import crash_child; crash_child(sys.argv[1], sys.argv[2])"
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), boundary],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 23, result.stderr.decode()


@pytest.mark.parametrize(
    "point,state",
    [
        ("after_reservation", "pending"),
        ("after_model", "pending"),
        ("after_reply_commit", "completed"),
        ("before_release", "completed"),
    ],
)
def test_real_private_process_exit_preserves_operation_without_model_replay(
    tmp_path, point, state
):
    execute_child(tmp_path, "private:" + point)
    clock = Clock(1_791_806_400)
    auth = AuthStore(tmp_path / "auth.db", clock=clock)
    events = EventStore(tmp_path / "events.db")
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, events)
    assert gate.reconcile()
    fake = FakeOpenAITransport([response()])
    runtime = Stage0ConversationRuntime(events, gate, fake, clock=clock)
    app = PrivateWebApplication(
        auth,
        runtime,
        PrivateWebConfig(frozenset({"chat.test"}), frozenset({"https://chat.test"})),
    )
    a = Browser(app)
    assert a.login("a", "synthetic-password-a")[0] == "303 See Other"
    status, _, body = a.submit(
        "process-crash", "Синтетический запрос CANARY-PROCESS-10"
    )
    assert status == "200 OK"
    data = json.loads(body)
    assert data["message_status"] == "duplicate"
    assert data["model_state"] == state
    assert (data["response"] is not None) == (state == "completed")
    assert fake.calls == []
    with events._open() as db:
        assert db.execute("SELECT count(*) FROM agent_reply_operations").fetchone() == (
            1,
        )
        assert db.execute("SELECT count(*) FROM event_envelopes").fetchone() == (1,)


@pytest.mark.parametrize(
    "marker,expected",
    [
        ("container_dispatch_started", "unknown"),
        ("container_created_publish_not_started", "ready"),
        ("publish_dispatch_started", "unknown"),
    ],
)
def test_real_publication_process_exit_uses_durable_evidence_without_resend(
    tmp_path, marker, expected
):
    from test_threads_stage0_integration import setup

    execute_child(tmp_path, "threads:" + marker)
    app, model, _, _ = setup(tmp_path, [])
    operation = app.wakeup_operation("wake-1")
    assert app.service.store.get(operation).state.value == "sending"
    assert (
        app.service.recover_sending(operation, now=app.clock.now()).state.value
        == expected
    )
    app.recover()
    assert app.journal.read(operation)["phase"] == "linked"
    assert model.requests == []
    assert app.simulator.trace == []
    assert app.simulator.network_calls == 0
    assert app.service.store.get(operation).operation_id == operation


def deletion_child(directory, phase):
    import os
    from test_stage0_acceptance_journeys import journey, submitted, trusted
    from app.privacy.application import EventStoreErasureAdapter
    from app.privacy.deletion import DeletionCoordinator
    from test_privacy_deletion import context, request

    guard = pytest.MonkeyPatch()
    install(guard)
    clock, auth, events, ledger, gate, _, _, a, _ = journey.__wrapped__(Path(directory))
    submitted(a, "delete-source", "CANARY-PROCESS-DELETE-10")
    source = gate.retrieve_private_events(trusted(auth, a))[0].event_id
    adapter = EventStoreErasureAdapter(events)
    original = adapter.erase

    def erase(req):
        if phase == "after_application":
            original(req)
        os._exit(23)

    adapter.erase = erase
    DeletionCoordinator(ledger, adapter).delete(
        context(), request(content_reference=source)
    )
    raise AssertionError("deletion checkpoint not reached")


@pytest.mark.parametrize("phase", ["after_intent", "after_application"])
def test_real_deletion_process_exit_replays_pending_ledger_before_reads(
    tmp_path, phase
):
    from app.privacy.application import PrivacyReadBlocked
    from test_private_web_conversation import principal

    code = "import sys; sys.path.insert(0, 'tests'); from test_stage0_process_acceptance import deletion_child; deletion_child(sys.argv[1], sys.argv[2])"
    process = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), phase],
        capture_output=True,
        timeout=15,
    )
    assert process.returncode == 23, process.stderr.decode()
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    events = EventStore(tmp_path / "events.db")
    pending = ledger.deletions_for_reconciliation()
    assert len(pending) == 1 and pending[0].disposition.value == "pending"
    gate = PrivacyReadGate(ledger, events)
    with pytest.raises(PrivacyReadBlocked):
        gate.retrieve_private_events(principal("a"))
    assert gate.reconcile()
    assert gate.retrieve_private_events(principal("a")) == ()
    assert ledger.deletions_for_reconciliation()[0].disposition.value == "applied"
    with events._open() as db:
        assert "CANARY-PROCESS-DELETE-10" not in "\n".join(db.iterdump())
