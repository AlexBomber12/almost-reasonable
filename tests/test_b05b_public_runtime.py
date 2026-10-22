"""Connected public deliberation and bounded fake catalog research."""

from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import json
import os
from threading import Thread

import pytest

from app.behavior.scheduler import WakeupEvent
from app.behavior.worker_client import PublicBehaviorClient
from app.behavior.deliberation import DeliberationOutcome, DeliberationResult
from app.behavior.mission import MissionPurpose, _MISSION_PURPOSE_TEXT
from app.core.agent_proposals import ResearchProposal
from app.core.public_research_client import PublicResearchClient
from app.core.public_behavior_mission import PublicMissionContext
from app.core.public_research_profile import (
    PUBLIC_PURPOSE,
    load_research_profile,
    provider_configuration,
)
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.integration.threads_runtime import ThreadsRuntime
from app.trusted.openai_worker_server import WorkerServerConfig
from app.trusted.public_research import PublicResearchBoundary, PublicResearchServer
from app.trusted.public_research_transport import PublicResearchTransport
from app.trusted.research_authority_journal import AuthorityJournal
from app.threads.domain import InboundKind
from app.threads.service import PolicyDeniedError
from test_int20_public_research import SDK, raw_response
from test_int24_runtime import NOW, run_once, setup


def _setup(tmp_path, proposal, *, purpose=MissionPurpose.GENERAL_INQUIRY):
    mission_path = tmp_path / "mission.json"
    mission_path.write_text(
        json.dumps(
            {
                "version": "mission-v1",
                "purpose": purpose.value,
                "text": _MISSION_PURPOSE_TEXT[purpose],
                "activated_at": (NOW - timedelta(hours=1)).isoformat(),
                "parent_version": None,
            }
        )
    )
    mission_path.chmod(0o600)
    mission = PublicMissionContext(mission_path, clock=lambda: NOW)
    return setup(tmp_path, proposal, mission=mission)


def _profile(tmp_path):
    common = {
        "version": "public-research-profile-v1",
        "purpose": PUBLIC_PURPOSE,
        "configuration": "public-research-v1",
        "provider_configuration": provider_configuration(),
        "profile_id": "a" * 64,
        "authorization": "b" * 64,
        "application_scope": "public-test",
        "account_ref": "account-1",
        "peer_uid": os.geteuid(),
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(days=1)).isoformat(),
        "reviewer": "test-operator",
        "reviewed_at": NOW.isoformat(),
        "aggregate_limit": 1,
        "topics": [
            {"id": "ocean", "question": "How do deep ocean currents form?", "limit": 1}
        ],
    }
    paths = []
    for role in ("application", "worker"):
        path = tmp_path / f"{role}.json"
        path.write_text(json.dumps(common | {"role": role}))
        path.chmod(0o600)
        paths.append(path)
    return paths


def _attach_fake_research(app, tmp_path, monkeypatch):
    application_path, _ = _profile(tmp_path)
    app.research_enabled = True
    app.research_scope = "public-test"
    app.research_profile_path = application_path
    app.research_client = PublicResearchClient(
        tmp_path / "unused-research.sock", expected_worker_uid=os.geteuid()
    )
    calls = []

    def execute(request):
        calls.append(request)
        return {
            "outcome": "completed",
            "attempts": 1,
            "report": {
                "main_finding": "Public source says currents move.",
                "sources": [
                    {
                        "reference": "source-1",
                        "url": "https://example.org/ocean",
                        "status": "recorded",
                        "metadata_validated": True,
                    }
                ],
            },
        }

    monkeypatch.setattr(app.research_client, "execute", execute)
    return calls


