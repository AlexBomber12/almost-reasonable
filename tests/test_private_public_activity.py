"""Native public dispatch -> protected local context -> two private IPC chats.

SDKs and Threads HTTP are offline fakes, not evidence of model behaviour.
"""

from datetime import timedelta
from dataclasses import replace
import json

import pytest

from app.integration.public_intellectual_source import PublicIntellectualSource
from app.integration.shared_intellectual_source import SharedIntellectualSource
from app.cognition.shared import SharedIntelligence
from app.threads.domain import OutboxState
from test_agent_intellectual_state import private_submit, proposal
from test_public_experience import wake
from test_public_private_intellectual import integrated as integrated
from test_reviewed_shared import candidate


POST = "Мне интересно, как сомнение помогает менять собственную позицию."


@pytest.fixture
def dialogue(request):
    values = request.getfixturevalue("integrated")
    public, _, _, _, clock, _bridge, app, *_ = values
    bridge = PublicIntellectualSource(
        account=public.account,
        journal_path=public.journal.path,
        threads_path=public.service.store.database,
        ledger_path=public.ledger.path,
        clock=clock.now,
        include_public_activity=True,
    )
    app._conversations._public_intellectual_source = bridge
    return (*values[:5], bridge, *values[6:])


def publication(values, identity="dialogue-post", text=POST, dispatch=True):
    public, worker, sdk, _http, clock, *_ = values
    sdk.proposal = {"decision": "propose_publication", "draft": text}
    wake(public, worker, clock, identity)
    operation = public.wakeup_operation(identity)
    if dispatch:
        public.approve(operation, expires_at=clock.now() + timedelta(minutes=5))
        assert public.dispatch(operation).state is OutboxState.CONFIRMED
    return operation


def post_sources(bridge, **kwargs):
    return {
        k: v
        for k, v in bridge.sources(**kwargs).items()
        if v["kind"] == "confirmed_publication"
    }


def test_confirmed_own_post_and_recorded_limits_reach_both_private_chats(dialogue):
    public, _, _, _, _, bridge, _, a, b, _, _, _, sdk, server = dialogue
    publication(dialogue)
    assert len(post_sources(bridge)) == 1
    assert (
        private_submit(
            server, a, "a-post", "Почему ты это опубликовал? PRIVATE_A_CANARY"
        )["model_state"]
        == "completed"
    )
    frame = json.loads(sdk.calls[-1]["input"])["intellectual_state"]
    record = next(s for s in frame["sources"] if s["kind"] == "confirmed_publication")
    value = json.loads(record["text"])
    assert value["text"] == POST and value["publication_id"] == "post-1"
    assert value["occurred_at"] and value["recorded_reason_category"] is None
    assert "not independently verified" in value["meaning"]
    assert "Do not invent a historical motive" in sdk.calls[-1]["instructions"]
    assert (
        private_submit(server, b, "b-post", "Что ты хотел сказать своим постом?")[
            "model_state"
        ]
        == "completed"
    )
    payload = sdk.calls[-1]["input"]
    assert POST in payload and "PRIVATE_A_CANARY" not in payload
    assert str(public.journal.path) not in payload and public.account not in payload
    assert "Русская архитектура" in payload  # Existing actual public position remains.


