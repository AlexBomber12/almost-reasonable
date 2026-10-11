"""Synthetic durable reply storage and privacy boundaries."""

from dataclasses import replace
import sqlite3

import pytest

from app.core.action_policy import ActionPolicy, CapabilityState, TrustedActionContext
from app.core.agent_proposals import ProposalValidation, ReplyProposal
from app.core.agent_output_store import AgentOutputStore, ReplyOperation, ResultState
from app.core.store import EventIdentityConflictError, EventStore, EventValidationError
from test_event_store import principal, private_input


def operation(**changes):
    return replace(
        ReplyOperation(
            operation_id="reply-1",
            initiating_event_id="event-a",
            conversation_id="chat-a",
            correlation_id="correlation-a",
            source_event_ids=("event-a",),
            model_identifier="fake-model",
            prompt_version="prompt-v1",
            core_self_version="self-v1",
        ),
        **changes,
    )


@pytest.fixture
def setup(tmp_path):
    store = EventStore(tmp_path / "events.db")
    store.append_private_input(principal("a"), private_input("a", "synthetic input"))
    return store, AgentOutputStore(store)


def persist(outputs, op=None, content="synthetic reply", context=None):
    route = TrustedActionContext("correlation-a", context or principal("a"))
    caps = CapabilityState("caps-v1", False, False, True, False, True)
    proposal = ReplyProposal(content)
    grant = ActionPolicy().evaluate(ProposalValidation(proposal, None), route, caps)
    return outputs.persist_reply(route, op or operation(), grant, caps)


def test_reservation_commit_restart_duplicate_and_conflict(setup):
    store, outputs = setup
    pending = outputs.reserve_reply(principal("a"), operation())
    assert pending.result_state is ResultState.PENDING
    assert pending.content is None
    assert outputs.reserve_reply(principal("a"), operation()) == pending
    outputs = AgentOutputStore(EventStore(store.database_path))
    assert outputs.read_reply(principal("a"), "reply-1") == pending
    saved = persist(outputs)
    assert saved.result_state is ResultState.PERSISTED
    assert saved.delivery_state == "held"
    assert saved.content == "synthetic reply"
    assert persist(outputs) == saved
    with pytest.raises(EventIdentityConflictError):
        persist(outputs, content="changed reply")
    with pytest.raises(EventIdentityConflictError):
        outputs.reserve_reply(principal("a"), operation(conversation_id="chat-other"))
    with sqlite3.connect(store.database_path) as db:
        assert db.execute("SELECT count(*) FROM agent_reply_payloads").fetchone() == (
            1,
        )