def test_pre_upgrade_completed_research_remains_public_context(tmp_path, monkeypatch):
    app, worker, _, _, _ = _setup(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    try:
        _attach_fake_research(app, tmp_path, monkeypatch)
        run_once(app, worker)
        with app.journal.transaction() as db:
            db.execute("DELETE FROM public_research_completions")
        _, evidence = app.public_context("later-decision")
        assert len(evidence) == 1
    finally:
        worker.close()


def test_wakeup_research_is_one_search_and_later_unverified_evidence(tmp_path):
    app, infer_worker, infer_sdk, http, clock = _setup(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    application_path, worker_path = _profile(tmp_path)
    ipc = tmp_path / "ipc"
    ipc.mkdir(mode=0o700)
    journal_path = ipc / "authority.sqlite"
    journal_path.touch(mode=0o600)
    sdk = SDK([raw_response()])
    boundary = PublicResearchBoundary(
        store=ProviderStateStore(EventStore(tmp_path / "search-events.sqlite")),
        transport=PublicResearchTransport(sdk),
        enabled=True,
        utcnow=lambda: NOW,
    )
    boundary.install_profile(
        load_research_profile(worker_path, role="worker"),
        profile_path=worker_path,
        journal=AuthorityJournal(journal_path),
    )
    research_worker = PublicResearchServer(
        WorkerServerConfig(ipc / "research.sock", frozenset({os.geteuid()})), boundary
    )
    app.research_enabled = True
    app.research_scope = "public-test"
    app.research_profile_path = application_path
    app.research_client = PublicResearchClient(
        ipc / "research.sock", expected_worker_uid=os.geteuid()
    )
    research_worker.start()
    thread = Thread(target=lambda: research_worker.serve(max_connections=1))
    thread.start()
    try:
        run_once(app, infer_worker)
        thread.join(timeout=5)
        assert not thread.is_alive()
        operation = app.wakeup_operation("wake-1")
        assert app.journal.read(operation)["phase"] == "linked"
        with app.journal.transaction() as db:
            research_id = db.execute(
                "SELECT research_id FROM public_research_ops WHERE operation=?",
                (operation,),
            ).fetchone()[0]
        assert app.provider.attempt_exists(operation)
        assert ProviderStateStore(
            EventStore(tmp_path / "search-events.sqlite")
        ).attempt_exists(research_id)
        assert len(sdk.calls) == 1
        assert (
            json.loads(sdk.calls[0]["input"])["question"]
            == "How do deep ocean currents form?"
        )
        assert (
            "How do deep ocean currents form?"
            in infer_sdk.calls[0]["input"][1]["content"]
        )
        assert "authorization" not in infer_sdk.calls[0]["input"][1]["content"]
        assert http.calls == []
        assert app.public_context("next")[1]
        evidence = json.loads(app.public_context("next")[1][0])
        assert evidence["claim_status"] == "unverified"
        assert evidence["semantic_support"] == "not_evaluated"
        restarted = ThreadsRuntime(
            tmp_path / "journal.sqlite",
            tmp_path / "scheduler.sqlite",
            tmp_path / "threads.sqlite",
            model=PublicBehaviorClient(
                tmp_path / "restarted-worker.sock", expected_uid=os.geteuid()
            ),
            transport=app.simulator,
            policy=app.policy,
            owner=app.owner,
            provider=app.provider,
            ledger=app.ledger,
            clock=app.clock,
            wakeup_policy=app.scheduler.policy,
            operator=app.operator,
            mission=app.mission,
            enabled=True,
            live_enabled=True,
            research_client=app.research_client,
            research_profile_path=application_path,
            research_scope="public-test",
            research_enabled=True,
        )
        assert restarted.public_context("later")[1] == app.public_context("later")[1]
        app.recover()
        assert len(sdk.calls) == 1
        infer_sdk.proposal = {"decision": "no_action", "reason_category": "low_value"}
        clock.advance(timedelta(seconds=2))
        next_inference = Thread(target=infer_worker.serve_once)
        next_inference.start()
        app.run((WakeupEvent("wake-2", clock.now(), "public initiative"),))
        next_inference.join(timeout=5)
        assert not next_inference.is_alive()
        assert "unverified" in infer_sdk.calls[1]["input"][1]["content"]
        assert len(sdk.calls) == 1
        generation = app.ledger.control_generation
        app.ledger.control_generation = lambda: generation() + 1
        assert app.public_context("later")[1] == ()
        app.ledger.control_generation = generation
        changed_mission = json.loads(app.mission.path.read_text())
        changed_mission["version"] = "mission-v2"
        changed_mission["parent_version"] = "mission-v1"
        app.mission.path.write_text(json.dumps(changed_mission))
        app.mission.path.chmod(0o600)
        assert app.public_context("later")[1] == ()
    finally:
        infer_worker.close()
        research_worker.close()


def test_public_message_research_when_publication_disabled(tmp_path):
    app, worker, sdk, http, _ = _setup(
        tmp_path,
        {"decision": "research", "topic_id": "ocean"},
        purpose=MissionPurpose.DISCONTINUE_OPTIONAL_CHANNEL,
    )
    application_path, worker_path = _profile(tmp_path)
    ipc = tmp_path / "ipc"
    ipc.mkdir(mode=0o700)
    journal_path = ipc / "authority.sqlite"
    journal_path.touch(mode=0o600)
    search_sdk = SDK([raw_response()])
    boundary = PublicResearchBoundary(
        store=ProviderStateStore(EventStore(tmp_path / "search-events.sqlite")),
        transport=PublicResearchTransport(search_sdk),
        enabled=True,
        utcnow=lambda: NOW,
    )
    boundary.install_profile(
        load_research_profile(worker_path, role="worker"),
        profile_path=worker_path,
        journal=AuthorityJournal(journal_path),
    )
    research_worker = PublicResearchServer(
        WorkerServerConfig(ipc / "research.sock", frozenset({os.geteuid()})), boundary
    )
    app.research_enabled = True
    app.research_scope = "public-test"
    app.research_profile_path = application_path
    app.research_client = PublicResearchClient(
        ipc / "research.sock", expected_worker_uid=os.geteuid()
    )
    app.policy.publication_capability_enabled = False
    research_worker.start()
    search_thread = Thread(target=lambda: research_worker.serve(max_connections=1))
    search_thread.start()
    try:
        app.poll(InboundKind.REPLY, correlation_id="poll-1")
        thread = Thread(target=worker.serve_once)
        thread.start()
        operations = app.process_inbound(InboundKind.REPLY)
        thread.join(timeout=5)
        search_thread.join(timeout=5)
        assert not thread.is_alive()
        assert not search_thread.is_alive()
        assert len(operations) == len(search_sdk.calls) == 1
        provider_input = json.loads(search_sdk.calls[0]["input"])
        assert provider_input["question"] == "How do deep ocean currents form?"
        assert "Опубликуй мои слова" not in str(search_sdk.calls)
        assert app.journal.read(operations[0])["phase"] == "linked"
        assert len(http.calls) == 1  # Read-only poll; no publication request.
        assert "ocean" in sdk.calls[0]["input"][1]["content"]
        assert app.public_context("later")[1]
        app.source_valid = lambda _payload: False
        assert app.public_context("later")[1] == ()
    finally:
        worker.close()
        research_worker.close()


def test_no_action_remains_available_when_publication_disabled(tmp_path):
    app, worker, _, http, _ = _setup(
        tmp_path,
        {"decision": "no_action", "reason_category": "low_value"},
        purpose=MissionPurpose.DISCONTINUE_OPTIONAL_CHANNEL,
    )
    app.policy.publication_capability_enabled = False
    try:
        run_once(app, worker)
        assert app.journal.read(app.wakeup_operation("wake-1"))["phase"] == "linked"
        assert http.calls == []
        with pytest.raises(PolicyDeniedError, match="publication capability"):
            app.publication_guard()
    finally:
        worker.close()


def test_unknown_topic_never_contacts_research(tmp_path, monkeypatch):
    app, worker, _, _, _ = _setup(
        tmp_path, {"decision": "research", "topic_id": "ocean-extra"}
    )
    calls = _attach_fake_research(app, tmp_path, monkeypatch)
    try:
        run_once(app, worker)
        assert calls == []
        assert app.journal.read(app.wakeup_operation("wake-1"))["phase"] == "started"
    finally:
        worker.close()


def test_application_rejects_unreviewed_choice_even_from_fake_worker(
    tmp_path, monkeypatch
):
    app, worker, _, _, _ = _setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    _attach_fake_research(app, tmp_path, monkeypatch)
    monkeypatch.setattr(
        app.engine,
        "deliberate",
        lambda _input: DeliberationResult(
            DeliberationOutcome.PROPOSED, ResearchProposal("unreviewed"), None, 1, 1
        ),
    )
    operation = app.wakeup_operation("wake-1")
    try:
        app._decide(
            operation,
            {"kind": "wake", "event": "wake-1", "correlation": operation},
            (),
        )
        assert app.journal.read(operation)["phase"] == "rejected"
    finally:
        worker.close()


def test_research_requires_explicit_client_profile_scope_and_mission(tmp_path):
    app, worker, _, _, _ = setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    try:
        with pytest.raises(ValueError, match="research configuration"):
            ThreadsRuntime(
                tmp_path / "other-journal",
                tmp_path / "other-scheduler",
                tmp_path / "other-threads",
                model=app.model,
                transport=app.simulator,
                policy=app.policy,
                owner=app.owner,
                provider=app.provider,
                ledger=app.ledger,
                clock=app.clock,
                wakeup_policy=app.scheduler.policy,
                operator=app.operator,
                research_enabled=True,
            )
    finally:
        worker.close()


@pytest.mark.parametrize(
    "change",
    [
        {"purpose": "private-conversation-public-research-v1"},
        {"account_ref": "another-account"},
        {"application_scope": "another-scope"},
        {"expires_at": NOW.isoformat()},
    ],
)
def test_wrong_public_authority_cannot_offer_research(tmp_path, monkeypatch, change):
    app, worker, _, _, _ = _setup(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    calls = _attach_fake_research(app, tmp_path, monkeypatch)
    path = app.research_profile_path
    value = json.loads(path.read_text()) | change
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    try:
        assert app.public_context("next")[0] == ()
        run_once(app, worker)
        assert calls == []
    finally:
        worker.close()


@pytest.mark.parametrize("malformed", ["invalid_utf8", "surrogate"])
def test_malformed_profile_encoding_fails_closed(tmp_path, monkeypatch, malformed):
    app, worker, _, _, _ = _setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    _attach_fake_research(app, tmp_path, monkeypatch)
    if malformed == "invalid_utf8":
        app.research_profile_path.write_bytes(b"\xff")
    else:
        value = json.loads(app.research_profile_path.read_text())
        value["topics"][0]["question"] = "\ud800"
        app.research_profile_path.write_text(json.dumps(value))
    app.research_profile_path.chmod(0o600)
    try:
        assert app.current_research_profile() is None
        assert app.public_context("next") == ((), ())
    finally:
        worker.close()


def test_lost_result_is_uncertain_and_never_retransmitted(tmp_path, monkeypatch):
    app, worker, _, _, _ = _setup(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    calls = _attach_fake_research(app, tmp_path, monkeypatch)

    def lost(request):
        calls.append(request)
        raise TimeoutError("lost result")

    monkeypatch.setattr(app.research_client, "execute", lost)
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        with app.journal.transaction() as db:
            assert (
                db.execute(
                    "SELECT phase FROM public_research_ops WHERE operation=?",
                    (operation,),
                ).fetchone()[0]
                == "uncertain"
            )
        assert app.public_context("next")[0] == ()
        app.recover()
        app._research(operation, app.journal.read(operation))
        with app.journal.transaction() as db:
            db.execute(
                "UPDATE public_research_ops SET phase='started' WHERE operation=?",
                (operation,),
            )
        app._research(operation, app.journal.read(operation))
        with app.journal.transaction() as db:
            assert (
                db.execute(
                    "SELECT phase FROM public_research_ops WHERE operation=?",
                    (operation,),
                ).fetchone()[0]
                == "uncertain"
            )
        assert len(calls) == 1
    finally:
        worker.close()


@pytest.mark.parametrize("control", ["stop", "spend"])
def test_current_controls_prevent_research_contact(tmp_path, monkeypatch, control):
    app, worker, _, _, _ = _setup(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    calls = _attach_fake_research(app, tmp_path, monkeypatch)

    def checkpoint(point):
        if point == "after_research_intent":
            if control == "stop":
                with app.owner._connection() as db:
                    db.execute("UPDATE owner_stop SET active=1")
            else:
                monkeypatch.setattr(app.provider, "is_spend_suspended", lambda: True)

    app.checkpoint = checkpoint
    try:
        run_once(app, worker)
        assert calls == []
        with app.journal.transaction() as db:
            assert (
                db.execute("SELECT phase FROM public_research_ops").fetchone()[0]
                == "held"
            )
    finally:
        worker.close()


def test_profile_replacement_after_intent_prevents_contact(tmp_path, monkeypatch):
    app, worker, _, _, _ = _setup(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    calls = _attach_fake_research(app, tmp_path, monkeypatch)

    def checkpoint(point):
        if point == "after_research_intent":
            value = json.loads(app.research_profile_path.read_text())
            value["profile_id"] = "c" * 64
            app.research_profile_path.write_text(json.dumps(value))
            app.research_profile_path.chmod(0o600)

    app.checkpoint = checkpoint
    try:
        run_once(app, worker)
        assert calls == []
        with app.journal.transaction() as db:
            assert (
                db.execute("SELECT phase FROM public_research_ops").fetchone()[0]
                == "held"
            )
    finally:
        worker.close()


def test_profile_replacement_after_decision_blocks_handoff(tmp_path, monkeypatch):
    app, worker, _, _, _ = _setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    calls = _attach_fake_research(app, tmp_path, monkeypatch)
    monkeypatch.setattr(
        app.engine,
        "deliberate",
        lambda _input: DeliberationResult(
            DeliberationOutcome.PROPOSED, ResearchProposal("ocean"), None, 1, 1
        ),
    )
    operation = app.wakeup_operation("wake-1")
    try:
        app._decide(
            operation, {"kind": "wake", "event": "wake-1", "correlation": operation}, ()
        )
        payload = app.journal.read(operation)
        assert payload["phase"] == "intent"
        value = json.loads(app.research_profile_path.read_text())
        value["profile_id"] = "c" * 64
        app.research_profile_path.write_text(json.dumps(value))
        app.research_profile_path.chmod(0o600)
        with pytest.raises(PolicyDeniedError, match="authority changed"):
            app._handoff(operation, payload)
        assert calls == []
    finally:
        worker.close()


def test_profile_change_during_inference_cannot_rebind_topic(tmp_path, monkeypatch):
    app, worker, sdk, _, _ = _setup(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    calls = _attach_fake_research(app, tmp_path, monkeypatch)
    original_create = sdk.create

    def changed_profile(**kwargs):
        value = json.loads(app.research_profile_path.read_text())
        value["profile_id"] = "c" * 64
        value["authorization"] = "d" * 64
        value["topics"][0]["question"] = "What causes auroras?"
        app.research_profile_path.write_text(json.dumps(value))
        app.research_profile_path.chmod(0o600)
        return original_create(**kwargs)

    sdk.create = changed_profile
    try:
        run_once(app, worker)
        assert calls == []
        assert app.journal.read(app.wakeup_operation("wake-1"))["phase"] == "rejected"
    finally:
        worker.close()


def test_profile_change_before_inference_hides_old_catalog(tmp_path, monkeypatch):
    app, worker, _, _, _ = _setup(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    calls = _attach_fake_research(app, tmp_path, monkeypatch)

    def checkpoint(point):
        if point == "after_started":
            value = json.loads(app.research_profile_path.read_text())
            value["profile_id"] = "c" * 64
            value["topics"][0]["question"] = "What causes auroras?"
            app.research_profile_path.write_text(json.dumps(value))
            app.research_profile_path.chmod(0o600)

    app.checkpoint = checkpoint
    try:
        run_once(app, worker)
        assert calls == []
        assert app.journal.read(app.wakeup_operation("wake-1"))["phase"] == "started"
    finally:
        worker.close()


@pytest.mark.parametrize("change", ["source", "race", "last_slot"])
def test_handoff_rechecks_source_and_atomic_last_slot(tmp_path, monkeypatch, change):
    app, worker, _, _, _ = _setup(
        tmp_path, {"decision": "no_action", "reason_category": "other"}
    )
    calls = _attach_fake_research(app, tmp_path, monkeypatch)
    monkeypatch.setattr(
        app.engine,
        "deliberate",
        lambda _input: DeliberationResult(
            DeliberationOutcome.PROPOSED, ResearchProposal("ocean"), None, 1, 1
        ),
    )
    operation = app.wakeup_operation("wake-1")
    try:
        app._decide(
            operation, {"kind": "wake", "event": "wake-1", "correlation": operation}, ()
        )
        payload = app.journal.read(operation)
        if change == "source":
            checks = iter((True, False))
            app.source_valid = lambda _payload: next(checks)
            with pytest.raises(PolicyDeniedError, match="source changed"):
                app._handoff(operation, payload)
        elif change == "race":
            current = app.current_research_profile

            def raced():
                profile = current()
                with app.journal.transaction() as db:
                    db.execute(
                        "INSERT INTO public_research_ops VALUES(?,?,?,?,?,NULL)",
                        (operation, "raced-search", "ocean", profile.digest, "started"),
                    )
                return profile

            app.current_research_profile = raced
            app._handoff(operation, payload)
            assert app.journal.read(operation)["phase"] == "linked"
        else:
            with app.journal.transaction() as db:
                db.execute(
                    "INSERT INTO public_research_ops VALUES(?,?,?,?,?,NULL)",
                    (
                        "other-decision",
                        "other-search",
                        "ocean",
                        payload["profile_digest"],
                        "started",
                    ),
                )
            with pytest.raises(PolicyDeniedError, match="capacity exhausted"):
                app._handoff(operation, payload)
        assert calls == []
    finally:
        worker.close()


def test_authority_change_during_search_discards_result(tmp_path, monkeypatch):
    app, worker, _, _, _ = _setup(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    calls = _attach_fake_research(app, tmp_path, monkeypatch)
    original = app.research_client.execute

    def changed(request):
        result = original(request)
        value = json.loads(app.research_profile_path.read_text())
        value["account_ref"] = "other-account"
        app.research_profile_path.write_text(json.dumps(value))
        app.research_profile_path.chmod(0o600)
        return result

    monkeypatch.setattr(app.research_client, "execute", changed)
    try:
        run_once(app, worker)
        assert len(calls) == 1
        assert app.public_context("later")[1] == ()
        with app.journal.transaction() as db:
            assert (
                db.execute("SELECT phase FROM public_research_ops").fetchone()[0]
                == "held"
            )
    finally:
        worker.close()


@pytest.mark.parametrize(
    ("result", "phase"),
    [
        ({"outcome": "authorization_denied", "attempts": 0}, "failed"),
        ({"outcome": "timeout", "attempts": 1}, "uncertain"),
        (
            {
                "outcome": "completed",
                "attempts": 1,
                "report": {"main_finding": "x" * 3000, "sources": []},
            },
            "failed",
        ),
        (
            {
                "outcome": "completed",
                "attempts": 1,
                "report": {"main_finding": "Ж" * 1100, "sources": []},
            },
            "failed",
        ),
    ],
)
def test_failed_or_oversized_research_does_not_expose_evidence(
    tmp_path, monkeypatch, result, phase
):
    app, worker, _, _, _ = _setup(
        tmp_path, {"decision": "research", "topic_id": "ocean"}
    )
    _attach_fake_research(app, tmp_path, monkeypatch)
    monkeypatch.setattr(app.research_client, "execute", lambda _request: result)
    try:
        run_once(app, worker)
        with app.journal.transaction() as db:
            assert (
                db.execute("SELECT phase FROM public_research_ops").fetchone()[0]
                == phase
            )
        assert app.public_context("later")[1] == ()
    finally:
        worker.close()


def test_public_worker_quota_survives_profile_replacement_and_concurrency(tmp_path):
    _, worker_path = _profile(tmp_path)
    journal_dir = tmp_path / "worker-journal"
    journal_dir.mkdir(mode=0o700)
    journal_path = journal_dir / "authority.sqlite"
    journal_path.touch(mode=0o600)
    journal = AuthorityJournal(journal_path)
    first = load_research_profile(worker_path, role="worker")
    journal.activate(first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        admitted = list(
            pool.map(
                lambda operation: journal.reserve(first, operation, "ocean"),
                ("search-1", "search-2"),
            )
        )
    assert sorted(admitted) == [False, True]
    journal = AuthorityJournal(journal_path)
    assert journal.reserve(first, "search-after-restart", "ocean") is False
    changed_account = json.loads(worker_path.read_text())
    changed_account["account_ref"] = "other-account"
    worker_path.write_text(json.dumps(changed_account))
    worker_path.chmod(0o600)
    changed = load_research_profile(worker_path, role="worker")
    assert changed.digest != first.digest
    with pytest.raises(ValueError, match="replayed or changed"):
        journal.activate(changed)
    replacement = json.loads(worker_path.read_text())
    replacement["account_ref"] = "account-1"
    replacement["profile_id"] = "c" * 64
    worker_path.write_text(json.dumps(replacement))
    worker_path.chmod(0o600)
    current = load_research_profile(worker_path, role="worker")
    journal.activate(current)
    assert journal.reserve(current, "search-3", "ocean") is False


def test_revoked_public_worker_profile_prevents_provider_contact(tmp_path):
    _, worker_path = _profile(tmp_path)
    journal_dir = tmp_path / "worker-journal"
    journal_dir.mkdir(mode=0o700)
    journal_path = journal_dir / "authority.sqlite"
    journal_path.touch(mode=0o600)
    journal = AuthorityJournal(journal_path)
    profile = load_research_profile(worker_path, role="worker")
    sdk = SDK([raw_response()])
    boundary = PublicResearchBoundary(
        store=ProviderStateStore(EventStore(tmp_path / "search-events.sqlite")),
        transport=PublicResearchTransport(sdk),
        enabled=True,
        utcnow=lambda: NOW,
    )
    boundary.install_profile(profile, profile_path=worker_path, journal=journal)
    journal.revoke(profile)
    result = boundary.execute(
        profile.request("search-revoked", "ocean"), peer_uid=os.geteuid()
    )
    assert result["outcome"] == "authorization_denied"
    assert sdk.calls == []
