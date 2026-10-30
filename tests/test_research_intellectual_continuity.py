"""Offline SDK/HTTP/IPC continuity, not real-model epistemic acceptance."""

from datetime import timedelta
import json
import os
from threading import Event, Thread

import pytest

from app.core.intellectual_state import IntellectualStateStore, initialize
from test_agent_intellectual_state import (
    private_setup,
    private_submit,
    proposal,
    source,
)
from test_agent_research_questions import authority
from test_b05b_public_runtime import _setup, _attach_fake_research
from test_public_experience import wake


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


def fields(kind, *, revised=False):
    if kind == "belief_or_hypothesis":
        return {
            "proposition": "Результаты могут зависеть от условий выборки.",
            "stance": "uncertain" if revised else "supports",
            "confidence": 0.3 if revised else 0.8,
            "status": "active",
            "alternatives": ["Различия могли возникнуть случайно."],
        }
    return {
        "description": "Разобраться в различиях между выборками.",
        "rationale": "Противоречие требует дополнительных данных.",
        "priority": 30 if revised else 60,
        "status": "suspended" if revised else "active",
        "constraints": ["Не делать причинный вывод из одной выборки."],
        "review_condition": "Когда появятся независимые данные.",
    }


def assessment_change(value, kind, *, retain=False):
    report = next(
        s for s in value["sources"] if s["kind"] == "hosted_research_model_report"
    )
    change = proposal(
        report["handle"],
        target=value["states"][0]["handle"],
        kind=kind,
        fields=fields(kind, revised=not retain),
    )
    change["reason"] = "Оценил противоречие и ограничения источников."
    change["conflicting"] = [report["handle"]]
    change["supporting"] = []
    return change


@pytest.mark.parametrize("kind", ["belief_or_hypothesis", "goal_or_intention"])
@pytest.mark.parametrize("retain", [False, True])
def test_public_report_can_revise_or_retain_state_in_next_sdk_call(
    tmp_path, monkeypatch, kind, retain
):
    app, worker, sdk, http, clock = _setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    calls = _attach_fake_research(app, tmp_path, monkeypatch)
    try:
        app.poll(
            __import__(
                "app.threads.domain", fromlist=["InboundKind"]
            ).InboundKind.REPLY,
            correlation_id="poll",
        )
        sdk.proposal["intellectual"] = proposal(kind=kind, fields=fields(kind))
        wake(app, worker, clock, "initial-position")
        sdk.proposal = {"decision": "research", "topic_id": "ocean"}
        wake(app, worker, clock, "research")
        # A report is not a second inference and not a forced state change.
        assert len(sdk.calls) == 2 and len(calls) == 1
        sdk.proposal = {"decision": "no_action", "reason_category": "other"}
        original = sdk.create

        def choose(**kwargs):
            value = json.loads(kwargs["input"][1]["content"])
            assert value["prior_untrusted_public_evidence"]
            sdk.proposal["intellectual"] = assessment_change(
                value["intellectual_state"], kind, retain=retain
            )
            return original(**kwargs)

        monkeypatch.setattr(sdk, "create", choose)
        wake(app, worker, clock, "assess")
        monkeypatch.setattr(sdk, "create", original)
        with app.journal.transaction() as db:
            rows = db.execute(
                "SELECT payload FROM agent_intellectual_revisions ORDER BY revision"
            ).fetchall()
            assert len(rows) == 2
            saved = json.loads(rows[-1][0])
            assert saved["fields"] == fields(kind, revised=not retain)
            assert saved["metadata"]["prompt"] == "agent-intellectual-state-prompt-v4"
            assert (
                next(
                    s
                    for s in saved["source_statuses"]
                    if s["handle"] in saved["conflicting"]
                )["kind"]
                == "hosted_research_model_report"
            )
        sdk.proposal = {"decision": "no_action", "reason_category": "other"}
        wake(app, worker, clock, "use-assessment")
        current = json.loads(sdk.calls[-1]["input"][1]["content"])["intellectual_state"]
        assert current["states"][0]["fields"] == saved["fields"]
        assert current["states"][0]["verification"] == "unverified"
        assert len(calls) == 1 and all(call[0] == "GET" for call in http.calls)
    finally:
        worker.close()


