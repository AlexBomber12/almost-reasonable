"""Optional public influence must fit its complete protected shared ancestry."""

from dataclasses import replace
import json
from threading import Event, Thread

import pytest

from app.cognition.shared import SharedIntelligence
from app.core.intellectual_state import validate_private_graph
from app.integration.public_intellectual_source import PublicIntellectualSource
from app.integration.shared_intellectual_source import SharedIntellectualSource
from app.private_web.runtime import WorkerConversationRuntime
from test_agent_intellectual_state import private_setup, proposal
from test_cognition_pipeline import append_event, principal
from test_int24_runtime import NOW, run_once, setup as public_setup
from test_reviewed_shared import candidate


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


@pytest.mark.parametrize(
    ("roots_per_record", "protected_limit", "admitted"),
    [(40, 45056, False), (10, 2000, False), (10, 45056, True)],
)
def test_import_accounts_for_all_shared_roots_before_optional_selection(
    tmp_path, monkeypatch, roots_per_record, protected_limit, admitted
):
    private_dir, public_dir = tmp_path / "private", tmp_path / "public"
    private_dir.mkdir(mode=0o700)
    public_dir.mkdir(mode=0o700)
    app, _, b, events, _, gate, sdk, server = private_setup(private_dir)
    worker = None
    stop = Event()
    thread = None
    roots, operations = set(), set()
    try:
        shared = SharedIntelligence(
            events, gate, clock=lambda: NOW, ordinary_abstractions_enabled=True
        )
        for group in range(3):
            selected = tuple(
                append_event(
                    events,
                    principal("a"),
                    f"capacity-{group}-{index:02d}-" + "x" * 50,
                    "PRIVATE_ROOT_CANARY Compare causal explanations.",
                )
                for index in range(roots_per_record)
            )
            roots.update(selected)
            item = candidate(selected[0], value="Compare causal models of uncertainty.")
            item = replace(
                item,
                item_id=f"shared-capacity-{group}",
                proposal=replace(
                    item.proposal,
                    operation_id=f"shared-capacity-{group}",
                    protected_source_event_ids=selected,
                ),
                audience=("participant-a", "participant-b", "public:account-1"),
            )
            shared.transition(item)
            operations.add(item.proposal.operation_id)
        change = proposal()
        change["supporting"] = ["s0", "s1", "s2"]
        public, worker, public_sdk, _, clock = public_setup(
            public_dir,
            {
                "decision": "no_action",
                "reason_category": "other",
                "intellectual": change,
            },
            shared_intellectual_source=SharedIntellectualSource(shared, "account-1"),
        )
        run_once(public, worker)
        frame = json.loads(public_sdk.calls[-1]["input"][1]["content"])
        assert len(frame["intellectual_state"]["sources"]) == 3
        bridge = PublicIntellectualSource(
            account=public.account,
            journal_path=public.journal.path,
            threads_path=public.service.store.database,
            ledger_path=public.ledger.path,
            shared_source=SharedIntellectualSource(shared, public.account),
            clock=clock.now,
        )
        source = bridge.sources(principal=principal("b"))
        assert len(source) == 1
        assert set(next(iter(source.values()))["receipt"]["shared"]) == operations
        old = app._conversations
        app._conversations = WorkerConversationRuntime(
            events,
            gate,
            old._transport,
            provider_state=old._provider,
            clock=old._clock,
            public_intellectual_source=bridge,
        )
        # A tighter test budget isolates byte expansion from the 100-root bound.
        monkeypatch.setattr(
            "app.core.public_intellectual_receipt.MAX_PROTECTED_BYTES", protected_limit
        )
        thread = Thread(
            target=lambda: server.serve(should_stop=stop.is_set), daemon=True
        )
        thread.start()
        status, _, body = b.submit("import-capacity", "Как изменились твои интересы?")
        assert (
            status == "201 Created" and json.loads(body)["model_state"] == "completed"
        )
        assert len(sdk.calls) == 1
        sent = json.loads(sdk.calls[-1]["input"])
        assert (
            any(
                row["kind"] == "public_agent_state"
                for row in sent["intellectual_state"]["sources"]
            )
            is admitted
        )
        assert "PRIVATE_ROOT_CANARY" not in sdk.calls[-1]["input"]
        with events._open() as db:
            validate_private_graph(db)
            snapshot = json.loads(
                db.execute(
                    "SELECT snapshot FROM agent_intellectual_operations WHERE audience='private'"
                ).fetchone()[0]
            )
            recorded = {
                key.removeprefix("event:")
                for key in snapshot["dependencies"]
                if key.startswith("event:")
            }
            assert (roots <= recorded) is admitted
            assert len(snapshot["dependencies"]) <= 100
        assert gate.reconcile()
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=5)
            assert not thread.is_alive()
        server.close()
        if worker is not None:
            worker.close()
