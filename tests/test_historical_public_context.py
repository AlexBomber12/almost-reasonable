"""Epistemic public changes preserve history; source withdrawal still invalidates it."""

import json

import pytest

import test_public_private_intellectual as public_cases
from test_agent_intellectual_state import private_submit, proposal
from test_cognition_pipeline import principal
from test_public_experience import wake

integrated = public_cases.integrated


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


def public_revision(public, worker, sdk, clock, identity, strength):
    change = proposal(target="i0")
    change["fields"]["strength"] = strength
    change["reason"] = "Обсуждение уточнило мою позицию."
    sdk.proposal = {
        "decision": "no_action",
        "reason_category": "other",
        "intellectual": change,
    }
    wake(public, worker, clock, identity)
    with public.journal.transaction() as db:
        revision = db.execute(
            "SELECT MAX(revision) FROM agent_intellectual_revisions"
        ).fetchone()[0]
    assert revision == strength - 59


def agent_ids(app, participant):
    return {
        row.event_id
        for row in app._conversations.history(principal(participant))
        if row.role == "agent"
    }


def test_public_revision_preserves_both_private_histories_until_source_withdrawal(
    integrated,
):
    (
        public,
        worker,
        sdk,
        _,
        clock,
        bridge,
        app,
        a,
        b,
        events,
        _,
        _,
        private_sdk,
        server,
    ) = integrated
    for actor, identity, text in (
        (a, "before-a", "PRIVATE_A_CANARY Обсудим интересы."),
        (b, "before-b", "PRIVATE_B_CANARY Обсудим позицию."),
    ):
        assert (
            private_submit(server, actor, identity, text)["model_state"] == "completed"
        )
    before = {name: agent_ids(app, name) for name in ("a", "b")}
    assert all(len(rows) == 1 for rows in before.values())
    generation = bridge.view.ledger.control_generation()
    public_revision(public, worker, sdk, clock, "legitimate-revision", 61)
    assert bridge.view.ledger.control_generation() == generation
    assert {name: agent_ids(app, name) for name in before} == before
    assert len(private_sdk.calls) == 2
    with events._open() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM agent_reply_resolutions WHERE outcome='completed'"
            ).fetchone()[0]
            == 2
        )
    with public.service.store._open() as db:
        db.execute(
            "DELETE FROM threads_inbound_events WHERE account_ref=?", (public.account,)
        )
    assert agent_ids(app, "a") == agent_ids(app, "b") == set()
    assert any(
        "PRIVATE_A_CANARY" in row.content
        for row in app._conversations.history(principal("a"))
    )
    assert any(
        "PRIVATE_B_CANARY" in row.content
        for row in app._conversations.history(principal("b"))
    )


def test_three_public_revisions_keep_complete_distinct_private_context_receipts(
    integrated,
):
    public, worker, sdk, _, clock, _, app, a, b, events, _, _, private_sdk, server = (
        integrated
    )
    original = set()
    for revision in (1, 2, 3):
        if revision > 1:
            public_revision(
                public, worker, sdk, clock, "revision-" + str(revision), 59 + revision
            )
        result = private_submit(
            server,
            a,
            "private-" + str(revision),
            "PRIVATE_A_CANARY Продолжим обсуждение.",
        )
        assert result["model_state"] == "completed"
        current = agent_ids(app, "a")
        assert original < current
        original = current
        with events._open() as db:
            snapshot = json.loads(
                db.execute(
                    "SELECT snapshot FROM agent_intellectual_operations "
                    "WHERE audience='private' ORDER BY rowid DESC LIMIT 1"
                ).fetchone()[0]
            )
            receipts = snapshot["external_receipts"]
            assert len(receipts) == revision
            assert {record["revision"] for record in receipts.values()} == set(
                range(1, revision + 1)
            )
            from app.privacy.application import _validate_graph

            _validate_graph(db)
    assert len(private_sdk.calls) == 3 and len(original) == 3
    assert (
        private_submit(server, b, "separate-b", "Что изменилось публично?")[
            "model_state"
        ]
        == "completed"
    )
    assert "PRIVATE_A_CANARY" not in private_sdk.calls[-1]["input"]


def test_historical_receipts_do_not_release_an_active_stale_public_snapshot(integrated):
    public, worker, sdk, _, clock, _, app, a, _, _, _, _, private_sdk, server = (
        integrated
    )
    private_sdk.before_result = lambda: public_revision(
        public, worker, sdk, clock, "revision-during-inference", 61
    )
    result = private_submit(server, a, "stale-active", "Обсудим интересы.")
    assert result["model_state"] == "unavailable"
    assert len(private_sdk.calls) == 1
    assert agent_ids(app, "a") == set()


