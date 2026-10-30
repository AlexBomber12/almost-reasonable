"""Read-only public influence in two isolated real private-worker conversations."""

import json
import sqlite3

import pytest

from app.integration.public_intellectual_source import PublicIntellectualSource
from app.private_web.runtime import WorkerConversationRuntime
from app.threads.domain import InboundKind
from test_agent_intellectual_state import private_setup, private_submit, proposal
from test_int24_runtime import setup as public_setup
from test_public_experience import wake


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


@pytest.fixture
def integrated(tmp_path):
    public_dir, private_dir = tmp_path / "public", tmp_path / "private"
    public_dir.mkdir(mode=0o700)
    private_dir.mkdir(mode=0o700)
    public, worker, sdk, http, clock = public_setup(
        public_dir, {"decision": "no_action", "reason_category": "other"}
    )
    public.poll(InboundKind.REPLY, correlation_id="public-source")
    sdk.proposal["intellectual"] = proposal()
    wake(public, worker, clock, "develop-interest")
    bridge = PublicIntellectualSource(
        account=public.account,
        journal_path=public.journal.path,
        threads_path=public.service.store.database,
        ledger_path=public.ledger.path,
        clock=clock.now,
    )
    app, a, b, events, ledger, gate, private_sdk, server = private_setup(private_dir)
    original = app._conversations
    app._conversations = WorkerConversationRuntime(
        events,
        gate,
        original._transport,
        provider_state=original._provider,
        clock=original._clock,
        public_intellectual_source=bridge,
    )
    try:
        yield (
            public,
            worker,
            sdk,
            http,
            clock,
            bridge,
            app,
            a,
            b,
            events,
            ledger,
            gate,
            private_sdk,
            server,
        )
    finally:
        server.close()
        worker.close()


def test_same_public_interest_reaches_both_private_sdk_inputs_without_history_leak(
    integrated,
):
    public, _, _, _, _, bridge, _, a, b, _, _, _, sdk, server = integrated
    assert bridge.sources()
    private_submit(server, a, "a-only", "Обсудим следующий шаг PRIVATE_A_CANARY.")
    first = json.loads(sdk.calls[-1]["input"])
    public_sources = [
        s
        for s in first["intellectual_state"]["sources"]
        if s["kind"] == "public_agent_state"
    ]
    assert (
        len(public_sources) == 1 and "Русская архитектура" in public_sources[0]["text"]
    )
    private_submit(server, b, "b-only", "Как изменились твои интересы?")
    second = sdk.calls[-1]["input"]
    assert "Русская архитектура" in second
    assert "PRIVATE_A_CANARY" not in second
    assert str(public.journal.path) not in second and public.account not in second
    assert "not independently verified" in public_sources[0]["text"]


def test_public_adapter_never_initializes_missing_databases(tmp_path):
    files = [tmp_path / name for name in ("journal.db", "threads.db", "ledger.db")]
    with pytest.raises((ValueError, OSError, sqlite3.Error)):
        PublicIntellectualSource(
            account="account-1",
            journal_path=files[0],
            threads_path=files[1],
            ledger_path=files[2],
        )
    assert not any(path.exists() for path in files)


def test_private_state_and_future_context_lose_withdrawn_public_ancestry(integrated):
    public, _, _, _, _, bridge, _, a, _, events, _, _, sdk, server = integrated

    def choose_interest():
        frame = json.loads(sdk.calls[-1]["input"])["intellectual_state"]
        source = next(s for s in frame["sources"] if s["kind"] == "public_agent_state")
        sdk.change = proposal(source["handle"])

    sdk.before_result = choose_interest
    private_submit(server, a, "learn", "Обсудим твои интересы.")
    with events._open() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM agent_intellectual_revisions").fetchone()[
                0
            ]
            == 1
        )
    sdk.before_result = lambda: None
    sdk.change = None
    with public.service.store._open() as db:
        db.execute(
            "DELETE FROM threads_inbound_events WHERE account_ref=?", (public.account,)
        )
    assert bridge.sources() == {}
    private_submit(server, a, "after-withdrawal", "Продолжим разговор.")
    assert "Русская архитектура" not in sdk.calls[-1]["input"]


