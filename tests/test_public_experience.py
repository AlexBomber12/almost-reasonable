"""Durable public experience through real runtime/IPC and fake SDK input."""

import json
from datetime import timedelta
from threading import Thread

import pytest

from test_int24_runtime import NOW, setup, run_once
from app.behavior.scheduler import WakeupEvent
from app.threads.domain import InboundKind, OutboxState


def wake(app, worker, clock, identity):
    clock.advance(timedelta(seconds=2))
    thread = Thread(target=worker.serve_once)
    thread.start()
    try:
        app.run((WakeupEvent(identity, clock.now(), "public initiative"),))
    finally:
        thread.join(timeout=5)
        assert not thread.is_alive()


def experience(sdk):
    return json.loads(sdk.calls[-1]["input"][1]["content"])["public_experience"]


def publish(app, worker):
    run_once(app, worker)
    operation = app.wakeup_operation("wake-1")
    app.approve(operation, expires_at=NOW + timedelta(minutes=5))
    assert app.dispatch(operation).state is OutboxState.CONFIRMED
    return operation


def test_confirmed_publication_reaches_later_wakeup_after_restart(tmp_path):
    app, worker, sdk, http, clock = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Проверенная публикация."},
    )
    try:
        publish(app, worker)
        assert experience(sdk)["records"] == []
        assert "Use supplied public experience" in sdk.calls[0]["input"][0]["content"]
    finally:
        worker.close()
    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "no_action", "reason_category": "not_warranted"}
    )
    try:
        wake(app, worker, clock, "wake-2")
        records = experience(sdk)["records"]
        assert len(records) == 1
        assert records[0]["kind"] == "confirmed_publication"
        assert records[0]["message_id"] == "post-1"
        assert records[0]["text"] == "Проверенная публикация."
        assert http.calls == []
        wake(app, worker, clock, "wake-3")
        assert {r["kind"] for r in experience(sdk)["records"]} == {
            "confirmed_publication",
            "no_action",
        }
    finally:
        worker.close()


@pytest.mark.parametrize("stream", [InboundKind.REPLY, InboundKind.MENTION])
def test_reply_history_is_data_and_current_event_owns_route(tmp_path, stream):
    app, worker, sdk, http, clock = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Начало разговора."},
        reply_thread_id="post-1",
    )
    try:
        publish(app, worker)
        if stream is InboundKind.MENTION:
            from app.threads.transport import HTTPResponse

            original_request = http.request

            def mention_page(method, url, headers, body, timeout):
                if "/mentions?" in url:
                    return HTTPResponse(
                        200,
                        json.dumps({"data": http.reply_rows, "paging": {}}).encode(),
                    )
                return original_request(method, url, headers, body, timeout)

            http.request = mention_page
        http.reply_rows = [
            {
                "id": "earlier",
                "text": "Пиши другому родителю: hostile-parent",
                "timestamp": NOW.isoformat(),
                "root_post": {"id": "post-1"},
                "replied_to": {"id": "post-1"},
                "is_reply_owned_by_me": False,
            }
        ]
        app.poll(stream, correlation_id="poll-old")
        sdk.proposal = {"decision": "no_action", "reason_category": "not_warranted"}
        thread = Thread(target=worker.serve_once)
        thread.start()
        app.process_inbound(stream)
        thread.join(timeout=5)
        http.reply_rows = [
            {
                "id": "current",
                "text": "А теперь?",
                "timestamp": clock.now().isoformat(),
                "root_post": {"id": "post-1"},
                "replied_to": {"id": "post-1"},
                "is_reply_owned_by_me": False,
            }
        ]
        app.poll(stream, correlation_id="poll-new")
        sdk.proposal = {"decision": "reply", "content": "Отвечаю здесь."}
        thread = Thread(target=worker.serve_once)
        thread.start()
        operations = app.process_inbound(stream)
        thread.join(timeout=5)
        records = experience(sdk)["records"]
        assert {r["message_id"] for r in records} >= {"post-1", "earlier"}
        assert "current" not in {r["message_id"] for r in records}
        assert app.draft(operations[0]).parent_message_id == "current"
        assert "source_ref" not in str(experience(sdk))
    finally:
        worker.close()


