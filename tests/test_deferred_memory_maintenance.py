"""Offline source-bound maintenance survives response delivery and restart."""

from threading import Event, Thread

import pytest

from app.cognition.worker_evolution import EvolutionModelRequest, EvolutionProposal
from app.cognition.worker_typed import TypedModelRequest, TypedProposal
from app.cognition.worker_shared import SharedProposalRequest, SharedSuggestion
from app.core.agent_proposals import (
    ProposalValidation,
    ReplyProposal,
    NoActionProposal,
    NoActionReason,
)
from app.core.openai_worker_client import ClientResult
from app.private_web.runtime import WorkerConversationRuntime
from test_cognition_pipeline import principal
from test_worker_shared_proposals import _setup


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


@pytest.fixture
def maintenance(tmp_path, monkeypatch):
    events, ledger, gate, shared, original, _ = _setup(
        tmp_path, monkeypatch, SharedSuggestion("no_change", None, None, None, None)
    )
    client, provider = original._transport, original._provider
    calls, controls = [], {"enabled": True, "before": lambda _request: None}

    def infer(request, *, operation_id, correlation_id):
        calls.append(request)
        controls["before"](request)
        if isinstance(request, EvolutionModelRequest):
            result = dict(
                evolution=EvolutionProposal(
                    "new_memory",
                    (),
                    "candidate_useful_memory",
                    "Синтетический интерес к языкам.",
                    0.6,
                )
            )
        elif isinstance(request, TypedModelRequest):
            result = dict(
                typed=TypedProposal(
                    "new_memory",
                    None,
                    "durable_interest",
                    {
                        "topic": "Итальянский язык",
                        "strength": 60,
                        "status": "active",
                        "reason_for_interest": "Синтетический разговор о языке.",
                    },
                    "supporting",
                )
            )
        elif isinstance(request, SharedProposalRequest):
            result = dict(shared=SharedSuggestion("no_change", None, None, None, None))
        else:
            result = dict(
                validation=ProposalValidation(
                    NoActionProposal(NoActionReason.NOT_WARRANTED)
                    if controls.get("no_action")
                    else ReplyProposal("Обсудим это."),
                    None,
                )
            )
        return ClientResult(
            operation_id, correlation_id, "completed", 1, None, **result
        )

    monkeypatch.setattr(client, "infer", infer)

    def build(**kwargs):
        config = dict(
            provider_state=provider,
            shared_intelligence=shared,
            memory_evolution_enabled=True,
            typed_memory_enabled=True,
            shared_proposals_enabled=True,
            shared_proposal_audience=("participant-b",),
            clock=original._clock,
            defer_memory_maintenance=True,
            maintenance_principal_enabled=lambda _principal: controls["enabled"],
        )
        config.update(kwargs)
        return WorkerConversationRuntime(events, gate, client, **config)

    return events, ledger, gate, provider, calls, controls, build


def test_response_precedes_auxiliary_inference_and_next_turn_uses_persisted_memory(
    maintenance,
):
    events, _, _, _, calls, _, build = maintenance
    runtime = build()
    assert (
        runtime.submit(
            principal("a"), "first", "Синтетический разговор о языках."
        ).model_state.value
        == "completed"
    )
    assert len(calls) == 1 and not isinstance(calls[0], EvolutionModelRequest)
    assert runtime.drain_memory_maintenance() == 1
    assert [type(row) for row in calls[1:]] == [
        EvolutionModelRequest,
        TypedModelRequest,
        SharedProposalRequest,
    ]
    # A renewed runtime resumes durable state without scheduling old paid work again.
    runtime = build()
    assert runtime.drain_memory_maintenance() == 0 and len(calls) == 4
    runtime.submit(principal("a"), "later", "Что помнишь о языках?")
    assert "Итальянский язык" in calls[-1].serialize()
    with events._open() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM private_extraction_receipts WHERE operation_id LIKE 'maintenance:%'"
            ).fetchone()[0]
            == 0
        )