def test_protected_credential_free_binding_config_and_read_only_connections(
    integrated, tmp_path
):
    public, _, _, _, clock, bridge, *_ = integrated
    path = tmp_path / "public-source.json"
    config = {
        "version": "public-intellectual-source-v1",
        "account": public.account,
        "journal_path": str(public.journal.path),
        "threads_path": str(public.service.store.database),
        "ledger_path": str(public.ledger.path),
        "mission_path": None,
    }
    path.write_text(json.dumps(config))
    path.chmod(0o400)
    restored = PublicIntellectualSource.from_config(path, clock=clock.now)
    assert restored.sources() == bridge.sources()
    with (
        restored.view.service.store._open() as db,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        db.execute("DELETE FROM threads_inbound_events")
    path.chmod(0o600)
    path.write_text(json.dumps(config | {"token": "not-a-real-token"}))
    path.chmod(0o400)
    with pytest.raises(ValueError, match="binding config"):
        PublicIntellectualSource.from_config(path)


@pytest.mark.parametrize(
    "case",
    ["account", "schema", "intellectual_schema", "threads_schema", "ledger", "missing"],
)
def test_public_reader_rejects_wrong_or_withdrawn_source_identity(integrated, case):
    public, _, _, _, clock, bridge, *_ = integrated
    if case == "account":
        with pytest.raises(ValueError, match="binding mismatch"):
            PublicIntellectualSource(
                account="other-account",
                journal_path=public.journal.path,
                threads_path=public.service.store.database,
                ledger_path=public.ledger.path,
                clock=clock.now,
            )
        return
    if case == "schema":
        with public.journal.transaction() as db:
            db.execute("UPDATE public_runtime_schema SET version='unsupported'")
    elif case == "intellectual_schema":
        with public.journal.transaction() as db:
            db.execute("UPDATE agent_intellectual_schema SET version='unsupported'")
    elif case == "threads_schema":
        with public.service.store._open() as db:
            db.execute("UPDATE threads_schema_metadata SET version=999")
    elif case == "ledger":
        with public.ledger._open() as db:
            db.execute("DELETE FROM privacy_control_generation")
    else:
        public.journal.path.rename(public.journal.path.with_suffix(".withdrawn"))
    assert bridge.sources() == {}


def test_private_cli_composes_readonly_identity_and_one_actual_owner_stop(
    integrated, tmp_path
):
    import os
    from pathlib import Path
    from types import SimpleNamespace
    from app.private_web.cli import main

    public, _, _, _, _, bridge, _, _, _, events, ledger, _, _, _ = integrated
    path = tmp_path / "binding.json"
    config = {
        "version": "public-intellectual-source-v1",
        "account": public.account,
        "journal_path": str(public.journal.path),
        "threads_path": str(public.service.store.database),
        "ledger_path": str(public.ledger.path),
        "mission_path": None,
        "owner_directory": str(public.owner.directory),
    }
    path.write_text(json.dumps(config))
    path.chmod(0o400)
    root = Path(events.database_path).parent
    captured = []

    def server(_host, _port, application, **_options):
        captured.append(application)
        return SimpleNamespace(serve_forever=lambda: None)

    assert (
        main(
            [
                "serve",
                "--auth-db",
                str(root / "auth.db"),
                "--event-db",
                str(events.database_path),
                "--ipc-state-db",
                str(root / "ipc-state.db"),
                "--privacy-ledger",
                str(ledger.path),
                "--owner-directory",
                str(public.owner.directory),
                "--worker-socket",
                str(root / "worker.sock"),
                "--worker-uid",
                str(os.geteuid()),
                "--allowed-host",
                "chat.test",
                "--allowed-origin",
                "https://chat.test",
                "--public-intellectual-config",
                str(path),
            ],
            server_factory=server,
        )
        == 0
    )
    assert (
        captured[0]._conversations._public_intellectual_source.sources()
        == bridge.sources()
    )
    restored = PublicIntellectualSource.from_config(
        path, expected_owner_directory=public.owner.directory
    )
    assert restored.sources() == bridge.sources()
    with pytest.raises(ValueError, match="share owner Stop"):
        PublicIntellectualSource.from_config(
            path, expected_owner_directory=tmp_path / "other-owner"
        )
    with pytest.raises(ValueError, match="owner Stop binding"):
        PublicIntellectualSource(
            account=public.account,
            journal_path=public.journal.path,
            threads_path=public.service.store.database,
            ledger_path=public.ledger.path,
            owner_directory=tmp_path / "other-owner",
        )


def test_public_projection_race_and_malformed_heads_fail_closed(
    integrated, monkeypatch
):
    public, _, _, _, _, bridge, *_ = integrated
    read = bridge.view.intellectual.sources

    def changed(**kwargs):
        result = read(**kwargs)
        with public.ledger._open() as db:
            public.ledger._increment_control_generation(db)
        return result

    monkeypatch.setattr(bridge.view.intellectual, "sources", changed)
    assert bridge.sources() == {}
    monkeypatch.setattr(bridge.view.intellectual, "sources", read)
    with public.journal.transaction() as db:
        db.execute("UPDATE agent_intellectual_revisions SET payload='null'")
    assert bridge.sources() == {}


def test_private_backup_cannot_revive_withdrawn_public_influence(integrated, tmp_path):
    from app.core.store import EventStore
    from app.privacy.application import PrivacyReadGate, EventStoreRestoreAdapter
    from app.privacy.recovery import create_sqlite_backup, QuarantinedRestore
    from test_int24_runtime import NOW

    public, _, _, _, _, bridge, app, a, _, events, ledger, _, sdk, server = integrated

    def learn():
        frame = json.loads(sdk.calls[-1]["input"])["intellectual_state"]
        source = next(
            item for item in frame["sources"] if item["kind"] == "public_agent_state"
        )
        sdk.change = proposal(source["handle"])

    sdk.before_result = learn
    private_submit(server, a, "before-backup", "Обсудим твои интересы.")
    with events._open() as db:
        creator = db.execute(
            "SELECT operation_id FROM agent_reply_operations LIMIT 1"
        ).fetchone()[0]
    assert app._conversations._intellectual.research_valid(
        a._principal
        if hasattr(a, "_principal")
        else __import__("test_event_store").principal("a"),
        creator,
    )
    backup, quarantine = tmp_path / "backup.db", tmp_path / "quarantine.db"
    create_sqlite_backup(events.database_path, backup, 14, NOW)
    with public.service.store._open() as db:
        db.execute(
            "DELETE FROM threads_inbound_events WHERE account_ref=?", (public.account,)
        )
    assert bridge.sources() == {}
    assert (
        QuarantinedRestore(ledger, 14, EventStoreRestoreAdapter)
        .restore(backup, quarantine, NOW)
        .activation_eligible
    )
    restored = EventStore(quarantine)
    gate = PrivacyReadGate(ledger, restored)
    assert gate.reconcile()
    previous = app._conversations
    app._conversations = WorkerConversationRuntime(
        restored,
        gate,
        previous._transport,
        provider_state=previous._provider,
        clock=previous._clock,
        public_intellectual_source=bridge,
    )
    sdk.before_result = lambda: None
    sdk.change = None
    private_submit(server, a, "after-backup", "Продолжим разговор.")
    assert "Русская архитектура" not in sdk.calls[-1]["input"]


def test_public_source_rejects_relative_and_forged_shared_paths(integrated):
    public, *_ = integrated
    args = dict(
        account=public.account,
        journal_path=public.journal.path,
        threads_path=public.service.store.database,
        ledger_path=public.ledger.path,
    )
    with pytest.raises(ValueError, match="absolute"):
        PublicIntellectualSource(**(args | {"journal_path": "relative.db"}))
    with pytest.raises(ValueError, match="shared binding"):
        PublicIntellectualSource(**args, shared_source=object())


def test_public_revision_during_private_provider_call_blocks_release(integrated):
    public, _, _, _, _, _, app, a, _, _, _, _, sdk, server = integrated
    before = len(sdk.calls)

    def withdraw():
        with public.service.store._open() as db:
            db.execute(
                "DELETE FROM threads_inbound_events WHERE account_ref=?",
                (public.account,),
            )

    sdk.before_result = withdraw
    result = private_submit(
        server,
        a,
        "race",
        "Продолжим разговор.",
    )
    assert result["model_state"] == "unavailable" and result["response"] is None
    assert len(sdk.calls) == before + 1
    with app._conversations._event_store._open() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM agent_intellectual_revisions").fetchone()[
                0
            ]
            == 0
        )


