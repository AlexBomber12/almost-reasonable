"""Actual Stage 0 runtime with synthetic principals and offline provider fixtures."""

from dataclasses import replace
from datetime import UTC, datetime
import json

import pytest

from app.core.store import EventStore
from app.privacy.application import PrivacyReadGate
from app.privacy.ledger import PrivacyLedger
from app.private_web.runtime import Stage0ConversationRuntime
from app.trusted.openai_transport import (
    FakeOpenAITransport,
    ProviderResponse,
    ProviderOutput,
    ProviderContent,
)
from test_private_web_conversation import principal, Clock
from app.private_web.conversation import MessageStatus

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def response(decision="reply", value="Да, я готов обсудить этот вопрос."):
    field = {
        "reply": "content",
        "no_action": "reason_category",
        "research": "brief",
        "propose_publication": "draft",
    }.get(decision, "content")
    data = dict(
        decision=decision, reason_category=None, content=None, brief=None, draft=None
    )
    data[field] = value
    return ProviderResponse(
        "synthetic-response",
        "completed",
        (
            ProviderOutput(
                "message", (ProviderContent("output_text", json.dumps(data)),)
            ),
        ),
        None,
    )


@pytest.fixture
def setup(tmp_path):
    store = EventStore(tmp_path / "events.db")
    ledger = PrivacyLedger(tmp_path / "ledger.db")
    gate = PrivacyReadGate(ledger, store)
    assert gate.reconcile()
    fake = FakeOpenAITransport([response() for _ in range(5)])
    runtime = Stage0ConversationRuntime(store, gate, fake, clock=Clock())
    return store, ledger, gate, fake, runtime


def test_round_trip_restart_retry_exact_identity_and_isolation(setup):
    store, ledger, gate, fake, runtime = setup
    a, b = principal("a"), principal("b")
    first = runtime.submit(a, "one", "Синтетический секрет А")
    runtime.submit(b, "one", "Синтетический секрет Б")
    runtime.submit(b, "two", "Продолжим отдельный разговор Б")
    runtime.submit(a, "two", "Продолжим разговор")
    assert first.model_state.value == "completed"
    assert first.response_content == "Да, я готов обсудить этот вопрос."
    reopened_store = EventStore(store.database_path)
    reopened_gate = PrivacyReadGate(ledger, reopened_store)
    assert reopened_gate.reconcile()
    reopened = Stage0ConversationRuntime(reopened_store, reopened_gate, fake)
    retry = reopened.submit(a, "one", "Синтетический секрет А")
    assert retry.message_status.value == "duplicate"
    assert retry.response_content == first.response_content
    assert len(fake.calls) == 4
    assert "секрет Б" not in fake.calls[-1].request.input_text
    assert [item.role for item in reopened.history(a)] == [
        "participant",
        "agent",
        "participant",
        "agent",
    ]
    with pytest.raises(ValueError):
        reopened.submit(a, "one", "Изменённый текст")
    assert len(fake.calls) == 4


def reopened(setup, **kwargs):
    store, ledger, _, fake, _ = setup
    new_store = EventStore(store.database_path)
    gate = PrivacyReadGate(ledger, new_store)
    assert gate.reconcile()
    return Stage0ConversationRuntime(new_store, gate, fake, **kwargs)


@pytest.mark.parametrize(
    ("point", "state", "calls"),
    [
        ("before_reservation", "completed", 1),
        ("after_reservation", "pending", 0),
        ("after_model", "pending", 1),
        ("after_reply_commit", "completed", 1),
        ("before_release", "completed", 1),
    ],
)
def test_crash_boundaries_never_repeat_uncertain_inference(setup, point, state, calls):
    store, _, gate, fake, _ = setup

    def crash(current):
        if current == point:
            raise RuntimeError("synthetic crash")

    runtime = Stage0ConversationRuntime(store, gate, fake, checkpoint=crash)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        runtime.submit(principal("a"), "crash", "Синтетический запрос")
    retry = reopened(setup).submit(principal("a"), "crash", "Синтетический запрос")
    assert retry.model_state.value == state
    assert len(fake.calls) == calls
    assert len(gate.retrieve_private_events(principal("a"))) == 1