def test_private_position_keeps_live_historical_public_evidence(integrated):
    public, worker, sdk, _, clock, _, app, a, _, _, _, _, private_sdk, server = (
        integrated
    )

    def learn():
        sources = json.loads(private_sdk.calls[-1]["input"])["intellectual_state"][
            "sources"
        ]
        handle = next(s["handle"] for s in sources if s["kind"] == "public_agent_state")
        private_sdk.change = proposal(handle)

    private_sdk.before_result = learn
    assert (
        private_submit(server, a, "learn", "Обсудим интерес.")["model_state"]
        == "completed"
    )
    public_revision(public, worker, sdk, clock, "new-position", 61)
    private_sdk.before_result = lambda: None
    private_sdk.change = None
    assert (
        private_submit(server, a, "recall", "Почему тебе это интересно?")["model_state"]
        == "completed"
    )
    frame = json.loads(private_sdk.calls[-1]["input"])["intellectual_state"]
    assert len(frame["states"]) == 1
    assert frame["states"][0]["fields"]["strength"] == 60
    assert len(agent_ids(app, "a")) == 2
    with public.service.store._open() as db:
        db.execute(
            "DELETE FROM threads_inbound_events WHERE account_ref=?", (public.account,)
        )
    assert agent_ids(app, "a") == set()
    assert (
        private_submit(server, a, "after-withdrawal", "Продолжим.")["model_state"]
        == "completed"
    )
    assert (
        json.loads(private_sdk.calls[-1]["input"])["intellectual_state"]["states"] == []
    )


def test_old_public_receipt_is_readable_before_and_after_writer_backfill(
    integrated, monkeypatch
):
    from app.core.intellectual_state import digest
    from app.core.public_intellectual_receipt import source_key
    from app.integration.public_state_history import TABLE, export, initialize

    public, worker, sdk, _, clock, bridge, _, _, _, events, _, _, _, _ = integrated
    with public.journal.transaction() as db:
        item, revision, payload, deps, _ = public.intellectual.state._heads(
            db, public.intellectual.sources()
        )[0]
        legacy = export(
            public.account,
            item,
            revision,
            payload,
            deps,
            bridge.view.ledger.control_generation(),
        )
        db.execute("DROP TABLE " + TABLE)
    key = source_key(public.account, item)
    receipts = {key: legacy["receipt"]}
    with events._open() as private_db:
        assert bridge.historical_sources(
            receipts, principal=principal("a"), _private_db=private_db
        ) == {key: legacy}
        with monkeypatch.context() as isolated:
            isolated.setattr(bridge, "sources", lambda **kwargs: {})
            assert bridge.historical_sources(
                receipts, principal=principal("a"), _private_db=private_db
            ) == {key: legacy}
    # Only the public writer upgrades its own journal. Read-only consumers do not.
    with public.journal.transaction() as db:
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()
            is None
        )
        initialize(db, public.account)
        initialize(db, public.account)
        assert db.execute("SELECT COUNT(*) FROM " + TABLE).fetchone()[0] == 4
    public_revision(public, worker, sdk, clock, "after-upgrade", 61)
    with events._open() as private_db:
        restored = bridge.historical_sources(
            receipts, principal=principal("a"), _private_db=private_db
        )
        assert restored == {key: legacy} and restored[key]["binding"] == digest(
            legacy["receipt"]
        )
        assert (
            bridge.historical_sources(
                receipts,
                principal=principal("a"),
                _private_db=private_db,
                current_keys=frozenset(receipts),
            )
            == {}
        )