def test_public_source_selection_is_bounded_before_private_projection(integrated):
    from datetime import timedelta
    from app.core.public_intellectual_receipt import source_key

    public, worker, _, _, clock, bridge, _, _, _, _, _, _, _, _ = integrated
    for index in range(6):
        clock.advance(timedelta(days=1))
        wake(public, worker, clock, f"additional-interest-{index}")
    with public.journal.transaction() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM agent_intellectual_revisions").fetchone()[
                0
            ]
            == 7
        )
    assert len(bridge.sources()) == 6
    with public.journal.transaction() as db:
        heads = public.intellectual.state._heads(db, public.intellectual.sources())
    keys = frozenset(source_key(public.account, row[0]) for row in heads)
    assert len(bridge.sources(required_keys=keys)) == 7
    assert bridge.sources(required_keys=frozenset({"missing"})) == {}


@pytest.mark.parametrize(
    "required", [[], frozenset(), frozenset(str(i) for i in range(101))]
)
def test_dependency_revalidation_requests_remain_closed_and_bounded(
    integrated, required
):
    assert integrated[5].sources(required_keys=required) == {}


def test_selection_eviction_preserves_live_private_reply_and_intellectual_state(
    integrated,
):
    from datetime import timedelta
    from test_event_store import principal

    public, worker, _, _, clock, bridge, app, a, _, events, _, gate, sdk, server = (
        integrated
    )
    original = bridge.sources()

    def choose():
        frame = json.loads(sdk.calls[-1]["input"])["intellectual_state"]
        handle = next(
            row["handle"]
            for row in frame["sources"]
            if row["kind"] == "public_agent_state"
        )
        sdk.change = proposal(handle)

    sdk.before_result = choose
    assert (
        private_submit(server, a, "retain-old", "Обсудим этот устойчивый интерес.")[
            "model_state"
        ]
        == "completed"
    )
    with events._open() as db:
        operation = db.execute(
            "SELECT operation_id FROM agent_reply_operations"
        ).fetchone()[0]
    runtime = app._conversations
    assert gate.authorized(
        lambda: runtime._public_intellectual_dependencies_live(
            operation, principal("a")
        )
    )
    for index in range(6):
        clock.advance(timedelta(days=1))
        wake(public, worker, clock, f"newer-{index}")
    assert len(bridge.sources()) == 6 and not set(original) & set(bridge.sources())
    assert bridge.sources(required_keys=frozenset(original)) == original
    assert gate.authorized(
        lambda: runtime._public_intellectual_dependencies_live(
            operation, principal("a")
        )
    )
    sdk.before_result = lambda: None
    sdk.change = None
    assert (
        private_submit(server, a, "after-eviction", "Продолжим разговор.")[
            "model_state"
        ]
        == "completed"
    )
    frame = json.loads(sdk.calls[-1]["input"])["intellectual_state"]
    assert len(frame["states"]) == 1
    assert (
        len([row for row in frame["sources"] if row["kind"] == "public_agent_state"])
        <= 6
    )
    with public.service.store._open() as db:
        db.execute(
            "DELETE FROM threads_inbound_events WHERE account_ref=?", (public.account,)
        )
    assert not gate.authorized(
        lambda: runtime._public_intellectual_dependencies_live(
            operation, principal("a")
        )
    )