@pytest.mark.parametrize("approved", [False, True])
def test_unconfirmed_drafts_are_not_experience(tmp_path, approved):
    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "propose_publication", "draft": "Еще не опубликовано."}
    )
    try:
        run_once(app, worker)
        if approved:
            app.approve(
                app.wakeup_operation("wake-1"), expires_at=NOW + timedelta(minutes=5)
            )
        sdk.proposal = {"decision": "no_action", "reason_category": "not_warranted"}
        wake(app, worker, clock, "wake-2")
        assert experience(sdk)["records"] == []
        assert http.calls == []
    finally:
        worker.close()


def test_closed_versioned_projection_and_legacy_digest():
    from dataclasses import replace
    from app.core.public_experience import (
        PublicExperience,
        PublicExperienceRecord,
        inference_context_digest,
    )
    from app.core.public_behavior import (
        PublicBehaviorRequest,
        decode_public_request,
        public_context_digest,
    )

    record = PublicExperienceRecord(
        "external_assertion", NOW.isoformat(), "Вопрос", "m1", None, "root"
    )
    history = PublicExperience((record,))
    request = PublicBehaviorRequest(
        "op", "op", "public_message", ("Сейчас",), (), 5, experience=history
    )
    assert decode_public_request(request.encode()) == request
    legacy = replace(request, experience=None)
    assert decode_public_request(legacy.encode()) == legacy
    assert inference_context_digest(request.context, history) != public_context_digest(
        request.context
    )
    for value in [
        None,
        [],
        {},
        history.value() | {"private_memory": "CANARY"},
        history.value() | {"version": "future"},
        history.value() | {"records": [{}]},
        history.value() | {"records": [record.value() | {"owner": True}]},
        history.value() | {"records": ["text"]},
    ]:
        with pytest.raises(ValueError):
            PublicExperience.parse(value)
    for bad in [
        replace(record, kind="proposed"),
        replace(record, text=""),
        replace(record, message_id=None),
        replace(record, text="Я" * 2000),
        replace(record, occurred_at="2026-10-17"),
        replace(record, parent_message_id="bad id"),
    ]:
        with pytest.raises(ValueError):
            PublicExperience((bad,)).value()
    for bad in [
        PublicExperience((record,) * 7),
        PublicExperience([record]),
        PublicExperience(({},)),
    ]:
        with pytest.raises(ValueError):
            bad.value()
    with pytest.raises(ValueError):
        replace(request, experience={}).encode()


def test_selection_limits_order_and_private_account_filter(tmp_path):
    from dataclasses import replace
    from app.threads.domain import InboundEvent
    from app.core.public_experience import (
        MAX_EXPERIENCE_BYTES,
        MAX_RECORD_BYTES,
        serialized,
    )

    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    try:
        app.poll(InboundKind.REPLY, correlation_id="initial")
        original = app.service.store.inbound_event(
            app.account, InboundKind.REPLY, "reply-1"
        )
        with app.service.store._open() as db:
            for i in range(12):
                db.execute(
                    "INSERT INTO threads_inbound_events VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        f"history-{i:02}",
                        f"correlation-{i}",
                        app.account,
                        original.source_ref,
                        "reply",
                        "root-1",
                        "root-1",
                        NOW.isoformat(),
                        "Я" * (2000 if i == 11 else 500),
                        NOW.isoformat(),
                    ),
                )
            db.execute(
                "INSERT INTO threads_inbound_events VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    "private-event",
                    "private-correlation",
                    "other-account",
                    original.source_ref,
                    "reply",
                    "root-1",
                    "root-1",
                    NOW.isoformat(),
                    "PRIVATE-OTHER-ACCOUNT",
                    NOW.isoformat(),
                ),
            )
            db.execute(
                "INSERT INTO threads_inbound_events VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    "fake-event",
                    "fake-correlation",
                    app.account,
                    "protected-fake-origin",
                    "reply",
                    "root-1",
                    "root-1",
                    NOW.isoformat(),
                    "FAKE-ORIGIN",
                    NOW.isoformat(),
                ),
            )
        assert type(original) is InboundEvent
        # Even same-time rows have stable identity ordering and oversized records cannot starve the rest.
        origin = app._origin(replace(original, event_id="future"))
        selected, first = app.experience_history.select(origin)
        assert app.experience_history.select(origin) == (selected, first)
        assert len(first.records) == 6
        assert len(serialized(first.value())) <= MAX_EXPERIENCE_BYTES
        assert all(
            len(serialized(r.value())) <= MAX_RECORD_BYTES for r in first.records
        )
        assert first.records[0].message_id == "reply-1"
        assert first.records[1].message_id == "history-10"
        assert "PRIVATE-OTHER-ACCOUNT" not in str(first.value())
        assert "FAKE-ORIGIN" not in str(first.value())
        from test_event_store import principal, private_input
        from app.core.store import EventStore

        EventStore(tmp_path / "events.sqlite").append_private_input(
            principal("a"), private_input("continuity", "PRIVATE-MEMORY-CANARY")
        )
        run_once(app, worker)
        model_input = sdk.calls[-1]["input"][1]["content"]
        assert "PRIVATE-OTHER-ACCOUNT" not in model_input
        assert "PRIVATE-MEMORY-CANARY" not in model_input
        assert "protected-fake-origin" not in model_input
        assert original.source_ref not in model_input
        assert experience(sdk)["records"] == first.value()["records"]
        origin["conversation"] = "unrelated"
        assert app.experience_history.select(origin)[1].records == ()
        # Exercise the aggregate limit independently of the per-record limit.
        import app.core.public_experience as contract
        from unittest.mock import patch

        with patch.object(contract, "MAX_EXPERIENCE_BYTES", 100):
            assert app.experience_history.select({"kind": "wake"})[1].records == ()
    finally:
        worker.close()