@pytest.mark.parametrize(
    "corruption",
    [
        "alias",
        "reasoned",
        "wrong-mode",
        "payload",
        "dependency",
        "generation",
        "receipt",
        "account",
        "key",
        "missing-index",
        "missing-revision",
    ],
)
def test_historical_lookup_rechecks_immutable_content_and_live_authority(
    integrated, corruption
):
    from copy import deepcopy
    from app.integration.public_state_history import TABLE

    public, worker, sdk, _, clock, bridge, _, _, _, events, _, _, _, _ = integrated
    old = deepcopy(bridge.sources())
    key, record = next(iter(old.items()))
    receipts = {key: record["receipt"]}
    public_revision(public, worker, sdk, clock, "after-capture", 61)
    with public.journal.transaction() as db:
        if corruption == "alias":
            db.execute(
                "UPDATE " + TABLE + " SET item='another-item' WHERE alias=?",
                (record["receipt"]["item"],),
            )
        elif corruption == "reasoned":
            db.execute(
                "UPDATE " + TABLE + " SET reasoned=2 WHERE alias=?",
                (record["receipt"]["item"],),
            )
        elif corruption == "wrong-mode":
            db.execute(
                "UPDATE " + TABLE + " SET reasoned=1-reasoned WHERE alias=?",
                (record["receipt"]["item"],),
            )
        elif corruption == "payload":
            db.execute(
                "UPDATE agent_intellectual_revisions SET payload=NULL WHERE revision=1"
            )
        elif corruption == "dependency":
            db.execute(
                "UPDATE agent_intellectual_revisions SET dependencies='{}' WHERE revision=1"
            )
        elif corruption == "missing-index":
            db.execute(
                "DELETE FROM " + TABLE + " WHERE alias=?", (record["receipt"]["item"],)
            )
        elif corruption == "missing-revision":
            db.execute("DELETE FROM agent_intellectual_revisions WHERE revision=1")
    if corruption == "generation":
        with public.ledger._open() as db:
            public.ledger._increment_control_generation(db)
    elif corruption == "receipt":
        receipts[key]["state_digest"] = "0" * 64
    elif corruption == "account":
        receipts[key]["account"] = "different-account"
        from app.core.public_intellectual_receipt import source_key

        receipts = {
            source_key("different-account", receipts[key]["item"]): receipts[key]
        }
    elif corruption == "key":
        receipts = {"public-state:" + "0" * 64: receipts[key]}
    with events._open() as private_db:
        assert (
            bridge.historical_sources(
                receipts, principal=principal("a"), _private_db=private_db
            )
            == {}
        )


@pytest.mark.parametrize(
    "receipts", [[], {}, {str(i): {} for i in range(101)}, {"bad": {}}]
)
def test_historical_lookup_is_closed_and_bounded(integrated, receipts):
    bridge, events = integrated[5], integrated[9]
    with events._open() as db:
        assert (
            bridge.historical_sources(
                receipts, principal=principal("a"), _private_db=db
            )
            == {}
        )


@pytest.mark.parametrize("current_keys", [set(), ["bad"], frozenset({"absent"})])
def test_historical_lookup_rejects_invalid_current_source_requirements(
    integrated, current_keys
):
    bridge, events = integrated[5], integrated[9]
    receipts = {key: source["receipt"] for key, source in bridge.sources().items()}
    with events._open() as db:
        assert (
            bridge.historical_sources(
                receipts,
                principal=principal("a"),
                _private_db=db,
                current_keys=current_keys,
            )
            == {}
        )


def test_legacy_revisions_remain_visible_without_combining_conflicting_receipts(
    integrated, monkeypatch
):
    from app.core.public_intellectual_receipt import source_key
    from app.integration.public_state_history import export

    (
        public,
        worker,
        sdk,
        _,
        clock,
        bridge,
        app,
        a,
        _,
        events,
        _,
        _,
        private_sdk,
        server,
    ) = integrated
    modern_sources = bridge.sources

    def old_producer(**kwargs):
        if kwargs.get("required_keys") is not None:
            return modern_sources(**kwargs)
        with public.journal.transaction() as db:
            item, revision, payload, deps, _ = public.intellectual.state._heads(
                db, public.intellectual.sources()
            )[0]
        source = export(
            public.account,
            item,
            revision,
            payload,
            deps,
            bridge.view.ledger.control_generation(),
        )
        return {source_key(public.account, item): source}

    monkeypatch.setattr(bridge, "sources", old_producer)
    for revision in (1, 2):
        if revision == 2:
            public_revision(public, worker, sdk, clock, "legacy-revision", 61)
        assert (
            private_submit(server, a, "old-" + str(revision), "Обсудим твою позицию.")[
                "model_state"
            ]
            == "completed"
        )
    assert len(agent_ids(app, "a")) == 2
    monkeypatch.setattr(bridge, "sources", modern_sources)
    with events._open() as db:
        operations = tuple(
            row[0]
            for row in db.execute(
                "SELECT operation FROM agent_intellectual_operations ORDER BY operation"
            )
        )
        with pytest.raises(ValueError, match="conflicting public context receipts"):
            app._conversations._intellectual.sources(
                principal("a"), db, _required_operations=operations
            )
    result = private_submit(server, a, "modern-turn", "Что изменилось?")
    assert result["model_state"] == "completed"
    assert len(agent_ids(app, "a")) == 3 and len(private_sdk.calls) == 3
    with events._open() as db:
        from app.privacy.application import _validate_graph

        _validate_graph(db)
        snapshots = [
            json.loads(raw)
            for (raw,) in db.execute(
                "SELECT snapshot FROM agent_intellectual_operations"
            )
        ]
        assert all(
            len(
                {
                    r["revision"]
                    for r in snapshot["external_receipts"].values()
                    if r["item"].startswith("agent:")
                }
            )
            <= 1
            for snapshot in snapshots
        )