def test_received_auxiliary_receipt_reconciles_after_crash_without_a_second_call(
    maintenance,
):
    _, _, _, _, calls, _, build = maintenance
    crashed = False

    def crash(point):
        nonlocal crashed
        if point == "after_extraction_receipt" and not crashed:
            crashed = True
            raise RuntimeError("synthetic interruption after durable receipt")

    runtime = build(checkpoint=crash)
    runtime.submit(principal("a"), "first", "Синтетический разговор.")
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        runtime.drain_memory_maintenance()
    assert len(calls) == 2
    assert build().drain_memory_maintenance() == 1
    assert len(calls) == 4


def test_interrupted_auxiliary_call_is_not_blindly_replayed(maintenance):
    _, _, _, _, calls, controls, build = maintenance
    runtime = build()
    runtime.submit(principal("a"), "first", "Синтетический разговор.")

    def interrupted(request):
        if isinstance(request, EvolutionModelRequest):
            raise RuntimeError("synthetic lost worker connection")

    controls["before"] = interrupted
    with pytest.raises(RuntimeError):
        runtime.drain_memory_maintenance()
    controls["before"] = lambda _request: None
    assert build().drain_memory_maintenance() == 1
    assert sum(isinstance(row, EvolutionModelRequest) for row in calls) == 1


def test_stop_preserves_the_backlog_and_disabled_account_cannot_start_maintenance(
    maintenance,
):
    _, _, _, _, calls, controls, build = maintenance
    stopped = {"value": False}
    runtime = build(emergency_stop=lambda: stopped["value"])
    runtime.submit(principal("a"), "first", "Синтетический разговор.")
    stopped["value"] = True
    assert runtime.drain_memory_maintenance() == 0 and len(calls) == 1
    stopped["value"] = False
    controls["enabled"] = False
    assert runtime.drain_memory_maintenance() == 1 and len(calls) == 1
    controls["enabled"] = True
    assert build().drain_memory_maintenance() == 0


def test_deleted_sources_never_reach_delayed_inference(maintenance):
    from app.privacy.application import EventStoreErasureAdapter
    from app.privacy.deletion import DeletionCoordinator
    from test_privacy_deletion import context, request

    events, ledger, gate, _, calls, _, build = maintenance
    runtime = build()
    runtime.submit(principal("a"), "first", "PRIVATE_MAINTENANCE_DELETE")
    with events._open() as db:
        root = db.execute(
            "SELECT event_id FROM event_envelopes WHERE source_message_id='first'"
        ).fetchone()[0]
    DeletionCoordinator(ledger, EventStoreErasureAdapter(events)).delete(
        context(), request(content_reference=root)
    )
    assert gate.reconcile()
    assert build().drain_memory_maintenance() == 0 and len(calls) == 1


def test_two_maintenance_drainers_do_not_duplicate_a_paid_operation(maintenance):
    _, _, _, _, calls, controls, build = maintenance
    runtime = build()
    runtime.submit(principal("a"), "first", "Синтетический разговор.")
    entered, release = Event(), Event()

    def blocked(request):
        if isinstance(request, EvolutionModelRequest):
            entered.set()
            assert release.wait(5)

    controls["before"] = blocked
    results = []
    thread = Thread(target=lambda: results.append(runtime.drain_memory_maintenance()))
    thread.start()
    try:
        assert entered.wait(5)
        assert build().drain_memory_maintenance() == 0
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and results == [1]
    assert sum(isinstance(row, EvolutionModelRequest) for row in calls) == 1


@pytest.mark.parametrize("limit", [0, 17, True, "1"])
def test_maintenance_limits_are_bounded(maintenance, limit):
    with pytest.raises(ValueError):
        maintenance[-1]().drain_memory_maintenance(limit=limit)


def test_maintenance_yields_between_stages_when_a_foreground_turn_arrives(maintenance):
    _, _, _, _, calls, controls, build = maintenance
    runtime = build()
    runtime.submit(principal("a"), "first", "Синтетический разговор.")
    auxiliary, foreground, release_auxiliary, release_foreground = (
        Event(),
        Event(),
        Event(),
        Event(),
    )

    def hold(request):
        if isinstance(request, EvolutionModelRequest):
            auxiliary.set()
            assert release_auxiliary.wait(5)
        elif not isinstance(request, (TypedModelRequest, SharedProposalRequest)):
            foreground.set()
            assert release_foreground.wait(5)

    controls["before"] = hold
    drained = []
    maintenance_thread = Thread(
        target=lambda: drained.append(runtime.drain_memory_maintenance())
    )
    maintenance_thread.start()
    assert auxiliary.wait(5)
    foreground_thread = Thread(
        target=lambda: runtime.submit(principal("b"), "second", "Другой разговор.")
    )
    foreground_thread.start()
    try:
        assert foreground.wait(5)
        assert runtime.drain_memory_maintenance() == 0
        release_auxiliary.set()
        maintenance_thread.join(5)
        assert not maintenance_thread.is_alive() and drained == [0]
        assert not any(isinstance(row, TypedModelRequest) for row in calls)
    finally:
        release_auxiliary.set()
        release_foreground.set()
        foreground_thread.join(5)
    controls["before"] = lambda _request: None
    assert runtime.drain_memory_maintenance() == 1
    assert sum(isinstance(row, EvolutionModelRequest) for row in calls) == 1