@pytest.fixture
def private_research(tmp_path, monkeypatch):
    from app.core.public_research_client import PublicResearchClient
    from app.core.public_research_profile import load_research_profile
    from app.core.provider_spend import ProviderStateStore
    from app.core.store import EventStore
    from app.private_web.public_research import PublicResearchBridge
    from app.private_web.runtime import WorkerConversationRuntime
    from app.trusted.public_research import PublicResearchBoundary, PublicResearchServer
    from app.trusted.public_research_transport import PublicResearchTransport
    from app.trusted.research_authority_journal import AuthorityJournal
    from app.trusted.openai_worker_server import WorkerServerConfig
    from test_int20_public_research import SDK
    from test_b05c_claim_evidence import assessed_raw

    app, a, b, events, ledger, gate, sdk, inference = private_setup(tmp_path)
    original_runtime = app._conversations
    now = original_runtime._clock()
    who = app._auth_store.authenticate(a.cookies["ar_session"]).trusted_context
    directory = tmp_path / "r"
    directory.mkdir(mode=0o700)
    path, value = authority(directory, role="application")
    value.update(
        principal_id=who.principal_id.value,
        authenticated_source_ref=who.authenticated_source.value,
        issued_at=now.isoformat(),
        reviewed_at=now.isoformat(),
        expires_at=(now + timedelta(days=1)).isoformat(),
    )
    path.write_text(json.dumps(value))
    worker_path = directory / "worker.json"
    worker_path.write_text(
        json.dumps(
            {
                k: v
                for k, v in (value | {"role": "worker"}).items()
                if k not in {"principal_id", "authenticated_source_ref"}
            }
        )
    )
    worker_path.chmod(0o600)
    journal_path = directory / "authority.sqlite"
    journal_path.touch(mode=0o600)
    search_sdk = SDK([assessed_raw()])
    boundary = PublicResearchBoundary(
        store=ProviderStateStore(EventStore(directory / "provider.db")),
        transport=PublicResearchTransport(search_sdk),
        enabled=True,
        utcnow=lambda: now,
    )
    boundary.install_profile(
        load_research_profile(worker_path, role="worker"),
        profile_path=worker_path,
        journal=AuthorityJournal(journal_path),
    )
    search = PublicResearchServer(
        WorkerServerConfig(directory / "search.sock", frozenset({os.geteuid()})),
        boundary,
    )
    search.start()

    def restart():
        bridge = PublicResearchBridge(
            events,
            gate,
            PublicResearchClient(
                directory / "search.sock", expected_worker_uid=os.geteuid()
            ),
            enabled=True,
            clock=original_runtime._clock,
            approval_path=path,
            application_scope="own-questions-test",
        )
        bridge.install_profile(load_research_profile(path, role="application"), who)
        app._conversations = WorkerConversationRuntime(
            events,
            gate,
            original_runtime._worker,
            provider_state=original_runtime._provider,
            research_bridge=bridge,
            clock=original_runtime._clock,
        )

    restart()
    stop = Event()
    thread = Thread(target=lambda: search.serve(should_stop=stop.is_set), daemon=True)
    thread.start()
    original_create = sdk.create
    sdk.research_next = False

    def choose(**kwargs):
        result = original_create(**kwargs)
        data = json.loads(result["output"][0]["content"][0]["text"])
        if sdk.research_next:
            data["proposal"] = {
                "decision": "research",
                "brief": "How do tectonic plates move?",
            }
        else:
            data["proposal"]["content"] += " PRIVATE_RESPONSE_CANARY_ZZQX"
        result["output"][0]["content"][0]["text"] = json.dumps(data)
        return result

    monkeypatch.setattr(sdk, "create", choose)
    try:
        yield (
            app,
            a,
            b,
            events,
            ledger,
            gate,
            sdk,
            inference,
            search_sdk,
            restart,
            who,
            path,
        )
    finally:
        stop.set()
        thread.join(5)
        assert not thread.is_alive()
        search.close()
        inference.close()