@pytest.mark.parametrize(
    ("proposal", "state"),
    [
        (response("no_action", "not_warranted"), "no_action"),
        (response("research", "synthetic brief"), "unsupported"),
        (response("propose_publication", "synthetic draft"), "unsupported"),
        (response("remember", "synthetic unauthorized"), "invalid"),
        (response("reply", ""), "invalid"),
    ],
)
def test_terminal_decisions_are_durable_and_have_no_fabricated_reply(
    setup, proposal, state
):
    store, _, gate, _, _ = setup
    fake = FakeOpenAITransport([proposal])
    runtime = Stage0ConversationRuntime(store, gate, fake)
    first = runtime.submit(principal("a"), "terminal", "Синтетический запрос")
    assert first.model_state.value == state
    assert first.response_content is None
    assert runtime.submit(
        principal("a"), "terminal", "Синтетический запрос"
    ) == replace(first, message_status=MessageStatus.DUPLICATE)
    assert len(fake.calls) == 1
    assert [item.role for item in runtime.history(principal("a"))] == ["participant"]


def test_provider_failure_and_russian_fallback_are_distinct(setup):
    from app.trusted.openai_transport import OpenAITransportError
    from app.behavior.russian_gate import RussianOutputPolicy

    store, _, gate, _, _ = setup
    fake = FakeOpenAITransport(
        [
            OpenAITransportError(timeout=True),
            response("reply", "English reply rejected"),
        ]
    )
    runtime = Stage0ConversationRuntime(store, gate, fake)
    failed = runtime.submit(principal("a"), "failure", "Синтетический запрос")
    assert failed.model_state.value == "failed"
    assert failed.response_content is None
    fallback = runtime.submit(principal("a"), "russian", "Синтетический запрос")
    assert fallback.response_content == RussianOutputPolicy.SAFE_FALLBACK
    assert (
        runtime.submit(
            principal("a"), "russian", "Синтетический запрос"
        ).response_content
        == fallback.response_content
    )
    assert len(fake.calls) == 2


def delete_source(setup, event_id):
    from app.privacy.application import EventStoreErasureAdapter
    from app.privacy.deletion import DeletionCoordinator
    from test_privacy_deletion import context, request

    store, ledger, _, _, _ = setup
    return DeletionCoordinator(ledger, EventStoreErasureAdapter(store)).delete(
        context(), request(content_reference=event_id)
    )


@pytest.mark.parametrize(
    "point",
    [
        "before_reservation",
        "after_reservation",
        "after_model",
        "after_reply_commit",
        "before_release",
        "released",
    ],
)
def test_deletion_at_each_boundary_and_restart_hides_dependent_replies(setup, point):
    from app.privacy.application import PrivacyReadBlocked

    store, ledger, gate, fake, runtime = setup

    def delete(current):
        if current == point:
            event = gate.retrieve_private_events(principal("a"))[0]
            assert delete_source(setup, event.event_id).disposition.value == "applied"

    runtime = Stage0ConversationRuntime(store, gate, fake, checkpoint=delete)
    if point == "released":
        runtime.submit(principal("a"), "delete", "Удаляемый синтетический секрет")
        delete_source(setup, gate.retrieve_private_events(principal("a"))[0].event_id)
    else:
        with pytest.raises(PrivacyReadBlocked):
            runtime.submit(principal("a"), "delete", "Удаляемый синтетический секрет")
    fresh = reopened(setup)
    assert fresh.history(principal("a")) == ()
    with pytest.raises(PrivacyReadBlocked):
        fresh.submit(principal("a"), "delete", "Удаляемый синтетический секрет")
    assert len(fake.calls) == (
        0 if point in ("before_reservation", "after_reservation") else 1
    )
    with store._open() as db:
        assert db.execute("SELECT count(*) FROM agent_reply_payloads").fetchone() == (
            0,
        )