@pytest.mark.parametrize("shared_count", [0, 8])
def test_dense_private_memory_preserves_public_identity_with_shared_context(
    dialogue, monkeypatch, shared_count
):
    from app.cognition.contracts import InterestMemoryProposal
    from app.cognition.pipeline import CognitionEngine
    from app.core.self_state import InterestStatus
    from app.core.self_state_store import SelfStateStore
    from app.private_web.runtime import WorkerConversationRuntime
    from test_cognition_pipeline import (
        FakeExtractor,
        append_event,
        principal,
        proposal_context,
    )
    from test_shared_intelligence import approval
    import test_cognition_pipeline as cognition

    _, _, _, _, _, _, app, a, b, events, _, gate, sdk, server = dialogue
    publication(dialogue)
    runtime = app._conversations
    monkeypatch.setattr(cognition, "NOW", runtime._clock() - timedelta(minutes=2))
    # Real persisted synthetic memories, not a mocked serialized request. Dense
    # optional material must not evict the agent's common public experience.
    for index in range(16):
        root = append_event(events, principal("a"), f"dense-{index}", "PRIVATE_DENSE_A")
        value = InterestMemoryProposal(
            proposal_context(root, f"dense-{index}"),
            f"interest-{index}",
            f"Astronomy {index}: " + "synthetic observations " * 50,
            70,
            InterestStatus.ACTIVE,
            "synthetic motivation " * 30,
        )
        assert (
            CognitionEngine(
                events, SelfStateStore(events), extractor=FakeExtractor(value)
            )
            .process(principal("a"), root)
            .status.value
            == "persisted"
        )
    approvals = tuple(
        approval(root, op=f"shared-{i}", item=f"shared-{i}")
        for i in range(shared_count)
    )
    runtime._shared = SharedIntelligence(
        events,
        gate,
        approvals=approvals,
        clock=runtime._clock,
    )
    for item in approvals:
        runtime._shared.commit(item)
    private_submit(
        server, a, "dense-question", "Astronomy: почему ты написал свой пост?"
    )
    raw = sdk.calls[-1]["input"]
    frame = json.loads(raw)
    assert len(raw.encode("utf-8")) <= runtime._limits.max_utf8_bytes
    assert frame["memories"] and frame["memories_omitted"]
    assert POST in raw and "Русская архитектура" in raw
    assert frame["intellectual_state"] is not None
    assert frame["shared_omitted"] is (shared_count > 0)
    # Reconstruct the runtime from the same durable stores. Optional context
    # omission must not erase memories or require a process-local state cache.
    app._conversations = WorkerConversationRuntime(
        events,
        gate,
        runtime._transport,
        provider_state=runtime._provider,
        clock=runtime._clock,
        shared_intelligence=runtime._shared,
        public_intellectual_source=dialogue[5],
    )
    private_submit(server, a, "dense-return", "Astronomy: вернёмся к твоему посту.")
    restored = json.loads(sdk.calls[-1]["input"])
    assert restored["memories"] and POST in sdk.calls[-1]["input"]
    assert len(SelfStateStore(events).retrieve_private_candidates(principal("a"))) == 16
    # A sparse second participant sees the same post without A's private data.
    private_submit(server, b, "sparse-question", "Почему ты написал свой пост?")
    other = sdk.calls[-1]["input"]
    assert POST in other and "Русская архитектура" in other
    assert "PRIVATE_DENSE_A" not in other and "Astronomy 0:" not in other


def test_drafts_unknown_and_failed_publications_are_not_confirmed_dialogue(dialogue):
    public, _, _, _, _, bridge, *_ = dialogue
    operation = publication(dialogue, dispatch=False)
    assert post_sources(bridge) == {}
    public.approve(operation, expires_at=dialogue[4].now() + timedelta(minutes=5))
    assert post_sources(bridge) == {}
    assert public.dispatch(operation).state is OutboxState.CONFIRMED
    assert post_sources(bridge)
    for state in ("unknown", "failed_terminal"):
        with public.service.store._open() as db:
            db.execute(
                "UPDATE threads_outbox SET state=? WHERE operation_id=?",
                (state, operation),
            )
        assert post_sources(bridge) == {}


def test_recorded_no_action_is_a_category_not_an_invented_explanation(dialogue):
    public, worker, sdk, _, clock, bridge, *_ = dialogue
    sdk.proposal = {"decision": "no_action", "reason_category": "not_warranted"}
    wake(public, worker, clock, "decline-publication")
    choices = [
        json.loads(r["text"])
        for r in bridge.sources().values()
        if r["kind"] == "no_action"
    ]
    assert choices and choices[0]["recorded_reason_category"] == "not_warranted"
    assert choices[0]["publication_id"] is None


def test_withdrawal_during_private_inference_blocks_release(dialogue):
    public, _, _, _, _, bridge, _, a, _, _, _, _, sdk, server = dialogue
    operation = publication(dialogue)
    assert post_sources(bridge)

    def withdraw():
        with public.journal.transaction() as db:
            db.execute(
                "UPDATE sources SET active=0 WHERE draft=(SELECT json_extract(payload,'$.draft') FROM intents WHERE operation=?)",
                (operation,),
            )

    sdk.before_result = withdraw
    result = private_submit(server, a, "race-post", "Обсудим твой пост и мотивы.")
    assert result["model_state"] == "unavailable" and result["response"] is None
    assert post_sources(bridge) == {}