def test_a_deliberate_no_action_still_allows_admitted_input_maintenance(maintenance):
    _, _, _, _, calls, controls, build = maintenance
    controls["no_action"] = True
    runtime = build()
    assert (
        runtime.submit(
            principal("a"), "quiet", "Синтетический разговор."
        ).model_state.value
        == "no_action"
    )
    assert runtime.drain_memory_maintenance() == 1
    assert len(calls) == 4


@pytest.mark.parametrize("control", ["stop", "spend"])
def test_a_mid_maintenance_pause_preserves_remaining_work(
    maintenance, monkeypatch, control
):
    _, _, _, provider, calls, controls, build = maintenance
    paused = {"value": False}
    monkeypatch.setattr(
        provider,
        "is_spend_suspended",
        lambda: paused["value"] if control == "spend" else False,
    )
    runtime = build(
        emergency_stop=lambda: paused["value"] if control == "stop" else False
    )
    runtime.submit(principal("a"), "first", "Синтетический разговор.")

    def pause(request):
        if isinstance(request, EvolutionModelRequest):
            paused["value"] = True

    controls["before"] = pause
    assert runtime.drain_memory_maintenance() == 0
    assert len(calls) == 2
    paused["value"] = False
    controls["before"] = lambda _request: None
    assert runtime.drain_memory_maintenance() == 1
    assert len(calls) == 4


@pytest.mark.parametrize(
    "options,error",
    [
        ({"defer_memory_maintenance": 1}, TypeError),
        ({"maintenance_principal_enabled": None}, TypeError),
        (
            {
                "memory_evolution_enabled": False,
                "typed_memory_enabled": False,
                "shared_proposals_enabled": False,
            },
            ValueError,
        ),
    ],
)
def test_deferral_requires_explicit_features_and_an_authorization_check(
    maintenance, options, error
):
    with pytest.raises(error):
        maintenance[-1](**options)


def test_disabled_deferral_has_no_background_work(maintenance):
    runtime = maintenance[-1](defer_memory_maintenance=False)
    assert runtime.drain_memory_maintenance() == 0


def test_completed_rows_do_not_hide_a_later_participants_backlog(maintenance):
    _, _, _, _, calls, _, build = maintenance
    runtime = build()
    runtime.submit(principal("a"), "first", "PRIVATE_MAINTENANCE_A")
    runtime.submit(principal("b"), "second", "PRIVATE_MAINTENANCE_B")
    assert runtime.drain_memory_maintenance() == 1
    assert runtime.drain_memory_maintenance() == 1
    last = next(
        row for row in reversed(calls) if isinstance(row, EvolutionModelRequest)
    )
    assert last.participant_input == "PRIVATE_MAINTENANCE_B"
    assert "PRIVATE_MAINTENANCE_A" not in last.serialize()
    assert last.memories == ()
    assert runtime.drain_memory_maintenance() == 0


def test_enabling_deferral_does_not_reprocess_earlier_inline_turns(maintenance):
    _, _, _, _, calls, _, build = maintenance
    build(defer_memory_maintenance=False).submit(
        principal("a"), "old", "Старый синтетический разговор."
    )
    before = len(calls)
    assert build().drain_memory_maintenance() == 0 and len(calls) == before