def test_old_backup_cannot_resurrect_released_reply(setup, tmp_path):
    import os
    import shutil
    from app.privacy.recovery import create_sqlite_backup, QuarantinedRestore
    from app.privacy.application import EventStoreRestoreAdapter, PrivacyReadBlocked

    store, ledger, gate, fake, runtime = setup
    runtime.submit(principal("a"), "backup", "Удаляемый синтетический секрет")
    backup = tmp_path / "old.db"
    create_sqlite_backup(store.database_path, backup, 5, NOW)
    event_id = gate.retrieve_private_events(principal("a"))[0].event_id
    delete_source(setup, event_id)
    result = QuarantinedRestore(ledger, 5, EventStoreRestoreAdapter).restore(
        backup, tmp_path / "quarantine.db", NOW
    )
    assert result.activation_eligible
    # Local synthetic copy only; never activate the quarantined artifact.
    restored = tmp_path / "restored-test.db"
    shutil.copyfile(backup, restored)
    os.chmod(restored, 0o600)
    old_store = EventStore(restored)
    old_gate = PrivacyReadGate(ledger, old_store)
    closed = Stage0ConversationRuntime(old_store, old_gate, fake)
    with pytest.raises(PrivacyReadBlocked):
        closed.history(principal("a"))
    assert old_gate.reconcile()
    assert closed.history(principal("a")) == ()
    with pytest.raises(PrivacyReadBlocked):
        closed.submit(principal("a"), "backup", "Удаляемый синтетический секрет")
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "point",
    ["initial", "after_reservation", "after_model", "before_release", "released"],
)
def test_stop_before_during_and_after_processing(setup, point):
    store, _, gate, fake, _ = setup
    stopped = [point == "initial"]

    def stop(current):
        if current == point:
            stopped[0] = True

    runtime = Stage0ConversationRuntime(
        store, gate, fake, emergency_stop=lambda: stopped[0], checkpoint=stop
    )
    result = runtime.submit(principal("a"), "stop", "Синтетический запрос")
    if point == "released":
        assert result.model_state.value == "completed"
        stopped[0] = True
    else:
        assert result.model_state.value == "skipped"
    assert (
        runtime.submit(principal("a"), "stop", "Синтетический запрос").response_content
        is None
    )
    assert all(item.role == "participant" for item in runtime.history(principal("a")))
    assert len(fake.calls) == (0 if point in ("initial", "after_reservation") else 1)


def test_spend_limit_suspension_survives_restart(setup):
    from app.trusted.openai_transport import OpenAITransportError

    store, ledger, gate, _, _ = setup
    fake = FakeOpenAITransport(
        [OpenAITransportError(error_code="project_spend_limit_exceeded"), response()]
    )
    runtime = Stage0ConversationRuntime(store, gate, fake)
    assert (
        runtime.submit(
            principal("a"), "spend", "Синтетический запрос"
        ).model_state.value
        == "skipped"
    )
    new_store = EventStore(store.database_path)
    new_gate = PrivacyReadGate(ledger, new_store)
    assert new_gate.reconcile()
    new_runtime = Stage0ConversationRuntime(new_store, new_gate, fake)
    assert (
        new_runtime.submit(
            principal("a"), "after", "Следующий запрос"
        ).model_state.value
        == "skipped"
    )
    assert len(fake.calls) == 1