def test_activity_receipt_survives_selection_eviction_but_not_withdrawal(dialogue):
    (
        public,
        worker,
        public_sdk,
        _,
        clock,
        bridge,
        app,
        a,
        _,
        events,
        _,
        gate,
        sdk,
        server,
    ) = dialogue
    operation = publication(dialogue)
    original = post_sources(bridge)

    def learn():
        frame = json.loads(sdk.calls[-1]["input"])["intellectual_state"]
        source = next(
            r for r in frame["sources"] if r["kind"] == "confirmed_publication"
        )
        sdk.change = proposal(source["handle"])

    sdk.before_result = learn
    assert (
        private_submit(
            server, a, "learn-post", "Что этот пост говорит о твоих интересах?"
        )["model_state"]
        == "completed"
    )
    with events._open() as db:
        count = db.execute(
            "SELECT count(*) FROM agent_intellectual_revisions"
        ).fetchone()[0]
    assert count == 1
    sdk.before_result = lambda: None
    sdk.change = None
    # More recent local choices cannot crowd the actual post out of the source pool.
    public_sdk.proposal = {"decision": "no_action", "reason_category": "other"}
    for index in range(8):
        wake(public, worker, clock, "later-choice-" + str(index))
    assert post_sources(bridge, required_keys=frozenset(original)) == original
    with public.journal.transaction() as db:
        db.execute(
            "UPDATE sources SET active=0 WHERE draft=(SELECT json_extract(payload,'$.draft') FROM intents WHERE operation=?)",
            (operation,),
        )
    assert post_sources(bridge, required_keys=frozenset(original)) == {}
    assert (
        private_submit(server, a, "after-post-withdrawal", "Продолжим.")["model_state"]
        == "completed"
    )
    assert POST not in sdk.calls[-1]["input"]


def test_activity_is_opt_in_and_bound_by_closed_protected_config(dialogue, tmp_path):
    public, _, _, _, clock, bridge, *_ = dialogue
    publication(dialogue)
    config = dict(
        version="public-intellectual-source-v1",
        account=public.account,
        journal_path=str(public.journal.path),
        threads_path=str(public.service.store.database),
        ledger_path=str(public.ledger.path),
        mission_path=None,
    )
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(config))
    path.chmod(0o400)
    assert not post_sources(PublicIntellectualSource.from_config(path, clock=clock.now))
    path.chmod(0o600)
    path.write_text(json.dumps(config | {"include_public_activity": True}))
    path.chmod(0o400)
    assert post_sources(
        PublicIntellectualSource.from_config(path, clock=clock.now)
    ) == post_sources(bridge)
    for bad in ("yes", 1, None):
        path.chmod(0o600)
        path.write_text(json.dumps(config | {"include_public_activity": bad}))
        path.chmod(0o400)
        with pytest.raises(ValueError, match="activity"):
            PublicIntellectualSource.from_config(path, clock=clock.now)