def test_a_bounded_batch_processes_both_participants_without_mixing_their_inputs(
    maintenance,
):
    _, _, _, _, calls, _, build = maintenance
    runtime = build()
    runtime.submit(principal("a"), "first", "PRIVATE_BATCH_A")
    runtime.submit(principal("b"), "second", "PRIVATE_BATCH_B")
    assert runtime.drain_memory_maintenance(limit=2) == 2
    auxiliary = calls[2:]
    assert len(auxiliary) == 6
    assert all("PRIVATE_BATCH_B" not in row.serialize() for row in auxiliary[:3])
    assert all("PRIVATE_BATCH_A" not in row.serialize() for row in auxiliary[3:])
    assert build().drain_memory_maintenance(limit=2) == 0
    assert len(calls) == 8


def test_missing_source_during_maintenance_fails_closed(maintenance, monkeypatch):
    _, _, _, _, calls, _, build = maintenance
    runtime = build()
    runtime.submit(principal("a"), "first", "Синтетический разговор.")
    monkeypatch.setattr(
        "app.private_web.memory_maintenance.retrieve_exact_private_event",
        lambda *_args: None,
    )
    assert runtime.drain_memory_maintenance() == 0 and len(calls) == 1


def test_a_terminal_receipt_observed_after_selection_is_not_retried(
    maintenance, monkeypatch
):
    from app.private_web.extraction_audit import ExtractionAudit

    _, _, _, _, calls, _, build = maintenance
    runtime = build()
    runtime.submit(principal("a"), "first", "Синтетический разговор.")
    audit = runtime._memory_maintenance.audit
    reserve = audit.reserve

    def completed(*args):
        reserve(*args)
        audit.finish(args[0], "accepted")
        return ExtractionAudit("accepted", None)

    monkeypatch.setattr(audit, "reserve", completed)
    assert runtime.drain_memory_maintenance() == 0 and len(calls) == 1


def test_retry_during_delayed_receipt_commit_does_not_race_maintenance(maintenance):
    _, _, _, _, calls, _, build = maintenance
    entered, release = Event(), Event()

    def checkpoint(point):
        if point == "after_extraction_receipt" and not entered.is_set():
            entered.set()
            assert release.wait(5)

    runtime = build(checkpoint=checkpoint)
    content = "Синтетический разговор."
    first = runtime.submit(principal("a"), "first", content)
    results, errors = [], []

    def drain():
        try:
            results.append(runtime.drain_memory_maintenance())
        except Exception as error:
            errors.append(type(error).__name__)

    thread = Thread(target=drain)
    thread.start()
    try:
        assert entered.wait(5)
        replay = runtime.submit(principal("a"), "first", content)
        assert replay.model_state.value == "completed"
        operation = "evolution:" + runtime._operation_id(
            first.turn[0].event_id
        ).removeprefix("reply:")
        assert (
            runtime._evolution_audit.read(
                operation, "participant-a", first.turn[0].event_id, content
            ).status
            == "received"
        )
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and errors == [] and results == [1]
    assert len(calls) == 4


def test_unrelated_old_context_deletion_does_not_cancel_a_live_input_pipeline(
    maintenance,
):
    from app.privacy.application import EventStoreErasureAdapter
    from app.privacy.deletion import DeletionCoordinator
    from test_privacy_deletion import context, request

    events, ledger, gate, _, calls, _, build = maintenance
    first = build(defer_memory_maintenance=False).submit(
        principal("a"), "old", "PRIVATE_OLD_CONTEXT"
    )
    runtime = build()
    second = runtime.submit(principal("a"), "new", "Что помнишь об итальянском языке?")
    assert "Итальянский язык" in calls[-1].serialize()
    with events._open() as db:
        assert (
            db.execute(
                "SELECT 1 FROM agent_reply_sources WHERE operation_id=? AND event_id=?",
                (
                    runtime._operation_id(second.turn[0].event_id),
                    first.turn[0].event_id,
                ),
            ).fetchone()
            is not None
        )
    before = len(calls)
    DeletionCoordinator(ledger, EventStoreErasureAdapter(events)).delete(
        context(), request(content_reference=first.turn[0].event_id)
    )
    assert gate.reconcile()
    assert runtime.drain_memory_maintenance() == 1
    assert len(calls) == before + 3
    recent = calls[before:]
    assert all("PRIVATE_OLD_CONTEXT" not in row.serialize() for row in recent)
    assert (
        next(row for row in recent if isinstance(row, EvolutionModelRequest)).memories
        == ()
    )