@pytest.mark.parametrize("kind", ["belief_or_hypothesis", "goal_or_intention"])
def test_private_full_report_counterevidence_and_revision_survive_restart(
    private_research, kind
):
    app, a, b, events, _, _, sdk, server, searches, restart, who, _ = private_research
    sdk.change = proposal(kind=kind, fields=fields(kind))
    private_submit(server, a, "position", "Обсудим разговор. PRIVATE_INPUT_CANARY_ZZQX")
    sdk.change = None
    sdk.research_next = True
    first = private_submit(
        server, a, "search", "Обсудим новое. PRIVATE_INPUT_CANARY_ZZQX"
    )
    assert first["model_state"] == "completed", first
    assert "contradicts" in first["response"]
    assert len(searches.calls) == 1
    sdk.research_next = False
    restart()

    def assess():
        data = json.loads(sdk.calls[-1]["input"])
        assert first["response"] in [
            segment["content"] for segment in data["conversation"]
        ]
        assert "same_operation_model" in sdk.calls[-1]["input"]
        assert "How do tectonic plates move?" in next(
            s["text"]
            for s in data["intellectual_state"]["sources"]
            if s["kind"] == "hosted_research_model_report"
        )
        sdk.change = assessment_change(data["intellectual_state"], kind)

    sdk.before_result = assess
    private_submit(server, a, "assess", "Какие выводы ты теперь делаешь?")
    sdk.before_result = lambda: None
    sdk.change = None
    restart()
    private_submit(server, a, "use", "Продолжим.")
    data = json.loads(sdk.calls[-1]["input"])
    assert data["intellectual_state"]["states"][0]["fields"] == fields(
        kind, revised=True
    )
    with events._open() as db:
        from app.privacy.application import _validate_graph

        _validate_graph(db, 14)
        assert (
            json.loads(
                db.execute(
                    "SELECT payload FROM agent_intellectual_revisions WHERE revision=2"
                ).fetchone()[0]
            )["metadata"]["prompt"]
            == "agent-intellectual-state-prompt-v4"
        )
    private_submit(server, b, "other", "Что ты узнал?")
    assert json.loads(sdk.calls[-1]["input"])["intellectual_state"]["states"] == []
    assert "https://example.org/study" not in sdk.calls[-1]["input"]
    assert len(searches.calls) == 1
    # Research-derived intellectual revisions remain source-bound even outside
    # the raw history window. This is current authorization, not erasure.
    from app.core.context import ContextLimits

    app._conversations._limits = ContextLimits(32_000, 1)
    path = private_research[-1]
    original_profile = path.read_text()
    value = json.loads(original_profile)
    value["authorization"] = "c" * 64
    path.write_text(json.dumps(value))
    private_submit(server, a, "withdrawn", "Обсудим продолжение.")
    assert "https://example.org/study" not in sdk.calls[-1]["input"]
    assert json.loads(sdk.calls[-1]["input"])["intellectual_state"]["states"] == []
    assert first["response"] not in [
        item.content for item in app._conversations.history(who)
    ]
    path.write_text(original_profile)

    from app.privacy.application import (
        EventStoreErasureAdapter,
        EventStoreRestoreAdapter,
    )
    from app.privacy.deletion import DeletionCoordinator
    from app.privacy.recovery import create_sqlite_backup, QuarantinedRestore
    from test_privacy_deletion import (
        context as deletion_context,
        request as deletion_request,
        NOW,
    )
    from pathlib import Path
    import sqlite3

    backup = Path(events.database_path).parent / "before-research-delete.db"
    create_sqlite_backup(events.database_path, backup, 14, NOW)
    ledger, gate = private_research[4:6]
    with events._open() as db:
        deleted = db.execute(
            "SELECT source_event_id FROM private_extraction_receipts WHERE operation_id LIKE 'researchq:%'"
        ).fetchone()[0]
    result = DeletionCoordinator(ledger, EventStoreErasureAdapter(events)).delete(
        deletion_context(), deletion_request(content_reference=deleted)
    )
    assert result.disposition.value == "applied" and gate.reconcile()
    quarantine = backup.parent / "quarantine.db"
    assert (
        QuarantinedRestore(ledger, 14, EventStoreRestoreAdapter)
        .restore(backup, quarantine, NOW)
        .activation_eligible
    )
    with sqlite3.connect(quarantine) as db:
        dump = "\n".join(db.iterdump())
        assert "https://example.org/study" not in dump
        assert db.execute(
            "SELECT payload FROM agent_intellectual_revisions WHERE revision=2"
        ).fetchone() == (None,)