def test_scope_isolation_and_policy_required(setup):
    store, outputs = setup
    outputs.reserve_reply(principal("a"), operation())
    assert outputs.read_reply(principal("b"), "reply-1") is None
    with pytest.raises(EventValidationError):
        outputs.reserve_reply(principal("b"), operation())
    with pytest.raises(EventValidationError):
        persist(outputs, context=principal("b"))
    with pytest.raises(EventValidationError):
        outputs.reserve_reply(None, operation())
    with pytest.raises(EventValidationError):
        outputs.persist_reply(None, operation(), None, None)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM private_payloads",
        "UPDATE private_payloads SET content=''",
        "UPDATE private_payloads SET retention_status='deleted'",
        "DELETE FROM protected_event_taints",
        "UPDATE protected_event_taints SET source_principal_id='participant-b'",
        "UPDATE agent_reply_sources SET retention_status='invalidated'",
        "DELETE FROM agent_reply_sources",
        "DELETE FROM agent_reply_payloads",
        "UPDATE agent_reply_payloads SET retention_status='deleted'",
        "UPDATE agent_reply_operations SET retention_status='invalidated'",
    ],
)
def test_deleted_or_tainted_material_never_reexposed(setup, sql):
    store, outputs = setup
    outputs.reserve_reply(principal("a"), operation())
    persist(outputs)
    with sqlite3.connect(store.database_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(sql)
    assert outputs.read_reply(principal("a"), "reply-1") is None
    with pytest.raises(EventValidationError):
        persist(outputs)


@pytest.mark.parametrize(
    "changes",
    [
        {"operation_id": "bad token"},
        {"source_event_ids": []},
        {"source_event_ids": ()},
        {"source_event_ids": (None,)},
        {"source_event_ids": ("bad token",)},
        {"source_event_ids": ("event-a", "event-a")},
        {"source_event_ids": ("event-z",)},
        {"correlation_id": "other-correlation"},
    ],
)
def test_malformed_operations_rejected(setup, changes):
    _, outputs = setup
    with pytest.raises(EventValidationError):
        outputs.reserve_reply(principal("a"), operation(**changes))


def test_untyped_operation_rejected(setup):
    _, outputs = setup
    with pytest.raises(EventValidationError):
        outputs.reserve_reply(principal("a"), None)


@pytest.mark.parametrize(
    "kind",
    ["stop", "disabled", "real-effects", "wrong-route", "no-action", "unreserved"],
)
def test_current_policy_denials_never_persist(setup, kind):
    from app.core.agent_proposals import NoActionProposal, NoActionReason

    _, outputs = setup
    if kind != "unreserved":
        outputs.reserve_reply(principal("a"), operation())
    route = TrustedActionContext("correlation-a", principal("a"))
    caps = CapabilityState("caps-v1", False, False, True, False, True)
    proposal = ReplyProposal("synthetic reply")
    if kind == "no-action":
        proposal = NoActionProposal(NoActionReason.NOT_WARRANTED)
    grant = ActionPolicy().evaluate(ProposalValidation(proposal, None), route, caps)
    if kind == "stop":
        caps = replace(caps, emergency_stop_active=True)
    if kind == "disabled":
        caps = replace(caps, private_reply_drafts_enabled=False)
    if kind == "real-effects":
        caps = replace(caps, simulation_only=False)
    if kind == "wrong-route":
        route = TrustedActionContext("correlation-a", principal("a"))
    with pytest.raises(EventValidationError):
        outputs.persist_reply(route, operation(), grant, caps)


def test_persist_binding_conflict(setup):
    _, outputs = setup
    outputs.reserve_reply(principal("a"), operation())
    with pytest.raises(EventIdentityConflictError):
        persist(outputs, operation(prompt_version="changed-prompt"))


@pytest.mark.parametrize(
    "point,committed",
    [
        ("after_reply_payload_insert", False),
        ("after_reply_commit", True),
    ],
)
def test_interruption_preserves_operation_and_committed_result(setup, point, committed):
    from app.core.store import FaultPoint

    store, outputs = setup
    outputs.reserve_reply(principal("a"), operation())

    def fail(actual):
        if actual == FaultPoint(point):
            raise RuntimeError("synthetic interruption")

    store._fault_injector = fail
    with pytest.raises(RuntimeError):
        persist(outputs)
    restarted = AgentOutputStore(EventStore(store.database_path))
    recovered = restarted.read_reply(principal("a"), "reply-1")
    assert recovered.result_state is (
        ResultState.PERSISTED if committed else ResultState.PENDING
    )
    assert persist(restarted).content == "synthetic reply"


def test_concurrent_reservations_and_results_are_singletons(setup):
    from concurrent.futures import ThreadPoolExecutor

    store, outputs = setup
    with ThreadPoolExecutor(max_workers=2) as pool:
        reservations = list(
            pool.map(
                lambda _: outputs.reserve_reply(principal("a"), operation()), range(2)
            )
        )
        replies = list(pool.map(lambda _: persist(outputs), range(2)))
    assert reservations[0] == reservations[1]
    assert replies[0] == replies[1]
    with sqlite3.connect(store.database_path) as db:
        assert db.execute("SELECT count(*) FROM agent_reply_operations").fetchone() == (
            1,
        )
        assert db.execute("SELECT count(*) FROM agent_reply_payloads").fetchone() == (
            1,
        )


@pytest.mark.parametrize("method", ["reserve", "persist", "read"])
def test_sqlite_failure_is_sanitized_and_rolls_back(setup, method):
    from app.core.store import EventStoreBusyError

    store, outputs = setup
    outputs.reserve_reply(principal("a"), operation())
    store._busy_timeout_ms = 1
    with sqlite3.connect(store.database_path) as db:
        db.execute("BEGIN EXCLUSIVE")
        if method == "read":
            # WAL readers retain their committed snapshot while a writer holds the lock.
            assert outputs.read_reply(principal("a"), "reply-1").content is None
        else:
            with pytest.raises(EventStoreBusyError):
                if method == "reserve":
                    outputs.reserve_reply(principal("a"), operation())
                else:
                    persist(outputs)
    assert (
        outputs.read_reply(principal("a"), "reply-1").result_state
        is ResultState.PENDING
    )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE agent_reply_operations SET correlation_id='different'",
        "UPDATE agent_reply_operations SET internal_decision='accepted_internal'",
        "INSERT INTO agent_reply_payloads VALUES ('reply-1', 'synthetic unexpected', 'active')",
    ],
)
def test_corrupt_reply_binding_and_lifecycle_rejected(setup, sql):
    store, outputs = setup
    outputs.reserve_reply(principal("a"), operation())
    with sqlite3.connect(store.database_path) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(sql)
    with pytest.raises(EventValidationError):
        outputs.read_reply(principal("a"), "reply-1")