@pytest.mark.parametrize("allow_b", [False, True])
def test_public_activity_keeps_private_ancestry_and_audience_boundaries(
    dialogue, allow_b
):
    from app.privacy.application import EventStoreErasureAdapter
    from app.privacy.deletion import DeletionCoordinator
    from test_privacy_deletion import context, request

    public, _, _, _, clock, bridge, app, a, b, events, ledger, gate, sdk, server = (
        dialogue
    )
    private_submit(server, a, "shared-a", "PRIVATE_SHARED_A_CANARY Обсудим объяснения.")
    with events._open() as db:
        root = db.execute(
            "SELECT event_id FROM event_envelopes WHERE source_message_id='shared-a'"
        ).fetchone()[0]
    shared = SharedIntelligence(
        events, gate, clock=clock.now, ordinary_abstractions_enabled=True
    )
    audiences = ("participant-a", "public:account-1") + (
        ("participant-b",) if allow_b else ()
    )
    item = replace(
        candidate(root, value="Compare causal models of uncertainty."),
        audience=tuple(sorted(audiences)),
    )
    shared.transition(item)
    source = SharedIntellectualSource(shared, public.account)
    public.shared_intellectual_source = source
    bridge.view.shared_intellectual_source = source
    publication(dialogue)
    assert post_sources(bridge)
    private_submit(server, b, "shared-post-b", "Почему ты написал этот пост?")
    frame = json.loads(sdk.calls[-1]["input"])["intellectual_state"]
    assert (
        any(row["kind"] == "confirmed_publication" for row in frame["sources"])
        is allow_b
    )
    assert "PRIVATE_SHARED_A_CANARY" not in sdk.calls[-1]["input"]
    if allow_b:
        with events._open() as db:
            operation = db.execute(
                "SELECT operation_id FROM agent_reply_operations WHERE audience_principal_id='participant-b'"
            ).fetchone()[0]
            assert db.execute(
                "SELECT shared_operation FROM protected_reply_shared WHERE operation_id=?",
                (operation,),
            ).fetchall() == [(item.proposal.operation_id,)]
        result = DeletionCoordinator(ledger, EventStoreErasureAdapter(events)).delete(
            context(), request(content_reference=root)
        )
        assert result.disposition.value == "applied" and gate.reconcile()
        assert post_sources(bridge) == {}
        with events._open() as db:
            assert db.execute(
                "SELECT retention_status FROM agent_reply_operations WHERE operation_id=?",
                (operation,),
            ).fetchone() == ("invalidated",)


def test_activity_selection_revalidates_two_independent_store_reads(
    dialogue, monkeypatch
):
    publication(dialogue)
    bridge = dialogue[5]
    original = bridge.view.experience_history._candidates
    calls = []

    def changed():
        values = original()
        calls.append(True)
        if len(calls) == 2:
            return {
                k: (record, "changed-binding")
                for k, (record, binding) in values.items()
            }
        return values

    monkeypatch.setattr(bridge.view.experience_history, "_candidates", changed)
    assert post_sources(bridge) == {}


def test_oversized_activity_is_omitted_without_hiding_public_positions(dialogue):
    public, _, _, _, _, bridge, *_ = dialogue
    publication(dialogue)
    # A mutually consistent but oversized locally recorded post must not overflow
    # the closed model projection; it is not silently truncated into a false quote.
    with public.journal.transaction() as db:
        db.execute(
            "UPDATE intents SET payload=json_set(payload,'$.content',?) WHERE json_extract(payload,'$.decision')='propose_publication'",
            ("Я" * 3000,),
        )
    with public.service.store._open() as db:
        db.execute("UPDATE threads_outbox SET content=?", ("Я" * 3000,))
    assert post_sources(bridge) == {}
    assert any(r["kind"] == "public_agent_state" for r in bridge.sources().values())


def test_orphaned_shared_activity_is_not_laundered_as_public(dialogue, monkeypatch):
    publication(dialogue)
    bridge = dialogue[5]
    sources = bridge.view.intellectual.sources()
    for source in sources.values():
        if source["kind"] == "confirmed_publication":
            source["dependencies"] = {"shared:missing-root": "untrusted-binding"}
    monkeypatch.setattr(bridge.view.intellectual, "sources", lambda **_kwargs: sources)
    assert post_sources(bridge) == {}


def test_overbound_private_ancestry_omits_activity_instead_of_dropping_roots(
    dialogue, monkeypatch
):
    publication(dialogue)
    public, _, _, _, clock, bridge, _, _, _, events, _, gate, *_ = dialogue
    sources = bridge.view.intellectual.sources()
    for source in sources.values():
        if source["kind"] == "confirmed_publication":
            source["dependencies"] = {
                "shared:root-" + str(i): "binding" for i in range(17)
            }
    shared = SharedIntelligence(
        events, gate, clock=clock.now, ordinary_abstractions_enabled=True
    )
    bridge.view.shared_intellectual_source = SharedIntellectualSource(
        shared, public.account
    )
    monkeypatch.setattr(bridge.view.intellectual, "sources", lambda **_kwargs: sources)
    assert post_sources(bridge) == {}