def test_priority_report_handle_cannot_be_crowded_out_by_six_other_sources(tmp_path):
    import sqlite3

    with sqlite3.connect(tmp_path / "state.db") as db:
        initialize(db)
        state = IntellectualStateStore("public", "account")
        sources = {str(i): source() for i in range(8)}
        sources["report"] = source(
            "Full report is in the protected SDK context.",
            "hosted_research_model_report",
        )
        value = state.reserve(
            db, "op", sources, "context", "controls", priority_keys=("report",)
        )
        assert len(value["sources"]) == 6
        assert value["sources"][0]["kind"] == "hosted_research_model_report"


@pytest.mark.parametrize("failure", ["capacity", "budget"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_optional_report_is_omitted_whole_when_durable_context_cannot_fit(
    private_research, monkeypatch, failure, wrapped
):
    from app.core.context import ContextLimits
    from app.core.intellectual_state import IntellectualCapacityError

    app, a, _, _, _, _, sdk, server, searches, _, _, _ = private_research
    sdk.research_next = True
    first = private_submit(
        server, a, "research", "Обсудим новое. PRIVATE_FALLBACK_CANARY_ZZQX"
    )
    assert first["model_state"] == "completed"
    sdk.research_next = False
    helper = app._conversations._intellectual
    if failure == "capacity":

        def fail(*args, **kwargs):
            raise IntellectualCapacityError("synthetic complete-union capacity")

        monkeypatch.setattr(IntellectualStateStore, "reserve", fail)
    original = helper.attach

    def bounded(db, principal, operation, request, event_ids, **kwargs):
        if wrapped:
            from app.cognition.shared_context import SharedModelRequest

            request = SharedModelRequest(request, (), False)
        if failure == "budget":
            app._conversations._limits = ContextLimits(
                len(request.serialize().encode()) + 127, 99
            )
        return original(db, principal, operation, request, event_ids, **kwargs)

    monkeypatch.setattr(helper, "attach", bounded)
    result = private_submit(server, a, "continue", "Продолжим разговор.")
    assert result["model_state"] == "completed"
    value = json.loads(sdk.calls[-1]["input"])
    assert all(segment["role"] == "participant" for segment in value["conversation"])
    assert first["response"] not in sdk.calls[-1]["input"]
    assert "hosted_research_model_report" not in sdk.calls[-1]["input"]
    assert len(searches.calls) == 1


def test_withdrawn_episode_is_removed_before_fiction_ranking_without_hiding_memory(
    tmp_path,
):
    from app.cognition.narrative_store import NarrativeStore
    from app.cognition.conversation_context import MemoryConversationAssembler
    from app.core.context import ContextLimits
    from test_cognition_retrieval import retrieval_setup, NOW

    events, state, who, _, denied, allowed = retrieval_setup.__wrapped__(tmp_path)

    NarrativeStore(events).commit(
        who,
        operation_id="narrative:withdrawn",
        source_event_id=denied,
        text="Я стоял у вымышленного маяка.",
        item_id="episode:withdrawn",
        expected_revision=0,
        expected_heads=(),
        exposed_sources=(denied,),
        exposed_episodes=(),
        created_at=NOW,
    )
    with events._open() as db:
        value, _ = MemoryConversationAssembler(events, ContextLimits(32_000, 99)).build(
            who,
            allowed,
            db,
            episode_permitted=lambda _episode: False,
            include_narrative=True,
        )
    assert value.episodes == ()
    assert {m.text for m in value.memories} == {
        "Orbital mechanics",
        "Watercolor painting",
    }


def test_required_context_must_fit_protected_operation_union(tmp_path):
    import sqlite3
    from app.core.intellectual_state import IntellectualCapacityError

    with sqlite3.connect(tmp_path / "state.db") as db:
        initialize(db)
        state = IntellectualStateStore("public", "account")
        with pytest.raises(IntellectualCapacityError):
            state.reserve(
                db,
                "too-many-ancestors",
                {"event": source()},
                "context",
                "control",
                dependencies_fit=lambda _dependencies: False,
            )
        assert state._operation(db, "too-many-ancestors") is None


@pytest.mark.parametrize("withdraw_phase", [None, "before", "during"])
def test_research_ancestry_protects_narrative_sdk_and_later_episode(
    private_research, monkeypatch, withdraw_phase
):
    from app.private_web.runtime import WorkerConversationRuntime
    from app.cognition.narrative_store import NarrativeStore

    app, a, _, events, _, gate, sdk, server, searches, _, who, path = private_research
    sdk.research_next = True
    first = private_submit(
        server, a, "search", "Обсудим новое. PRIVATE_NARRATIVE_CANARY_ZZQX"
    )
    assert first["model_state"] == "completed"
    sdk.research_next = False
    old = app._conversations
    original = sdk.create
    server._narrative_enabled = True

    def narrate(**kwargs):
        result = original(**kwargs)
        request = json.loads(kwargs["input"])
        if request["task"] == "propose_private_narrative":
            assert first["response"] in [s["content"] for s in request["conversation"]]
            result["output"][0]["content"][0]["text"] = json.dumps(
                {
                    "proposal": {
                        "decision": "create",
                        "handle": None,
                        "episode": "Я видел вымышленный маяк RESEARCH_FICTION_CANARY.",
                    }
                }
            )
            if withdraw_phase == "during":
                withdraw()
        return result

    def withdraw():
        value = json.loads(path.read_text())
        value["authorization"] = "c" * 64
        path.write_text(json.dumps(value))

    def checkpoint(point):
        if withdraw_phase == "before" and point == "after_reservation":
            withdraw()

    monkeypatch.setattr(sdk, "create", narrate)
    app._conversations = WorkerConversationRuntime(
        events,
        gate,
        old._worker,
        provider_state=old._provider,
        research_bridge=old._research,
        clock=old._clock,
        narrative_enabled=True,
        narrative_permission_current=lambda: True,
        checkpoint=checkpoint,
    )
    stop = Event()
    thread = Thread(target=lambda: server.serve(should_stop=stop.is_set), daemon=True)
    thread.start()
    before = len(sdk.calls)
    try:
        result = json.loads(a.submit("fiction", "Создай вымышленную историю.")[2])
        if withdraw_phase is not None:
            assert len(sdk.calls) == before + (withdraw_phase == "during")
            assert result["model_state"] == "unavailable"
            assert NarrativeStore(events).current(who) == ()
        else:
            assert result["model_state"] == "completed", result
            assert len(sdk.calls) == before + 2  # Explicit narrative + ordinary reply.
            assert NarrativeStore(events).current(who)
            withdraw()
            app._conversations._checkpoint = lambda _point: None
            result = json.loads(a.submit("later", "Продолжим разговор.")[2])
            assert result["model_state"] == "completed", result
            assert "RESEARCH_FICTION_CANARY" not in sdk.calls[-1]["input"]
            assert json.loads(sdk.calls[-1]["input"])["fictional_episodes"] == []
        assert len(searches.calls) == 1
    finally:
        stop.set()
        thread.join(5)
        assert not thread.is_alive()