def test_reservation_does_not_resurrect_removed_result(setup):
    store, outputs = setup
    outputs.reserve_reply(principal("a"), operation())
    persist(outputs)
    with sqlite3.connect(store.database_path) as db:
        db.execute("DELETE FROM agent_reply_payloads")
    with pytest.raises(EventValidationError):
        outputs.reserve_reply(principal("a"), operation())


def test_changed_source_set_is_not_identical_operation(setup):
    store, outputs = setup
    for name in ("c", "d"):
        store.append_private_input(
            principal("a"), private_input(name, "synthetic evidence")
        )
    outputs.reserve_reply(
        principal("a"), operation(source_event_ids=("event-a", "event-c"))
    )
    with pytest.raises(EventValidationError):
        outputs.reserve_reply(
            principal("a"), operation(source_event_ids=("event-a", "event-d"))
        )


def test_global_identity_cannot_be_reused_by_valid_other_principal(setup):
    from app.core.events import EventAudience

    store, outputs = setup
    outputs.reserve_reply(principal("a"), operation())
    store.append_private_input(
        principal("b"),
        private_input(
            "b",
            "synthetic b",
            audience=EventAudience.private_for(principal("b").principal_id),
        ),
    )
    with pytest.raises(EventIdentityConflictError):
        outputs.reserve_reply(
            principal("b"),
            operation(
                initiating_event_id="event-b",
                correlation_id="correlation-b",
                source_event_ids=("event-b",),
            ),
        )


def test_sqlite_read_failure_is_sanitized(setup, monkeypatch):
    from app.core.store import EventStoreError

    _, outputs = setup

    def fail(*args):
        raise sqlite3.DatabaseError("synthetic private diagnostic")

    monkeypatch.setattr(outputs, "_read", fail)
    with pytest.raises(EventStoreError, match="^event store operation failed$"):
        outputs.read_reply(principal("a"), "reply-1")


def test_reservation_source_insert_failure_rolls_back_identity(setup, monkeypatch):
    from app.core.store import EventStoreError

    store, outputs = setup
    original_connect = sqlite3.connect

    class FailingSourcesConnection(sqlite3.Connection):
        def executemany(self, statement, parameters):
            raise sqlite3.IntegrityError("synthetic private provenance failure")

    with monkeypatch.context() as patch:
        patch.setattr(
            sqlite3,
            "connect",
            lambda *args, **kwargs: original_connect(
                *args, **kwargs, factory=FailingSourcesConnection
            ),
        )
        with pytest.raises(EventStoreError, match="^event store operation failed$"):
            outputs.reserve_reply(principal("a"), operation())
    assert outputs.read_reply(principal("a"), "reply-1") is None
    assert (
        outputs.reserve_reply(principal("a"), operation()).result_state
        is ResultState.PENDING
    )


def test_multi_source_dependency_removal_hides_reply(setup):
    store, outputs = setup
    store.append_private_input(principal("a"), private_input("c", "synthetic context"))
    op = operation(source_event_ids=("event-a", "event-c"))
    outputs.reserve_reply(principal("a"), op)
    persist(outputs, op)
    with sqlite3.connect(store.database_path) as db:
        db.execute("DELETE FROM private_payloads WHERE payload_id='payload-c'")
    assert outputs.read_reply(principal("a"), "reply-1") is None