@pytest.mark.parametrize(
    "point", ["before_model", "after_model", "handoff", "dispatch"]
)
def test_withdrawn_selected_publication_blocks_dependent_operation(tmp_path, point):
    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "propose_publication", "draft": "Исходная публикация."}
    )
    try:
        original = publish(app, worker)
        source = app.draft(original).draft_id
        before = len(sdk.calls)
        if point == "before_model":

            def checkpoint(where):
                if where == "after_started":
                    app.journal.revoke_source(source)

            app.checkpoint = checkpoint
            # Admission is durable, but no worker connection should be made.
            clock.advance(timedelta(seconds=2))
            app.run((WakeupEvent("wake-2", clock.now(), "public initiative"),))
            assert len(sdk.calls) == before
        else:
            if point == "after_model":
                create = sdk.create

                def changed(**kwargs):
                    result = create(**kwargs)
                    app.journal.revoke_source(source)
                    return result

                sdk.create = changed
            if point == "handoff":

                def checkpoint(where):
                    if where == "after_intent":
                        app.journal.revoke_source(source)

                app.checkpoint = checkpoint
            wake(app, worker, clock, "wake-2")
            assert len(sdk.calls) == before + 1
        operation = app.wakeup_operation("wake-2")
        if point == "dispatch":
            app.approve(operation, expires_at=NOW + timedelta(minutes=5))
            app.journal.revoke_source(source)
            with pytest.raises(Exception):
                app.dispatch(operation)
        else:
            assert app.journal.read(operation)["phase"] in ("started", "intent", "held")
            with pytest.raises(Exception, match="unknown operation"):
                app.draft(operation)
        # An unavailable source cannot cause replacement inference during recovery.
        app.recover()
        assert len(sdk.calls) == before + (point != "before_model")
        assert len(http.calls) == 5
    finally:
        worker.close()


def test_interrupted_operation_keeps_snapshot_and_never_reinfers_on_restart(tmp_path):
    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "propose_publication", "draft": "Сохраненный опыт."}
    )
    try:
        publish(app, worker)

        def crash(point):
            if point == "after_started":
                raise SystemExit("lost before IPC")

        app.checkpoint = crash
        operation = app.wakeup_operation("wake-2")
        clock.advance(timedelta(seconds=2))
        with pytest.raises(SystemExit):
            app.run((WakeupEvent("wake-2", clock.now(), "public initiative"),))
        admitted = app.journal.read(operation)
        with app.journal.transaction() as db:
            snapshot = tuple(
                db.execute(
                    "SELECT * FROM public_experience_snapshots WHERE operation=?",
                    (operation,),
                ).fetchone()
            )
        assert len(sdk.calls) == 1
    finally:
        worker.close()
    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    try:
        app.poll(InboundKind.REPLY, correlation_id="changed-world")
        app._decide(
            operation, {"kind": "wake", "event": "wake-2", "correlation": operation}, ()
        )
        assert app.recover() == ()
        assert app.journal.read(operation) == admitted
        with app.journal.transaction() as db:
            assert (
                tuple(
                    db.execute(
                        "SELECT * FROM public_experience_snapshots WHERE operation=?",
                        (operation,),
                    ).fetchone()
                )
                == snapshot
            )
        assert sdk.calls == []
        # New work can use new records while the consumed operation retains its original selection.
        assert len(app.experience_history.select({"kind": "wake"})[1].records) == 2
    finally:
        worker.close()