def test_two_concurrent_duplicate_requests_share_one_operation(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    store, _, gate, fake, runtime = setup
    started, finish = Event(), Event()
    original = FakeOpenAITransport.call

    def blocking(self, request, **kwargs):
        started.set()
        assert finish.wait(10)
        return original(self, request, **kwargs)

    monkeypatch.setattr(FakeOpenAITransport, "call", blocking)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(
            runtime.submit, principal("a"), "concurrent", "Синтетический запрос"
        )
        assert started.wait(10)
        second_runtime = reopened(setup)
        duplicate = pool.submit(
            second_runtime.submit, principal("a"), "concurrent", "Синтетический запрос"
        ).result(10)
        assert duplicate.model_state.value == "pending"
        finish.set()
        assert first.result(10).model_state.value == "completed"
    assert len(fake.calls) == 1
    assert runtime.submit(
        principal("a"), "concurrent", "Синтетический запрос"
    ).response_content


def test_runtime_cannot_reach_network_credentials_or_live_effects(setup, monkeypatch):
    import socket
    from app.trusted.openai_transport import OpenAIResponsesTransport

    def forbidden(*args, **kwargs):
        pytest.fail("live entry point reached")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(OpenAIResponsesTransport, "from_environment", forbidden)
    monkeypatch.setattr(
        "app.trusted.openai_credential_files.build_worker_openai_transport", forbidden
    )
    monkeypatch.setattr(
        "app.trusted.openai_credential_files.read_protected_worker_file", forbidden
    )
    _, _, _, fake, runtime = setup
    assert runtime.submit(
        principal("a"), "offline", "Синтетический запрос"
    ).response_content
    assert len(fake.calls) == 1


def test_runtime_constructor_rejects_live_transport_and_mismatched_privacy_store(
    setup, tmp_path
):
    store, ledger, gate, fake, _ = setup
    with pytest.raises(TypeError):
        Stage0ConversationRuntime(store, gate, object())
    other = EventStore(tmp_path / "other.db")
    with pytest.raises(ValueError):
        Stage0ConversationRuntime(other, gate, fake)


def test_delivery_capability_is_checked_before_inference_and_release(setup):
    store, _, gate, fake, _ = setup
    disabled = Stage0ConversationRuntime(
        store, gate, fake, delivery_enabled=lambda: False
    )
    assert (
        disabled.submit(
            principal("a"), "disabled", "Синтетический запрос"
        ).model_state.value
        == "skipped"
    )
    assert not fake.calls
    enabled = [True]

    def suspend(point):
        if point == "before_release":
            enabled[0] = False

    runtime = Stage0ConversationRuntime(
        store, gate, fake, delivery_enabled=lambda: enabled[0], checkpoint=suspend
    )
    assert (
        runtime.submit(principal("a"), "held", "Синтетический запрос").response_content
        is None
    )
    assert [item.role for item in runtime.history(principal("a"))] == [
        "participant",
        "participant",
    ]
    enabled[0] = True
    assert (
        reopened(setup)
        .submit(principal("a"), "held", "Синтетический запрос")
        .response_content
    )
    assert len(fake.calls) == 1


def test_actual_owner_stop_controls_runtime_without_broadening_audience(
    setup, tmp_path
):
    from app.owner_control.store import OwnerStore

    owner = OwnerStore(tmp_path / "owner", clock=lambda: NOW)
    owner.provision("synthetic-owner", "synthetic-owner-password")
    token = owner.login(
        "synthetic-owner", "synthetic-owner-password", "local-fixture"
    ).session_token
    store, _, gate, fake, _ = setup
    runtime = Stage0ConversationRuntime(store, gate, fake, emergency_stop=owner.active)
    assert (
        runtime.submit(
            principal("a"), "stopped", "Синтетический запрос"
        ).model_state.value
        == "skipped"
    )
    assert owner.change(token, owner.issue(token), False) == "applied"
    assert (
        runtime.submit(
            principal("a"), "resumed", "Синтетический запрос"
        ).model_state.value
        == "completed"
    )
    assert owner.change(token, owner.issue(token), True) == "applied"
    assert (
        runtime.submit(
            principal("a"), "resumed", "Синтетический запрос"
        ).response_content
        is None
    )
    assert runtime.history(principal("b")) == ()
    assert len(fake.calls) == 1


def test_race_between_initial_lookup_and_reservation_reuses_winners_reply(setup):
    store, _, gate, fake, other = setup

    def race(point):
        if point == "before_reservation":
            assert other.submit(
                principal("a"), "race", "Синтетический запрос"
            ).response_content

    racing = Stage0ConversationRuntime(store, gate, fake, checkpoint=race)
    assert racing.submit(
        principal("a"), "race", "Синтетический запрос"
    ).response_content
    assert len(fake.calls) == 1


def test_legacy_ambiguous_input_identity_is_rejected(setup):
    from app.private_web.conversation import ConversationService
    from app.core.context import PrivateContextAssembler, ContextLimits
    from app.core.events import AuthenticatedSourceReference
    from app.privacy.application import PrivacyReadBlocked

    store, _, gate, fake, runtime = setup
    legacy = ConversationService(
        store, PrivateContextAssembler(store, ContextLimits(32000, 99))
    )
    legacy.submit(principal("a"), "ambiguous", "Синтетический запрос")
    another_session = replace(
        principal("a"),
        authenticated_source=AuthenticatedSourceReference("another-session"),
    )
    legacy.submit(another_session, "ambiguous", "Синтетический запрос")
    with pytest.raises(PrivacyReadBlocked, match="ambiguous"):
        runtime.submit(principal("a"), "ambiguous", "Синтетический запрос")
    assert not fake.calls


def test_unreserved_original_outside_context_window_does_not_use_latest_event(setup):
    from app.private_web.conversation import ConversationService
    from app.core.context import PrivateContextAssembler, ContextLimits
    from app.privacy.application import PrivacyReadBlocked

    store, _, gate, fake, _ = setup
    clock = Clock()
    legacy = ConversationService(
        store, PrivateContextAssembler(store, ContextLimits(32000, 99)), clock=clock
    )
    legacy.submit(principal("a"), "old", "Старый синтетический запрос")
    for number in range(100):
        legacy.submit(principal("a"), f"later-{number}", "Поздний синтетический запрос")
    runtime = Stage0ConversationRuntime(store, gate, fake, clock=clock)
    with pytest.raises(PrivacyReadBlocked, match="context_window"):
        runtime.submit(principal("a"), "old", "Старый синтетический запрос")
    assert not fake.calls


@pytest.mark.parametrize("point", ["after_reservation", "before_release"])
def test_reconciled_deletion_still_rejects_missing_sources(setup, point):
    from app.privacy.application import PrivacyReadBlocked

    store, _, gate, fake, _ = setup

    def remove(current):
        if current == point:
            event_id = gate.retrieve_private_events(principal("a"))[0].event_id
            delete_source(setup, event_id)
            assert gate.reconcile()

    runtime = Stage0ConversationRuntime(store, gate, fake, checkpoint=remove)
    with pytest.raises(PrivacyReadBlocked, match="reply_sources"):
        runtime.submit(principal("a"), "reconciled", "Удаляемый синтетический запрос")
    assert runtime.history(principal("a")) == ()


def test_provider_duplicate_and_record_failure_preserve_pending(setup, monkeypatch):
    from app.core.provider_spend import ProviderStateStore, ProviderPersistenceError

    store, _, gate, fake, _ = setup

    def reserved_elsewhere(point):
        if point == "after_reservation":
            event = gate.retrieve_private_events(principal("a"))[-1]
            ProviderStateStore(store).reserve_attempt(
                f"reply:{event.event_id}:1", event.correlation_id, NOW
            )

    runtime = Stage0ConversationRuntime(
        store, gate, fake, checkpoint=reserved_elsewhere
    )
    assert (
        runtime.submit(
            principal("a"), "duplicate-provider", "Синтетический запрос"
        ).model_state.value
        == "pending"
    )
    assert not fake.calls

    def fail(*args, **kwargs):
        raise ProviderPersistenceError("synthetic persistence fault")

    monkeypatch.setattr(ProviderStateStore, "record_attempt", fail)
    failed_runtime = Stage0ConversationRuntime(store, gate, fake)
    assert (
        failed_runtime.submit(
            principal("a"), "failed-record", "Синтетический запрос"
        ).model_state.value
        == "pending"
    )
    assert (
        failed_runtime.submit(
            principal("a"), "failed-record", "Синтетический запрос"
        ).model_state.value
        == "pending"
    )
    assert len(fake.calls) == 1


def test_transient_stop_at_gateway_preflight_is_an_operational_skip(setup):
    store, _, gate, fake, _ = setup
    calls = [0]

    def stop():
        calls[0] += 1
        return calls[0] == 2

    runtime = Stage0ConversationRuntime(store, gate, fake, emergency_stop=stop)
    assert (
        runtime.submit(
            principal("a"), "transient-stop", "Синтетический запрос"
        ).model_state.value
        == "skipped"
    )
    assert not fake.calls


def test_crash_before_input_commit_rolls_back_and_allows_one_new_operation(setup):
    from app.core.store import FaultPoint

    store, _, gate, fake, runtime = setup

    def crash(point):
        if point is FaultPoint.AFTER_PAYLOAD_INSERT:
            raise RuntimeError("synthetic input transaction crash")

    store._fault_injector = crash
    with pytest.raises(RuntimeError):
        runtime.submit(principal("a"), "input-crash", "Синтетический запрос")
    store._fault_injector = None
    assert runtime.history(principal("a")) == ()
    assert not fake.calls
    assert runtime.submit(
        principal("a"), "input-crash", "Синтетический запрос"
    ).response_content
    assert len(fake.calls) == 1


def test_delayed_unreserved_request_context_ends_at_exact_original_event(setup):
    from app.private_web.conversation import ConversationService
    from app.core.context import PrivateContextAssembler, ContextLimits

    store, _, gate, fake, _ = setup
    clock = Clock()
    legacy = ConversationService(
        store, PrivateContextAssembler(store, ContextLimits(32000, 99)), clock=clock
    )
    legacy.submit(principal("a"), "original", "Первоначальный синтетический запрос")
    legacy.submit(principal("a"), "later", "Поздний посторонний запрос")
    runtime = Stage0ConversationRuntime(store, gate, fake, clock=clock)
    assert runtime.submit(
        principal("a"), "original", "Первоначальный синтетический запрос"
    ).response_content
    payload = json.loads(fake.calls[0].request.input_text)
    assert [segment["content"] for segment in payload["conversation"]] == [
        "Первоначальный синтетический запрос"
    ]
    event = gate.retrieve_private_events(principal("a"))[0]
    op = runtime._outputs.read_reply(
        principal("a"), f"reply:{event.event_id}"
    ).operation
    assert op.initiating_event_id == event.event_id
    assert op.source_event_ids == (event.event_id,)


def test_maximum_valid_principal_has_bounded_durable_conversation_route(setup):
    from app.core.events import ParticipantId

    _, _, gate, fake, runtime = setup
    who = replace(principal("a"), principal_id=ParticipantId("p" * 128))
    assert runtime.submit(who, "bounded-route", "Синтетический запрос").response_content
    event = gate.retrieve_private_events(who)[0]
    operation = runtime._outputs.read_reply(who, f"reply:{event.event_id}").operation
    assert len(operation.conversation_id) < 128
    assert who.principal_id.value not in fake.calls[0].request.input_text


def test_concurrent_deletion_during_actual_inference_blocks_commit(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.privacy.application import PrivacyReadBlocked

    _, _, gate, fake, runtime = setup
    started, finish = Event(), Event()
    original = FakeOpenAITransport.call

    def pending(self, request, **kwargs):
        started.set()
        assert finish.wait(10)
        return original(self, request, **kwargs)

    monkeypatch.setattr(FakeOpenAITransport, "call", pending)
    with ThreadPoolExecutor(1) as pool:
        work = pool.submit(
            runtime.submit,
            principal("a"),
            "inflight-deletion",
            "Удаляемый синтетический запрос",
        )
        assert started.wait(10)
        event = gate.retrieve_private_events(principal("a"))[0]
        assert delete_source(setup, event.event_id).disposition.value == "applied"
        finish.set()
        with pytest.raises(PrivacyReadBlocked):
            work.result(10)
    assert gate.reconcile()
    assert runtime.history(principal("a")) == ()
    assert len(fake.calls) == 1
    with pytest.raises(PrivacyReadBlocked):
        runtime.submit(
            principal("a"), "inflight-deletion", "Удаляемый синтетический запрос"
        )
