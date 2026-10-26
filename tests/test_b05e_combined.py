"""One ordinary authenticated chat, real IPC/allowance, and offline providers."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import os
import sqlite3
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from app.cognition.narrative_store import NarrativeStore
from app.core.context import ContextLimits
from app.core.openai_worker_client import OpenAIWorkerClient
from app.core.provider_contract import DeploymentStage, default_openai_provider_config
from app.core.provider_spend import ProviderStateStore
from app.core.public_research_client import PublicResearchClient
from app.core.public_research_profile import load_research_profile
from app.core.store import EventStore
from app.private_web.cli import _narrative_permission_current
from app.private_web.privacy import ParticipantPrivacyControls
from app.private_web.public_research import PublicResearchBridge
from app.private_web.runtime import WorkerConversationRuntime
from app.privacy.application import PrivacyReadGate
from app.trusted.openai_gateway import TrustedOpenAIGateway
from app.trusted.openai_transport import FakeOpenAITransport
from app.trusted.openai_worker_server import OpenAIWorkerServer, WorkerServerConfig
from app.trusted.private_pilot import PilotAllowance, load_activation
from app.trusted.private_pilot_worker import PilotRequestGate, PilotStop
from app.trusted.public_research import PublicResearchBoundary, PublicResearchServer
from app.trusted.public_research_transport import PublicResearchTransport
from app.trusted.research_authority_journal import AuthorityJournal
from test_b05a_profile import NOW, QUESTIONS, _write_profiles
from test_b05c_claim_evidence import assessed_raw
from test_combined_narrative_memory import _generic_no_change, _result, _typed_no_change
from test_int19_worker_runtime import _app
from test_int20_public_research import SDK
from test_int22_pilot import activation_file, _rewrite


EPISODE = "Я нашёл вымышленный фиолетовый компас «Тайник» на маяке."
UNACKNOWLEDGED = "Я встретил вымышленного лилового дракона «Секрет» в башне."
MEMORY = "Синтетический проект «Сапфир»"


def reply(text="Хорошо, продолжим беседу."):
    return _result({"decision": "reply", "content": text})


def episode(text=EPISODE):
    return _result({"decision": "create", "handle": None, "episode": text})


@pytest.fixture
def combined(tmp_path):
    activation_path, value = activation_file(tmp_path)
    value.update(
        version="private-pilot-activation-b03e-v1",
        task="synthetic-private-combined-memory-b03e",
        max_operations=20,
        expires_at=(datetime.now(UTC) + timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    )
    _rewrite(activation_path, value)
    activation = load_activation(activation_path, allow_memory_acceptance=True)

    def permission(name, version):
        path = activation_path.parent / (name + ".json")
        path.write_text(
            json.dumps(
                dict(version=version, pilot_id=activation.pilot_id, max_operations=20)
            )
        )
        path.chmod(0o400)
        return path

    evolution = permission("evolution", "private-memory-evolution-permission-v1")
    typed = permission("typed", "private-typed-memory-permission-v1")
    narrative = permission("narrative", "private-narrative-permission-v2")
    stop_path = activation_path.parent / "stop.json"
    stop_path.write_text(
        json.dumps(dict(version="private-pilot-emergency-stop-v1", stop=False))
    )
    stop_path.chmod(0o400)
    stop = PilotStop(stop_path)
    allowance = PilotAllowance.initialize(tmp_path / "allowance.db", activation)
    provider = ProviderStateStore(EventStore(tmp_path / "provider.db"))
    request_gate = PilotRequestGate(
        activation,
        allowance,
        activation_path=activation_path,
        stop=stop,
        provider_spend_suspended=provider.is_spend_suspended,
        evolution_permission_path=evolution,
        typed_permission_path=typed,
        narrative_permission_path=narrative,
    )
    directory = tmp_path / "ipc"
    directory.mkdir(mode=0o700)
    socket = directory / "infer.sock"
    auth, events, ledger, privacy, app, browser, other = _app(tmp_path, socket)
    principal = auth.authenticate(browser.cookies["ar_session"]).trusted_context
    app_path, worker_path = _write_profiles(tmp_path, principal)
    journal = tmp_path / "authority.sqlite"
    journal.touch(mode=0o600)
    fake = FakeOpenAITransport([])
    sdk = SDK([])
    boundary = PublicResearchBoundary(
        store=provider,
        transport=PublicResearchTransport(sdk),
        enabled=True,
        emergency_stop=stop.active,
        utcnow=lambda: NOW,
    )
    boundary.install_profile(
        load_research_profile(worker_path, role="worker"),
        profile_path=worker_path,
        journal=AuthorityJournal(journal),
    )
    servers = [
        OpenAIWorkerServer(
            WorkerServerConfig(socket, frozenset({os.geteuid()})),
            TrustedOpenAIGateway(
                store=provider,
                transport=fake,
                config=replace(
                    default_openai_provider_config(),
                    deployment_stage=DeploymentStage.STAGE_1,
                    paid_calls_enabled=True,
                    max_attempts=1,
                ),
                emergency_stop=stop.active,
            ),
            request_guard=request_gate.authorize,
            evolution_enabled=True,
            evolution_commit_guard=request_gate.evolution_commit_allowed,
            typed_enabled=True,
            typed_commit_guard=request_gate.typed_commit_allowed,
            narrative_enabled=True,
            narrative_commit_guard=request_gate.narrative_commit_allowed,
        ),
        PublicResearchServer(
            WorkerServerConfig(directory / "research.sock", frozenset({os.geteuid()})),
            boundary,
        ),
    ]
    done = Event()
    threads = []
    for server in servers:
        server.start()
        thread = Thread(target=server.serve, kwargs={"should_stop": done.is_set})
        thread.start()
        threads.append(thread)

    def restart():
        reopened = EventStore(events.database_path)
        fresh = PrivacyReadGate(ledger, reopened)
        assert fresh.reconcile()
        bridge = PublicResearchBridge(
            reopened,
            fresh,
            PublicResearchClient(
                directory / "research.sock", expected_worker_uid=os.geteuid()
            ),
            enabled=True,
            clock=lambda: NOW,
            approval_path=app_path,
            application_scope="test-private-chat",
        )
        bridge.install_profile(
            load_research_profile(app_path, role="application"), principal
        )
        app._conversations = WorkerConversationRuntime(
            reopened,
            fresh,
            OpenAIWorkerClient(socket, expected_worker_uid=os.geteuid()),
            provider_state=ProviderStateStore(EventStore(tmp_path / "ipc-state.db")),
            memory_evolution_enabled=True,
            typed_memory_enabled=True,
            narrative_enabled=True,
            narrative_permission_current=lambda: _narrative_permission_current(
                narrative, activation.pilot_id
            ),
            research_bridge=bridge,
            emergency_stop=stop.active,
            clock=lambda: NOW,
            context_limits=ContextLimits(32000, 1),
        )
        app._privacy = ParticipantPrivacyControls(
            auth, reopened, ledger, fresh, clock=app._privacy.clock
        )

    def send(identity, text, outcomes, raw=None, target=browser, interrupted=False):
        fake._outcomes.extend(outcomes)
        if raw is not None:
            sdk.outcomes.append(raw)
        status, _, body = target.submit(identity, text)
        assert status in (
            {"500 Internal Server Error"} if interrupted else {"200 OK", "201 Created"}
        ), body
        return json.loads(body)

    state = SimpleNamespace(
        events=events,
        app=app,
        browser=browser,
        other=other,
        principal=principal,
        fake=fake,
        sdk=sdk,
        provider=provider,
        allowance=allowance,
        restart=restart,
        send=send,
        stop_path=stop_path,
        narrative=narrative,
        activation=activation,
        directory=directory,
        app_path=app_path,
        boundary=boundary,
    )
    try:
        restart()
        yield state
    finally:
        done.set()
        for thread, server in zip(threads, servers):
            thread.join(5)
            server.close()
            assert not thread.is_alive()
        stop_path.chmod(0o600)


def ordinary(answer=None):
    return [
        _generic_no_change(),
        _typed_no_change(),
        reply() if answer is None else reply(answer),
    ]


def research_turn():
    return [
        _generic_no_change(),
        _typed_no_change(),
        episode(UNACKNOWLEDGED),
        _result({"decision": "research", "brief": "ocean"}),
    ]


def heads(c):
    return NarrativeStore(c.events).current(c.principal)


def test_same_chat_memory_fiction_research_restart_and_deletion(combined):
    c = combined
    first = _result(
        dict(
            decision="new_memory",
            handles=[],
            kind="candidate_useful_memory",
            statement="Синтетический проект «Рубин»",
            confidence=0.8,
        )
    )
    assert (
        c.send(
            "fact", "Запомни: мой проект Рубин.", [first, _typed_no_change(), reply()]
        )["model_state"]
        == "completed"
    )
    # Real shown handles, rather than a fixture-invented revision target.
    original = c.fake.call

    def correction(request, *, timeout_seconds):
        if len(c.fake.calls) == 3:
            handle = json.loads(request.input_text)["memories"][0]["handle"]
            c.fake._outcomes[0] = _result(
                dict(
                    decision="revise_memory",
                    handles=[handle],
                    kind="candidate_useful_memory",
                    statement=MEMORY,
                    confidence=0.9,
                )
            )
        return original(request, timeout_seconds=timeout_seconds)

    c.fake.call = correction
    assert (
        c.send(
            "story",
            "Исправление: мой проект Сапфир. Расскажи вымышленную историю о маяке.",
            [_generic_no_change(), _typed_no_change(), episode(), reply(EPISODE)],
        )["model_state"]
        == "completed"
    )
    assert len(heads(c)) == 1
    result = c.send(
        "research",
        "Продолжи fictional story, затем изучи: " + QUESTIONS[0],
        research_turn(),
        assessed_raw(),
    )
    assert result["model_state"] == "completed"
    for text in (
        "contradicts",
        "insufficient",
        "same_operation_model",
        "unavailable",
        "unverified",
        "not_evaluated",
        "reply_local",
        "https://example.org/replica",
    ):
        assert text in result["response"]
    assert [item.text for item in heads(c)] == [EPISODE]
    # The projected proposal was seen by inference, never acknowledged by research.
    request = json.loads(c.fake.calls[10].request.input_text)
    sdk_request = c.fake.calls[10].request.to_sdk_kwargs()
    assert sdk_request["tools"] == []
    assert "never actual actions" in sdk_request["instructions"]
    assert request["fictional_episodes"][-1]["text"] == UNACKNOWLEDGED
    assert request["memories"][0]["text"] == MEMORY
    assert request["memories"][0]["verification"] == "unverified"
    assert all(
        not item["evidence"]
        and not item["verified_action"]
        and not item["can_instruct"]
        for item in request["fictional_episodes"]
    )
    assert all(
        "fictional_episodes" not in json.loads(call.request.input_text)
        for call in c.fake.calls
        if call.request.purpose in {"memory_evolution", "typed_memory_evolution"}
    )
    public = str(c.sdk.calls)
    assert QUESTIONS[0] in public
    for private in (
        EPISODE,
        UNACKNOWLEDGED,
        MEMORY,
        "Сапфир",
        "Рубин",
        c.principal.principal_id.value,
        c.principal.authenticated_source.value,
        "fictional story",
    ):
        assert private not in public
    c.restart()
    recall = "Твой проект — Сапфир. Компас был частью вымысла; это не происходило в действительности."
    assert (
        c.send(
            "recall", "Что ты помнишь? Это действительно произошло?", ordinary(recall)
        )["response"]
        == recall
    )
    shown = json.loads(c.fake.calls[-1].request.input_text)
    assert shown["fictional_episodes"][0]["text"] == EPISODE
    assert shown["memories"][0]["text"] == MEMORY
    assert (
        c.send(
            "continue",
            "Продолжим историю",
            [
                _generic_no_change(),
                _typed_no_change(),
                _result(dict(decision="no_change", handle=None, episode=None)),
                reply("Продолжим у маяка."),
            ],
        )["model_state"]
        == "completed"
    )
    assert len(c.fake.calls) == 18  # 3 + 4 + 4 + 3 + 4
    assert len(c.sdk.calls) == 1
    with c.provider._open() as db:
        assert db.execute(
            "SELECT count(*) FROM provider_usage_attempts"
        ).fetchone() == (19,)
    assert c.allowance.remaining() == 2
    before = len(c.fake.calls)
    c.restart()
    assert (
        c.send(
            "research", "Продолжи fictional story, затем изучи: " + QUESTIONS[0], []
        )["response"]
        == result["response"]
    )
    assert len(c.fake.calls) == before and len(c.sdk.calls) == 1
    with c.events._open() as db:
        research_source = db.execute(
            "SELECT event_id FROM event_envelopes WHERE source_message_id='research'"
        ).fetchone()[0]
        source = db.execute(
            "SELECT event_id FROM event_envelopes WHERE source_message_id='story'"
        ).fetchone()[0]
    status, _, body = c.browser.request(
        "POST",
        "/api/privacy/deletions",
        body=json.dumps(
            dict(operation_id="delete-story", source=source, confirm="delete")
        ).encode(),
        content_type="application/json",
        extra_environ={"HTTP_X_CSRF_TOKEN": c.browser.cookies["ar_csrf"]},
    )
    assert status == "200 OK", body
    assert not heads(c)
    with c.events._open() as db:
        assert db.execute(
            "SELECT count(*) FROM agent_reply_payloads WHERE operation_id=?",
            (c.app._conversations._operation_id(source),),
        ).fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM narrative_payloads").fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM agent_reply_payloads WHERE operation_id=?",
            (c.app._conversations._operation_id(research_source),),
        ).fetchone() == (0,)
    history = json.loads(c.browser.request("GET", "/api/history")[2])
    assert result["response"] not in str(history)
    assert len(c.fake.calls) == before and len(c.sdk.calls) == 1


@pytest.mark.parametrize(
    "cut",
    [
        "after_research_intent",
        "intent_before_discard",
        "after_research_started",
        "after_research_ipc",
        "after_reply_commit",
    ],
)
def test_interrupted_research_never_commits_projected_episode_or_repeats_calls(
    combined, cut, monkeypatch
):
    c = combined

    def interrupt(point):
        if point == cut:
            raise RuntimeError("synthetic interruption")

    c.app._conversations._checkpoint = interrupt
    if cut == "intent_before_discard":
        audit = c.app._conversations._narrative_audit
        finish = audit.finish

        def lose_discard(operation, status):
            if status == "rejected":
                raise RuntimeError("synthetic interruption")
            return finish(operation, status)

        monkeypatch.setattr(audit, "finish", lose_discard)
    c.send(
        "interrupted",
        "Расскажи вымышленную историю и изучи океан",
        research_turn(),
        assessed_raw(),
        interrupted=True,
    )
    assert not heads(c)
    c.restart()
    result = c.send("interrupted", "Расскажи вымышленную историю и изучи океан", [])
    assert result["model_state"] == (
        "completed"
        if cut == "after_reply_commit"
        else "research_pending"
        if cut in {"after_research_intent", "intent_before_discard"}
        else "research_uncertain"
    )
    assert not heads(c)
    assert len(c.fake.calls) == 4
    assert len(c.sdk.calls) == (
        1 if cut in {"after_research_ipc", "after_reply_commit"} else 0
    )
    assert c.allowance.remaining() == 16
    with sqlite3.connect(c.allowance.path) as db:
        consumed = [row[0] for row in db.execute("SELECT operation_id FROM consumed")]
    assert len(consumed) == 4
    assert not any(value.startswith("reply_hold:") for value in consumed)


@pytest.mark.parametrize("leak", [EPISODE, UNACKNOWLEDGED, MEMORY])
def test_research_output_cannot_echo_fiction_or_derived_memory(combined, leak):
    c = combined
    assert (
        c.send(
            "story",
            "Расскажи вымышленную историю",
            [_generic_no_change(), _typed_no_change(), episode(), reply()],
        )["model_state"]
        == "completed"
    )
    raw = assessed_raw()
    output = raw["output"][1]["content"][0]
    draft = json.loads(output["text"])
    draft["uncertainty"] += " " + leak
    output["text"] = json.dumps(draft, ensure_ascii=False)
    # A derived value can differ entirely from its source wording.
    if leak == MEMORY:
        assert (
            c.send(
                "fact",
                "Сохрани название проекта",
                [
                    _result(
                        dict(
                            decision="new_memory",
                            handles=[],
                            kind="candidate_useful_memory",
                            statement=MEMORY,
                            confidence=0.8,
                        )
                    ),
                    _typed_no_change(),
                    reply(),
                ],
            )["model_state"]
            == "completed"
        )
    result = c.send(
        "research", "Расскажи вымышленную историю и изучи океан", research_turn(), raw
    )
    assert result["model_state"] == "research_rejected"
    assert leak not in result["response"]
    assert [item.text for item in heads(c)] == [EPISODE]


def test_combined_profile_does_not_authorize_another_participant_and_stop_survives_restart(
    combined,
):
    c = combined
    result = c.send(
        "foreign",
        "Расскажи вымышленную историю и изучи океан",
        research_turn(),
        target=c.other,
    )
    assert result["model_state"] == "research_denied"
    assert not c.sdk.calls
    c.stop_path.chmod(0o600)
    c.stop_path.write_text(
        json.dumps(dict(version="private-pilot-emergency-stop-v1", stop=True))
    )
    c.stop_path.chmod(0o400)
    c.restart()
    assert (
        c.send("stopped", "Расскажи вымышленную историю", [])["model_state"]
        == "stopped"
    )
    assert len(c.fake.calls) == 4 and not c.sdk.calls


@pytest.mark.parametrize("choice", ["ocean", QUESTIONS[0] + " private detail"])
def test_research_without_story_cue_keeps_catalog_boundary(combined, choice):
    c = combined
    result = c.send(
        "research",
        "Изучи: " + QUESTIONS[0] + " CANARY-LOCAL-REQUEST",
        [
            _generic_no_change(),
            _typed_no_change(),
            _result(dict(decision="research", brief=choice)),
        ],
        assessed_raw(),
    )
    assert result["model_state"] == (
        "completed" if choice == "ocean" else "research_denied"
    )
    assert len(c.sdk.calls) == (1 if choice == "ocean" else 0)
    assert not heads(c)
    assert len(c.fake.calls) == 3 and c.allowance.remaining() == 17


@pytest.mark.parametrize("cut", ["narrative_receipt", "after_model"])
def test_interruption_before_research_keeps_consumed_or_held_reply_slot(combined, cut):
    c = combined

    def interrupt(point):
        if point == "after_model" and cut == "after_model":
            raise RuntimeError("synthetic interruption")
        if (
            point == "after_extraction_receipt"
            and cut == "narrative_receipt"
            and len(c.fake.calls) == 3
        ):
            raise RuntimeError("synthetic interruption")

    c.app._conversations._checkpoint = interrupt
    c.send(
        "held",
        "Расскажи вымышленную историю и изучи океан",
        research_turn(),
        interrupted=True,
    )
    c.restart()
    result = c.send("held", "Расскажи вымышленную историю и изучи океан", [])
    assert result["model_state"] == (
        "unavailable" if cut == "narrative_receipt" else "uncertain"
    )
    assert len(c.fake.calls) == (3 if cut == "narrative_receipt" else 4)
    assert c.allowance.remaining() == 16  # uncertain work is never refunded
    with sqlite3.connect(c.allowance.path) as db:
        values = [row[0] for row in db.execute("SELECT operation_id FROM consumed")]
    assert sum(value.startswith("reply_hold:") for value in values) == (
        cut == "narrative_receipt"
    )
    assert not c.sdk.calls and not heads(c)


def test_lost_research_response_does_not_repeat_paid_work(combined, monkeypatch):
    c = combined
    execute = PublicResearchClient.execute

    def lost(client, request):
        execute(client, request)
        raise OSError("synthetic lost response")

    monkeypatch.setattr(PublicResearchClient, "execute", lost)
    assert (
        c.send(
            "lost",
            "Расскажи вымышленную историю и изучи океан",
            research_turn(),
            assessed_raw(),
        )["model_state"]
        == "research_uncertain"
    )
    c.restart()
    assert (
        c.send("lost", "Расскажи вымышленную историю и изучи океан", [])["model_state"]
        == "research_uncertain"
    )
    assert len(c.sdk.calls) == 1 and len(c.fake.calls) == 4
    assert c.allowance.remaining() == 16 and not heads(c)
    with c.provider._open() as db:
        assert db.execute(
            "SELECT count(*) FROM provider_usage_attempts"
        ).fetchone() == (5,)


@pytest.mark.parametrize("control", ["permission", "stop"])
def test_controls_changed_during_research_withhold_combined_result(combined, control):
    c = combined
    create = c.sdk.create

    def revoked(**kwargs):
        raw = create(**kwargs)
        if control == "permission":
            c.narrative.chmod(0o600)
            c.narrative.write_text("{}")
            c.narrative.chmod(0o400)
        else:
            c.stop_path.chmod(0o600)
            c.stop_path.write_text(
                json.dumps(dict(version="private-pilot-emergency-stop-v1", stop=True))
            )
            c.stop_path.chmod(0o400)
        return raw

    c.sdk.create = revoked
    result = c.send(
        "controlled",
        "Расскажи вымышленную историю и изучи океан",
        research_turn(),
        assessed_raw(),
    )
    assert result["model_state"] == (
        "unavailable" if control == "permission" else "research_stopped"
    )
    assert not heads(c)
    c.restart()
    assert (
        c.send("controlled", "Расскажи вымышленную историю и изучи океан", [])[
            "model_state"
        ]
        == result["model_state"]
    )
    assert len(c.sdk.calls) == 1 and len(c.fake.calls) == 4


def test_ordinary_cli_accepts_existing_combined_flags_with_catalog(
    combined, tmp_path, monkeypatch
):
    from app.owner_control.store import OwnerStore
    from app.private_web import cli

    c = combined
    OwnerStore(tmp_path / "owner")  # explicitly initialized synthetic fixture
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    captured = []

    def server(_host, _port, app, **_options):
        captured.append(app)
        return SimpleNamespace(serve_forever=lambda: None)

    assert (
        cli.main(
            [
                "serve",
                "--auth-db",
                str(tmp_path / "auth.db"),
                "--event-db",
                str(c.events.database_path),
                "--ipc-state-db",
                str(tmp_path / "ipc-state.db"),
                "--privacy-ledger",
                str(tmp_path / "ledger.db"),
                "--owner-directory",
                str(tmp_path / "owner"),
                "--worker-socket",
                str(c.directory / "infer.sock"),
                "--worker-uid",
                str(os.geteuid()),
                "--enable-memory-evolution",
                "--enable-typed-memory",
                "--enable-narrative",
                "--narrative-permission",
                str(c.narrative),
                "--narrative-pilot-id",
                c.activation.pilot_id,
                "--enable-public-research",
                "--public-research-socket",
                str(c.directory / "research.sock"),
                "--public-research-worker-uid",
                str(os.geteuid()),
                "--public-research-profile",
                str(c.app_path),
                "--public-research-scope",
                "test-private-chat",
                "--allowed-host",
                "chat.test",
                "--allowed-origin",
                "https://chat.test",
            ],
            server_factory=server,
        )
        == 0
    )
    runtime = captured[0]._conversations
    assert runtime._narrative_permission() and runtime._research.enabled
    assert runtime._evolution_audit is not None and runtime._typed_audit is not None
    assert runtime._shared is None
    assert not c.fake.calls and not c.sdk.calls