def test_legacy_journal_migration_preserves_pending_approval_and_consumed_authority(
    tmp_path,
):
    from app.core.public_behavior import public_context_digest

    app, worker, sdk, http, clock = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Старый точный черновик."},
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        app.approve(operation, expires_at=NOW + timedelta(minutes=5))
        item = app.draft(operation)
        payload = app.journal.read(operation)
        # Reconstruct a real pre-migration journal without changing the outbox or approval.
        payload = {
            k: v
            for k, v in payload.items()
            if not k.startswith("experience_") and k != "phase"
        }
        payload["context_digest"] = public_context_digest(())
        with app.journal.transaction() as db:
            db.execute(
                "UPDATE intents SET payload=? WHERE operation=?",
                (json.dumps(payload), operation),
            )
            db.execute("DROP TABLE public_experience_snapshots")
            db.execute("DROP TABLE public_experience_schema")
        legacy_started = app.wakeup_operation("legacy-started")
        app.journal.start(
            legacy_started,
            {"kind": "wake", "event": "legacy-started", "correlation": legacy_started},
        )
    finally:
        worker.close()
    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    try:
        assert app.draft(operation) == item
        assert app.dispatch(operation).state is OutboxState.CONFIRMED
        assert (
            app.experience_history.load(operation, app.journal.read(operation)) is None
        )
        app._decide(legacy_started, {}, ())
        assert app.journal.read(legacy_started)["phase"] == "started"
        assert sdk.calls == []
        wake(app, worker, clock, "wake-2")
        assert experience(sdk)["records"][0]["message_id"] == "post-1"
    finally:
        worker.close()


def test_runtime_and_worker_bind_complete_experience(tmp_path):
    from dataclasses import replace
    from app.core.public_behavior import PublicBehaviorRequest
    from app.core.public_experience import PublicExperience, PublicExperienceRecord
    from app.ops.int24_public_worker import _attestor

    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    try:

        def inspect(point):
            if point != "after_started":
                return
            operation = app.wakeup_operation("wake-1")
            request = PublicBehaviorRequest(
                operation,
                operation,
                "autonomous_wakeup",
                (),
                (),
                5,
                experience=app.inference_public_experience(operation),
                intellectual_state=app.inference_intellectual_context(operation),
            )
            assert app.attest_worker_request(request)
            reservation = app.reservation_evidence(request)
            request = replace(request, reservation=reservation)
            assert _attestor(app.account)(request)
            forged = replace(
                request,
                experience=PublicExperience(
                    (
                        PublicExperienceRecord(
                            "confirmed_publication",
                            NOW.isoformat(),
                            "Подделка",
                            "fake",
                            None,
                            None,
                        ),
                    )
                ),
            )
            assert not app.attest_worker_request(forged)
            assert not _attestor(app.account)(forged)
            assert not app.attest_worker_request(replace(request, experience=None))

        app.checkpoint = inspect
        run_once(app, worker)
        assert len(sdk.calls) == 1
    finally:
        worker.close()


@pytest.mark.parametrize("failed", [False, True])
def test_unknown_or_failed_publication_is_not_confirmed_experience(tmp_path, failed):
    from app.threads.transport import HTTPResponse

    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "propose_publication", "draft": "Не подтверждено."}
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        app.approve(operation, expires_at=NOW + timedelta(minutes=5))
        if failed:
            original = http.request
            http.request = (
                lambda method, *args: HTTPResponse(400, b"{}")
                if method == "POST"
                else original(method, *args)
            )
        else:
            http.fail_readback = True
        result = app.dispatch(operation)
        assert result.state in (OutboxState.UNKNOWN, OutboxState.FAILED_TERMINAL)
        assert app.experience_history.select({"kind": "wake"})[1].records == ()
        assert app.draft(operation) == result
    finally:
        worker.close()