def test_oversized_converted_public_record_does_not_hide_other_live_sources(integrated):
    public, worker, sdk, _, clock, bridge, *_ = integrated
    original = bridge.sources()
    sdk.proposal["intellectual"] = proposal(
        fields={
            "topic": "x" * 1740,
            "strength": 60,
            "status": "active",
            "reason_for_interest": "Хочу разобраться.",
        }
    )
    wake(public, worker, clock, "large-valid-interest")
    with public.journal.transaction() as db:
        heads = public.intellectual.state._heads(db, public.intellectual.sources())
    assert len(heads) == 2
    assert set(original) <= set(bridge.sources())


def test_arbitrary_adapter_cannot_enter_the_private_runtime(integrated):
    _, _, _, _, _, _, app, _, _, events, _, gate, sdk, _ = integrated
    old, calls = app._conversations, len(sdk.calls)
    with pytest.raises(TypeError, match="exact public intellectual source"):
        WorkerConversationRuntime(
            events,
            gate,
            old._worker,
            provider_state=old._provider,
            clock=old._clock,
            public_intellectual_source=object(),
        )
    assert len(sdk.calls) == calls


@pytest.mark.parametrize("phase", [None, "before", "during", "evicted"])
def test_public_ancestry_protects_fiction_inference_and_later_context(
    integrated, phase
):
    from threading import Event, Thread
    from app.cognition.narrative_store import NarrativeStore
    from test_event_store import principal

    (
        public,
        public_worker,
        _,
        _,
        public_clock,
        bridge,
        app,
        a,
        _,
        events,
        _,
        gate,
        sdk,
        server,
    ) = integrated
    private_submit(server, a, "public-context", "Обсудим твой новый интерес.")
    sdk.change = None
    old, create = app._conversations, sdk.create
    server._narrative_enabled = True

    def withdraw():
        with public.service.store._open() as db:
            db.execute(
                "DELETE FROM threads_inbound_events WHERE account_ref=?",
                (public.account,),
            )

    def narrate(**kwargs):
        result = create(**kwargs)
        if json.loads(kwargs["input"])["task"] == "propose_private_narrative":
            result["output"][0]["content"][0]["text"] = json.dumps(
                {
                    "proposal": {
                        "decision": "create",
                        "handle": None,
                        "episode": "Я видел вымышленный маяк PUBLIC_FICTION_CANARY.",
                    }
                }
            )
            if phase == "during":
                withdraw()
        return result

    def checkpoint(point):
        if phase == "before" and point == "after_reservation":
            withdraw()

    sdk.create = narrate
    app._conversations = WorkerConversationRuntime(
        events,
        gate,
        old._worker,
        provider_state=old._provider,
        clock=old._clock,
        public_intellectual_source=bridge,
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
        if phase in {None, "evicted"}:
            assert result["model_state"] == "completed" and len(sdk.calls) == before + 2
            assert NarrativeStore(events).current(principal("a"))
            if phase == "evicted":
                from datetime import timedelta

                for index in range(6):
                    public_clock.advance(timedelta(days=1))
                    wake(public, public_worker, public_clock, f"after-fiction-{index}")
                sdk.create = create
                result = json.loads(a.submit("still-live", "Продолжим разговор.")[2])
                assert result["model_state"] == "completed"
                assert "PUBLIC_FICTION_CANARY" in sdk.calls[-1]["input"]
            withdraw()
            sdk.create = create
            result = json.loads(a.submit("later", "Продолжим разговор.")[2])
            assert result["model_state"] == "completed"
            assert "PUBLIC_FICTION_CANARY" not in sdk.calls[-1]["input"]
        else:
            assert result["model_state"] == "unavailable"
            assert len(sdk.calls) == before + (phase == "during")
            assert NarrativeStore(events).current(principal("a")) == ()
    finally:
        stop.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