def test_reasoned_historical_alias_retains_original_projection_bytes(integrated):
    from app.core.intellectual_state import digest
    from app.core.public_intellectual_receipt import source_key
    from app.integration.public_state_history import export

    public, worker, sdk, _, clock, bridge, _, _, _, events, _, _, _, _ = integrated
    with public.journal.transaction() as db:
        item, revision, payload, deps, _ = public.intellectual.state._heads(
            db, public.intellectual.sources()
        )[0]
    alias = "reasoned-state-" + digest(item)
    reasoned = export(
        public.account,
        alias,
        revision,
        payload,
        deps,
        bridge.view.ledger.control_generation(),
        reasoned=True,
    )
    public_revision(public, worker, sdk, clock, "after-reasoned-receipt", 61)
    key = source_key(public.account, alias)
    with events._open() as db:
        assert bridge.historical_sources(
            {key: reasoned["receipt"]}, principal=principal("a"), _private_db=db
        ) == {key: reasoned}


def test_history_lookup_rechecks_generation_after_read(integrated, monkeypatch):
    public, _, _, _, _, bridge, _, _, _, events, *_ = integrated
    receipts = {key: value["receipt"] for key, value in bridge.sources().items()}
    read = bridge.view.intellectual.sources

    def changed(**kwargs):
        result = read(**kwargs)
        with public.ledger._open() as db:
            public.ledger._increment_control_generation(db)
        return result

    monkeypatch.setattr(bridge.view.intellectual, "sources", changed)
    with events._open() as db:
        assert (
            bridge.historical_sources(
                receipts, principal=principal("a"), _private_db=db
            )
            == {}
        )


def test_old_private_reply_without_public_projection_remains_eligible(integrated):
    _, _, _, _, _, bridge, app, a, _, events, _, _, sdk, server = integrated
    runtime = app._conversations
    runtime._public_intellectual_source = None
    assert (
        private_submit(server, a, "old-private", "PRIVATE_A_CANARY Старый разговор.")[
            "model_state"
        ]
        == "completed"
    )
    runtime._public_intellectual_source = bridge
    assert (
        private_submit(server, a, "new-private", "Продолжим.")["model_state"]
        == "completed"
    )
    assert len(agent_ids(app, "a")) == 2 and len(sdk.calls) == 2
    with events._open() as db:
        assert (
            runtime._public_intellectual_operation_sources(
                db, principal("b"), "missing-operation"
            )
            == {}
        )


@pytest.fixture
def shared_cycle(tmp_path, request):
    from test_public_private_erasure import cycle

    yield from cycle.__wrapped__(tmp_path, request)


def test_historical_shared_revision_keeps_participant_permissions_and_withdrawal(
    shared_cycle, monkeypatch
):
    from app.privacy.application import EventStoreErasureAdapter
    from app.privacy.deletion import DeletionCoordinator
    from test_privacy_deletion import context, request

    _, events, ledger, gate, _, _, root, _, _, bridge, _ = shared_cycle
    with events._open() as db:
        selected = bridge.sources(principal=principal("b"), _private_db=db)
        assert selected
        receipts = {key: value["receipt"] for key, value in selected.items()}
        selected = {
            key: {
                field: value
                for field, value in record.items()
                if field != "reasonless_key"
            }
            for key, record in selected.items()
        }
        from app.core.read_only_sqlite import connect_read_only
        from app.integration.public_state_history import resolve

        live = bridge.view.intellectual.sources(
            _shared_sources=bridge.view.shared_intellectual_source._sources_in(db)
        )
        with connect_read_only(bridge.view.journal.path) as journal:
            assert (
                resolve(
                    journal,
                    bridge.view.account,
                    next(iter(receipts.values())),
                    live,
                    bridge.view.ledger.control_generation(),
                    None,
                )
                is None
            )
        monkeypatch.setattr(bridge, "sources", lambda **kwargs: {})
        assert (
            bridge.historical_sources(
                receipts, principal=principal("b"), _private_db=db
            )
            == selected
        )
        assert (
            bridge.historical_sources(
                receipts, principal=principal("outsider"), _private_db=db
            )
            == {}
        )
    deletion = DeletionCoordinator(ledger, EventStoreErasureAdapter(events)).delete(
        context(), request(content_reference=root)
    )
    assert deletion.disposition.value == "applied" and gate.reconcile()
    with events._open() as db:
        assert (
            bridge.historical_sources(
                receipts, principal=principal("b"), _private_db=db
            )
            == {}
        )