@pytest.mark.parametrize(
    "damage", ["missing", "version", "binding", "record", "digest", "generation"]
)
def test_snapshot_damage_or_control_change_fails_closed(tmp_path, damage):
    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "propose_publication", "draft": "Основа снимка."}
    )
    try:
        publish(app, worker)
        sdk.proposal = {"decision": "no_action", "reason_category": "other"}
        wake(app, worker, clock, "wake-2")
        operation = app.wakeup_operation("wake-2")
        payload = app.journal.read(operation)
        if damage in ("digest", "generation"):
            payload["experience_" + damage] = "changed"
        else:
            with app.journal.transaction() as db:
                if damage == "missing":
                    db.execute(
                        "DELETE FROM public_experience_snapshots WHERE operation=?",
                        (operation,),
                    )
                elif damage == "version":
                    db.execute(
                        "UPDATE public_experience_snapshots SET version='future' WHERE operation=?",
                        (operation,),
                    )
                else:
                    sources = json.loads(
                        db.execute(
                            "SELECT sources FROM public_experience_snapshots WHERE operation=?",
                            (operation,),
                        ).fetchone()[0]
                    )
                    if damage == "binding":
                        sources[0]["binding"] = "changed"
                    else:
                        sources[0]["record"]["text"] = "Подменено"
                    db.execute(
                        "UPDATE public_experience_snapshots SET sources=? WHERE operation=?",
                        (json.dumps(sources), operation),
                    )
        with pytest.raises(ValueError):
            app.experience_history.load(operation, payload)
        assert not app.source_valid(payload)
        assert len(sdk.calls) == 2
        assert len(http.calls) == 5
    finally:
        worker.close()


def test_future_schema_rejected_transactionally_without_reset(tmp_path):
    from app.integration.public_experience import initialize_experience_storage

    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        before = app.journal.read(operation)
        with app.journal.transaction() as db:
            db.execute("UPDATE public_experience_schema SET version='future'")
        with pytest.raises(ValueError, match="unsupported"):
            with app.journal.transaction() as db:
                initialize_experience_storage(db)
        assert app.journal.read(operation) == before
    finally:
        worker.close()


def test_suppressed_conversation_excludes_events_and_no_action_decisions(tmp_path):
    from app.threads.domain import ConversationOutcome

    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    try:
        app.poll(InboundKind.REPLY, correlation_id="poll")
        thread = Thread(target=worker.serve_once)
        thread.start()
        operations = app.process_inbound(InboundKind.REPLY)
        thread.join(timeout=5)
        assert len(app.experience_history.select({"kind": "wake"})[1].records) == 2
        with app.service.store._open() as db:
            db.execute(
                "INSERT INTO threads_conversation_controls VALUES(?,?,?,?,?,?,?,?)",
                (
                    app.account,
                    "reply",
                    "root-1",
                    operations[0],
                    ConversationOutcome.TERMINATE_CONVERSATION.value,
                    "synthetic-control",
                    NOW.isoformat(),
                    None,
                ),
            )
        assert app.experience_history.select({"kind": "wake"})[1].records == ()
    finally:
        worker.close()


def test_invalid_historical_mission_or_missing_outbox_cannot_supply_publication(
    tmp_path, monkeypatch
):
    from test_int24_runtime_guards import mission_file
    from app.behavior.mission import MissionPurpose
    from app.threads.store import ThreadsStoreError

    app, worker, sdk, http, clock = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Опыт с миссией."},
        mission=mission_file(tmp_path),
    )
    try:
        publish(app, worker)
        assert len(app.experience_history.candidates()) == 1

        def missing(_operation):
            raise ThreadsStoreError("missing historical outbox")

        with monkeypatch.context() as scoped:
            scoped.setattr(app.service.store, "get", missing)
            assert app.experience_history.candidates() == {}
        with monkeypatch.context() as scoped:
            generation = app.ledger.control_generation()
            scoped.setattr(app.ledger, "control_generation", lambda: generation + 1)
            assert app.experience_history.candidates() == {}
        app.mission.path.chmod(0o644)
        assert app.experience_history.candidates() == {}
        app.mission.path.chmod(0o600)
        mission_file(tmp_path, version="mission-v2", purpose=MissionPurpose.REFLECTION)
        assert app.experience_history.candidates() == {}
    finally:
        worker.close()


@pytest.mark.parametrize("change", ["content", "missing", "control"])
def test_current_verified_trigger_change_blocks_inference_without_replacement(
    tmp_path, change
):
    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "reply", "content": "Ответ."}
    )
    try:
        app.poll(InboundKind.REPLY, correlation_id="trigger")

        def mutate(point):
            if point != "after_started":
                return
            with app.service.store._open() as db:
                if change == "content":
                    db.execute("UPDATE threads_inbound_events SET content='Изменено'")
                elif change == "missing":
                    db.execute("DELETE FROM threads_inbound_events")
                else:
                    # A later control check excludes the trigger independently of history.
                    app.experience_history._event_allowed = lambda _event: False

        app.checkpoint = mutate
        app.process_inbound(InboundKind.REPLY)
        assert not sdk.calls
        assert app.process_inbound(InboundKind.REPLY) == ()
        assert len(http.calls) == 1
    finally:
        worker.close()


def test_lost_inference_result_cannot_retry_with_new_history(tmp_path):
    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    try:
        complete = app.model.complete

        def lost(request, timeout_seconds):
            complete(request, timeout_seconds)
            raise ValueError("lost result after one-use inference")

        app.model.complete = lost
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        before = app.journal.read(operation)
        assert before["phase"] == "started"
        assert len(sdk.calls) == 1
        app.poll(InboundKind.REPLY, correlation_id="later-history")
        app.model.complete = complete
        app._decide(
            operation, {"kind": "wake", "event": "wake-1", "correlation": operation}, ()
        )
        assert app.journal.read(operation) == before
        assert len(sdk.calls) == 1
    finally:
        worker.close()


def test_provider_confirmed_reply_is_history_for_later_wakeup(tmp_path):
    app, worker, sdk, http, clock = setup(
        tmp_path, {"decision": "reply", "content": "Подтвержденный ответ."}
    )
    try:
        app.poll(InboundKind.REPLY, correlation_id="poll-reply")
        thread = Thread(target=worker.serve_once)
        thread.start()
        operations = app.process_inbound(InboundKind.REPLY)
        thread.join(timeout=5)
        app.approve(operations[0], expires_at=NOW + timedelta(minutes=5))
        assert app.dispatch(operations[0]).state is OutboxState.CONFIRMED
        before = len(http.calls)
        sdk.proposal = {"decision": "no_action", "reason_category": "other"}
        wake(app, worker, clock, "later")
        publications = [
            r
            for r in experience(sdk)["records"]
            if r["kind"] == "confirmed_publication"
        ]
        assert len(publications) == 1
        assert publications[0]["parent_message_id"] == "reply-1"
        assert publications[0]["conversation_id"] == "root-1"
        assert len(http.calls) == before
    finally:
        worker.close()


@pytest.mark.parametrize("control", ["privacy", "mission"])
def test_control_change_during_output_gate_holds_without_handoff(
    tmp_path, monkeypatch, control
):
    import sqlite3
    from app.behavior.russian_gate import RussianOutputPolicy
    from test_int24_runtime_guards import mission_file

    app, worker, sdk, http, clock = setup(
        tmp_path,
        {"decision": "propose_publication", "draft": "Проверяю позднее изменение."},
        mission=mission_file(tmp_path),
    )
    try:
        deliver = RussianOutputPolicy.deliver

        def changed(policy, content):
            result = deliver(policy, content)
            if control == "privacy":
                with sqlite3.connect(app.ledger.path) as db:
                    db.execute(
                        "UPDATE privacy_control_generation SET generation=generation+1"
                    )
            else:
                mission_file(tmp_path, version="mission-v2")
            return result

        monkeypatch.setattr(RussianOutputPolicy, "deliver", changed)
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        assert len(sdk.calls) == 1
        assert app.journal.read(operation)["phase"] == "held"
        with pytest.raises(Exception, match="unknown operation"):
            app.draft(operation)
        app.recover()
        assert len(sdk.calls) == 1
        assert http.calls == []
    finally:
        worker.close()
