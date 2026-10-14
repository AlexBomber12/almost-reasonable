"""Connected synthetic private generic-memory graph evidence."""

from dataclasses import replace

from app.cognition.contracts import (
    CognitionCapabilities,
    CognitionOutcomeStatus,
    ExtractorIdentity,
    GenericMemoryProposal,
    GenericReviewState,
    MemoryKind,
    NoChangeProposal,
    ProposalContext,
)
from app.cognition.generic_store import GenericMemoryStore
from app.cognition.generic_store import (
    GenericMemoryError,
    GenericMemoryConflict,
    GenericMemoryStale,
)
from app.cognition.pipeline import CognitionEngine
from app.cognition.retrieval import MemoryContextSelector
from app.cognition.conversation_context import MemoryConversationAssembler
from app.core.context import ContextLimits
from app.cognition.contracts import RetrievalRequest, RetrievalPurpose
from app.cognition.synthetic import SyntheticMemoryExtractor
from app.core.events import VerificationStatus
from app.core.self_state_store import SelfStateStore
from app.core.store import EventStore, FaultPoint, schema_objects
from app.privacy.application import EventStoreErasureAdapter
from test_cognition_pipeline import NOW, append_event, principal
from test_privacy_deletion import request
from test_private_memory_runtime import setup as runtime_setup
from test_private_conversation_runtime import delete_source, NOW as RUNTIME_NOW
from app.private_web.runtime import Stage0ConversationRuntime
from app.private_web.auth import AuthStore
from app.private_web.wsgi import PrivateWebApplication
from app.privacy.application import PrivacyReadGate, EventStoreRestoreAdapter
from app.privacy.recovery import QuarantinedRestore, create_sqlite_backup
from test_stage0_acceptance_journeys import journey as wsgi_journey, submitted, trusted
import json
import sqlite3
import hashlib
import pytest
import subprocess
import sys
from acceptance.guards import install
from app.core.generic_graph import validate_generic_graph
from app.core.agent_output_store import AgentOutputStore, ReplyOperation
from app.core.store import EventValidationError
from shared_schema_helpers import remove_v7
from app.privacy.recovery import _schema_version


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    install(monkeypatch)


def proposal(source, item, operation, *, parents=(), expected=None, evidence=()):
    return GenericMemoryProposal(
        ProposalContext(
            operation,
            source,
            (source,),
            evidence,
            expected,
            "Synthetic review",
            NOW,
            NOW,
        ),
        MemoryKind.CANDIDATE_USEFUL_MEMORY,
        "Synthetic memory " + item,
        0.4,
        VerificationStatus.UNVERIFIED,
        item,
        parents,
    )


def engine(store, candidate):
    return CognitionEngine(
        store,
        SelfStateStore(store),
        extractor=SyntheticMemoryExtractor(
            lambda _request: candidate, ExtractorIdentity("synthetic", "v1")
        ),
        capabilities=CognitionCapabilities(
            generic_memory_write=True, private_consolidation=True
        ),
        clock=lambda: NOW,
    )


def test_multilevel_private_consolidation_replay_restart_and_erasure(tmp_path):
    store = EventStore(tmp_path / "events.db")
    a, b = principal("a"), principal("b")
    for name, who in (
        ("one", a),
        ("two", a),
        ("three", a),
        ("four", a),
        ("five", a),
        ("other", b),
    ):
        append_event(store, who, name)
    for name, item in (("one", "m1"), ("two", "m2")):
        evidence = ("event-two",) if item == "m1" else ()
        result = engine(
            store, proposal("event-" + name, item, "op-" + item, evidence=evidence)
        ).process(a, "event-" + name, evidence_event_ids=evidence)
        assert result.status is CognitionOutcomeStatus.PERSISTED
    first = proposal("event-three", "m3", "op-m3", parents=(("m1", 1), ("m2", 1)))
    assert engine(store, first).process(a, "event-three").generic_revision == ("m3", 1)
    second = proposal("event-four", "m4", "op-m4", parents=(("m3", 1), ("m2", 1)))
    assert engine(store, second).process(a, "event-four").generic_revision == ("m4", 1)
    revision = proposal("event-five", "m1", "op-m1-rev", expected=1)
    assert engine(store, revision).process(a, "event-five").generic_revision == (
        "m1",
        2,
    )
    assert engine(store, first).process(a, "event-three").generic_revision == ("m3", 1)
    assert (
        engine(store, first).process(b, "event-three").status
        is CognitionOutcomeStatus.REJECTED
    )
    reopened = EventStore(store.database_path)
    reads = GenericMemoryStore(reopened).current(a)
    assert [read.view.item_id for read in reads] == ["m1", "m2", "m3", "m4"]
    assert reads[-1].source_event_ids == (
        "event-four",
        "event-one",
        "event-three",
        "event-two",
    )
    assert reads[-1].view.counterevidence_count == 1
    assert reads[-1].view.verification is VerificationStatus.UNVERIFIED
    changed = replace(first, statement="Conflicting replay")
    assert (
        engine(reopened, changed).process(a, "event-three").status
        is CognitionOutcomeStatus.REJECTED
    )
    assert (
        EventStoreErasureAdapter(reopened)
        .erase(request(content_reference="event-one"))
        .blockers
        == ()
    )
    assert [read.view.item_id for read in GenericMemoryStore(reopened).current(a)] == [
        "m2"
    ]
    assert GenericMemoryStore(reopened).current(b) == ()


def test_later_trusted_conversation_uses_consolidated_private_memory(tmp_path):
    store, ledger, gate, fake, _ = runtime_setup.__wrapped__(tmp_path)
    index = 0

    def generate(r):
        nonlocal index
        index += 1
        source = r.experience.event_id
        if index < 3:
            return proposal(source, f"m{index}", f"op-m{index}")
        if index == 3:
            assert {m.item_id for m in r.private_memories} == {"m1", "m2"}
            return proposal(source, "m3", "op-m3", parents=(("m1", 1), ("m2", 1)))
        return NoChangeProposal(source, "No update", (source,))

    caps = CognitionCapabilities(generic_memory_write=True, private_consolidation=True)
    runtime = Stage0ConversationRuntime(
        store,
        gate,
        fake,
        memory_extractor=SyntheticMemoryExtractor(
            generate, ExtractorIdentity("synthetic", "v1")
        ),
        cognition_capabilities=caps,
    )
    for name in ("one", "two", "three"):
        runtime.submit(principal("a"), name, "Синтетическое наблюдение")
    reopened = EventStore(store.database_path)
    fresh_gate = PrivacyReadGate(ledger, reopened)
    assert fresh_gate.reconcile()
    fresh = Stage0ConversationRuntime(
        reopened,
        fresh_gate,
        fake,
        memory_extractor=SyntheticMemoryExtractor(
            lambda r: NoChangeProposal(
                r.experience.event_id, "No update", (r.experience.event_id,)
            ),
            ExtractorIdentity("synthetic", "v1"),
        ),
        cognition_capabilities=caps,
    )
    for index in range(101):
        append_event(reopened, principal("a"), f"padding-{index}")
    fresh.submit(principal("a"), "later", "Что помнишь?")
    projected = json.loads(fake.calls[-1].request.input_text)
    assert {memory["text"] for memory in projected["memories"]} >= {
        "Synthetic memory m3"
    }
    assert all("item_id" not in memory for memory in projected["memories"])
    later = fresh_gate.retrieve_private_events(principal("a"))[-1].event_id
    reply = fresh_gate.read_reply(principal("a"), fresh._operation_id(later))
    sources = reply.operation.source_event_ids
    assert len(sources) >= 4
    assert set(
        GenericMemoryStore(reopened).current(principal("a"))[-1].source_event_ids
    ) <= set(sources)
    fresh.submit(principal("b"), "later", "Что помнишь?")
    assert json.loads(fake.calls[-1].request.input_text)["memories"] == []
    backup = tmp_path / "old.db"
    create_sqlite_backup(reopened.database_path, backup, 7, RUNTIME_NOW)
    first = sorted(
        GenericMemoryStore(reopened).current(principal("a")),
        key=lambda read: read.view.item_id,
    )[0].source_event_ids[0]
    assert (
        delete_source(
            (reopened, ledger, fresh_gate, fake, fresh), first
        ).disposition.value
        == "applied"
    )
    assert fresh_gate.reconcile()
    assert fresh_gate.read_reply(principal("a"), fresh._operation_id(later)) is None
    assert [
        read.view.item_id
        for read in GenericMemoryStore(reopened).current(principal("a"))
    ] == ["m2"]
    quarantine = tmp_path / "quarantine.db"
    assert (
        QuarantinedRestore(ledger, 7, EventStoreRestoreAdapter)
        .restore(backup, quarantine, RUNTIME_NOW)
        .activation_eligible
    )
    with sqlite3.connect(f"{quarantine.as_uri()}?mode=ro", uri=True) as db:
        assert db.execute(
            "SELECT statement FROM generic_memory_payloads"
        ).fetchall() == [("Synthetic memory m2",)]


def test_authenticated_wsgi_multilevel_consolidation_reaches_later_private_context(
    tmp_path,
):
    clock, auth, events, ledger, gate, fake, config, a, b = wsgi_journey.__wrapped__(
        tmp_path
    )
    index = 0

    def extract(r):
        nonlocal index
        index += 1
        source = r.experience.event_id
        if index <= 2:
            return proposal(source, f"m{index}", f"op-m{index}")
        if index == 3:
            assert {m.item_id for m in r.private_memories} == {"m1", "m2"}
            return proposal(source, "m3", "op-m3", parents=(("m1", 1), ("m2", 1)))
        return NoChangeProposal(source, "No update", (source,))

    caps = CognitionCapabilities(generic_memory_write=True, private_consolidation=True)
    web = PrivateWebApplication(
        auth,
        Stage0ConversationRuntime(
            events,
            gate,
            fake,
            clock=clock,
            memory_extractor=SyntheticMemoryExtractor(
                extract, ExtractorIdentity("synthetic", "v1")
            ),
            cognition_capabilities=caps,
        ),
        config,
    )
    a.app = b.app = web
    for name in ("one", "two", "three"):
        submitted(a, name, "Синтетическое частное наблюдение")
    pa, pb = trusted(auth, a), trusted(auth, b)
    assert {r.view.item_id for r in GenericMemoryStore(events).current(pa)} == {
        "m1",
        "m2",
        "m3",
    }
    assert GenericMemoryStore(events).current(pb) == ()

    reopened = EventStore(events.database_path)
    auth2 = AuthStore(auth._database_path, clock=clock)
    gate2 = PrivacyReadGate(ledger, reopened)
    assert gate2.reconcile()
    web2 = PrivateWebApplication(
        auth2,
        Stage0ConversationRuntime(
            reopened,
            gate2,
            fake,
            clock=clock,
            memory_extractor=SyntheticMemoryExtractor(
                lambda r: NoChangeProposal(
                    r.experience.event_id, "No update", (r.experience.event_id,)
                ),
                ExtractorIdentity("synthetic", "v1"),
            ),
            cognition_capabilities=caps,
        ),
        config,
    )
    a.app = b.app = web2
    submitted(a, "later-a", "Что помнишь?")
    assert "Synthetic memory m3" in {
        item["text"]
        for item in json.loads(fake.calls[-1].request.input_text)["memories"]
    }
    submitted(b, "later-b", "Что помнишь?")
    assert json.loads(fake.calls[-1].request.input_text)["memories"] == []


def populated_graph(path):
    store = EventStore(path)
    a, b = principal("a"), principal("b")
    for name, who in (("one", a), ("two", a), ("three", a), ("four", a), ("other", b)):
        append_event(store, who, name)
    for name in ("one", "two"):
        assert (
            engine(store, proposal("event-" + name, "m-" + name, "op-" + name))
            .process(a, "event-" + name)
            .status
            is CognitionOutcomeStatus.PERSISTED
        )
    assert (
        engine(
            store,
            proposal(
                "event-three",
                "m-three",
                "op-three",
                parents=(("m-one", 1), ("m-two", 1)),
            ),
        )
        .process(a, "event-three")
        .status
        is CognitionOutcomeStatus.PERSISTED
    )
    assert (
        engine(store, proposal("event-four", "m-one", "op-one-rev", expected=1))
        .process(a, "event-four")
        .status
        is CognitionOutcomeStatus.PERSISTED
    )
    assert (
        engine(store, proposal("event-other", "m-other", "op-other"))
        .process(b, "event-other")
        .status
        is CognitionOutcomeStatus.PERSISTED
    )
    return store


def rehash(db, operation):
    sources = db.execute(
        "SELECT event_id,role FROM generic_memory_sources WHERE operation_id=?",
        (operation,),
    ).fetchall()
    parents = db.execute(
        "SELECT parent_operation_id FROM generic_memory_parents WHERE operation_id=?",
        (operation,),
    ).fetchall()
    digest = hashlib.sha256(
        json.dumps(
            [sorted(sources), sorted(p[0] for p in parents)],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    db.execute(
        "UPDATE generic_memory_operations SET graph_digest=?,source_count=?,parent_count=? WHERE operation_id=?",
        (digest, len(sources), len(parents), operation),
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "foreign_source",
        "wrong_retention",
        "foreign_event",
        "forged_payload_audience",
        "missing_taint",
        "missing_event_payload",
        "missing_parent",
        "missing_source",
        "missing_payload",
        "wrong_head",
        "mixed_status",
        "wrong_expected",
        "malformed_binding",
        "changed_binding",
        "missing_union",
        "foreign_parent",
        "cycle",
        "inactive_parent",
    ),
)
def test_corrupt_generic_graph_blocks_erasure_and_reads(tmp_path, mutation):
    store = populated_graph(tmp_path / "events.db")
    with sqlite3.connect(store.database_path) as db:
        if mutation == "foreign_source":
            db.execute(
                "UPDATE generic_memory_sources SET source_principal_id='participant-b' WHERE operation_id='op-one'"
            )
        elif mutation == "wrong_retention":
            db.execute(
                "UPDATE generic_memory_sources SET retention_status='invalidated' WHERE operation_id='op-one'"
            )
        elif mutation == "foreign_event":
            db.execute(
                "UPDATE generic_memory_sources SET event_id='event-other' WHERE operation_id='op-one' AND role='initiating'"
            )
        elif mutation == "forged_payload_audience":
            db.execute(
                "UPDATE private_payloads SET audience_principal_id='participant-b' WHERE payload_id='payload-one'"
            )
        elif mutation == "missing_taint":
            db.execute("DELETE FROM protected_event_taints WHERE event_id='event-one'")
        elif mutation == "missing_event_payload":
            db.execute(
                "UPDATE event_envelopes SET payload_id=NULL WHERE event_id='event-one'"
            )
        elif mutation == "missing_parent":
            db.execute(
                "INSERT INTO generic_memory_parents VALUES ('op-three','missing')"
            )
        elif mutation == "missing_source":
            db.execute(
                "DELETE FROM generic_memory_sources WHERE operation_id='op-one' AND role='initiating'"
            )
        elif mutation == "missing_payload":
            db.execute(
                "DELETE FROM generic_memory_payloads WHERE operation_id='op-one'"
            )
        elif mutation == "wrong_head":
            db.execute(
                "UPDATE generic_memory_items SET current_revision=9 WHERE item_id='m-one'"
            )
        elif mutation == "mixed_status":
            db.execute(
                "UPDATE generic_memory_operations SET status='invalidated' WHERE operation_id='op-one'"
            )
            db.execute(
                "DELETE FROM generic_memory_payloads WHERE operation_id='op-one'"
            )
            db.execute(
                "UPDATE generic_memory_sources SET retention_status='invalidated' WHERE operation_id='op-one'"
            )
        elif mutation == "wrong_expected":
            db.execute(
                "UPDATE generic_memory_operations SET expected_revision=9 WHERE operation_id='op-one-rev'"
            )
        elif mutation == "malformed_binding":
            db.execute(
                "UPDATE generic_memory_payloads SET binding='{}' WHERE operation_id='op-one'"
            )
        elif mutation == "changed_binding":
            db.execute(
                "UPDATE generic_memory_payloads SET binding=json_set(binding,'$.statement','forged') WHERE operation_id='op-one'"
            )
        elif mutation == "missing_union":
            db.execute(
                "DELETE FROM generic_memory_sources WHERE operation_id='op-three' AND event_id='event-one' AND role='initiating'"
            )
            rehash(db, "op-three")
        elif mutation == "foreign_parent":
            db.execute(
                "UPDATE generic_memory_parents SET parent_operation_id='op-other' WHERE operation_id='op-three' AND parent_operation_id='op-two'"
            )
            rehash(db, "op-three")
        elif mutation == "cycle":
            db.execute(
                "INSERT INTO generic_memory_parents VALUES ('op-one','op-three')"
            )
            rehash(db, "op-one")
        else:
            db.execute(
                "UPDATE generic_memory_operations SET status='invalidated' WHERE item_id='m-one'"
            )
            db.execute(
                "DELETE FROM generic_memory_payloads WHERE operation_id IN ('op-one','op-one-rev')"
            )
            db.execute(
                "UPDATE generic_memory_sources SET retention_status='invalidated' WHERE operation_id IN ('op-one','op-one-rev')"
            )
    with sqlite3.connect(store.database_path) as db:
        with pytest.raises(ValueError, match="invalid_generic_graph"):
            validate_generic_graph(db)
    report = EventStoreErasureAdapter(store).erase(
        request(content_reference="event-one")
    )
    assert report.blockers
    with pytest.raises(GenericMemoryError):
        GenericMemoryStore(store).current(principal("a"))


def commit_direct(store, value, who=None):
    return GenericMemoryStore(store).commit(
        who or principal("a"),
        value,
        model_identifier="synthetic",
        prompt_version="v1",
        core_self_version="core-self-v1",
        decided_at=NOW,
    )


def test_generic_operation_identity_staleness_and_private_ownership(tmp_path):
    store = EventStore(tmp_path / "events.db")
    a, b = principal("a"), principal("b")
    append_event(store, a, "one")
    append_event(store, a, "two")
    append_event(store, b, "other")
    first = proposal("event-one", "m1", "op-one")
    assert commit_direct(store, first) == ("m1", 1)
    generic = GenericMemoryStore(store)
    assert generic.operation_exists(a, "op-one")
    assert not generic.operation_exists(b, "op-one")
    with pytest.raises(GenericMemoryError):
        generic.operation_exists(a, "bad operation id")
    with pytest.raises(GenericMemoryConflict):
        commit_direct(store, replace(first, statement="Changed"))
    with pytest.raises(GenericMemoryConflict):
        commit_direct(store, proposal("event-other", "m1", "op-other"), b)
    with pytest.raises(GenericMemoryStale):
        commit_direct(store, proposal("event-two", "new", "op-new", expected=1))
    with pytest.raises(GenericMemoryStale):
        commit_direct(store, proposal("event-two", "m1", "op-stale"))
    revision = proposal("event-two", "m1", "op-revise", expected=1)
    assert commit_direct(store, revision) == ("m1", 2)
    assert commit_direct(store, first) == ("m1", 1)
    with pytest.raises(GenericMemoryStale):
        commit_direct(
            store, proposal("event-two", "m2", "op-bad-parent", parents=(("m1", 1),))
        )
    with pytest.raises(GenericMemoryError):
        commit_direct(store, proposal("missing", "m2", "op-missing"))
    assert (
        EventStoreErasureAdapter(store)
        .erase(request(content_reference="event-one"))
        .blockers
        == ()
    )
    with pytest.raises(GenericMemoryConflict):
        commit_direct(store, first)


@pytest.mark.parametrize(
    "change",
    (
        lambda p: None,
        lambda p: replace(p, statement=""),
        lambda p: replace(p, statement="\ud800"),
        lambda p: replace(p, item_id="bad item id"),
        lambda p: replace(p, parent_revisions=(("m1", 1), ("m1", 1))),
        lambda p: replace(p, parent_revisions=(("m1", 1),)),
        lambda p: replace(p, parent_revisions=(("bad item id", 1),)),
        lambda p: replace(
            p,
            context=replace(
                p.context, supporting_event_ids=tuple(f"event-{i}" for i in range(100))
            ),
        ),
    ),
)
def test_generic_store_rejects_malformed_proposals(tmp_path, change):
    store = EventStore(tmp_path / "events.db")
    append_event(store, principal("a"), "one")
    value = proposal("event-one", "m1", "op-one")
    with pytest.raises(GenericMemoryError):
        commit_direct(store, change(value))
    assert GenericMemoryStore(store).current(principal("a")) == ()


def test_generic_sql_failure_rolls_back(tmp_path, monkeypatch):
    import app.cognition.generic_store as module

    store = EventStore(tmp_path / "events.db")
    append_event(store, principal("a"), "one")

    def failure(_db):
        raise sqlite3.OperationalError("synthetic database failure")

    monkeypatch.setattr(module, "validate_generic_graph", failure)
    with pytest.raises(GenericMemoryError, match="generic_memory_store_failure"):
        commit_direct(store, proposal("event-one", "m1", "op-one"))
    with sqlite3.connect(store.database_path) as db:
        assert db.execute("SELECT * FROM generic_memory_items").fetchall() == []


def test_inherited_dependency_bound_rejects_whole_consolidation(tmp_path):
    store = EventStore(tmp_path / "events.db")
    a = principal("a")
    for name in (
        "one",
        "two",
        "three",
        *(f"a-{i}" for i in range(49)),
        *(f"b-{i}" for i in range(49)),
    ):
        append_event(store, a, name)
    for label in ("a", "b"):
        source = "event-one" if label == "a" else "event-two"
        value = proposal(source, "m-" + label, "op-" + label)
        extras = tuple(f"event-{label}-{i}" for i in range(49))
        value = replace(
            value,
            context=replace(value.context, supporting_event_ids=(source, *extras)),
        )
        assert commit_direct(store, value) == ("m-" + label, 1)
    child = proposal(
        "event-three", "m-child", "op-child", parents=(("m-a", 1), ("m-b", 1))
    )
    with pytest.raises(GenericMemoryError, match="generic_source_bound"):
        commit_direct(store, child)
    assert (
        engine(store, child).process(principal("a"), "event-three").reason.value
        == "store_rejected"
    )
    assert {read.view.item_id for read in GenericMemoryStore(store).current(a)} == {
        "m-a",
        "m-b",
    }


def test_orphaned_generic_operation_is_detected_before_release(tmp_path):
    store = EventStore(tmp_path / "events.db")
    with sqlite3.connect(store.database_path) as db:
        db.execute(
            """INSERT INTO generic_memory_operations VALUES
            ('forged','missing',1,NULL,1,0,'active',?, 'synthetic','v1','core',?)""",
            (NOW.isoformat(), "0" * 64),
        )
    with sqlite3.connect(store.database_path) as db:
        with pytest.raises(ValueError, match="invalid_generic_graph"):
            validate_generic_graph(db)


def test_generic_authorization_rejects_hidden_stale_and_disabled_parents(
    tmp_path, monkeypatch
):
    store = EventStore(tmp_path / "events.db")
    a, b = principal("a"), principal("b")
    for name, who in (("one", a), ("two", a), ("three", a), ("other", b)):
        append_event(store, who, name)
    assert (
        engine(store, proposal("event-one", "m1", "op-one"))
        .process(a, "event-one")
        .status
        is CognitionOutcomeStatus.PERSISTED
    )
    assert (
        engine(store, proposal("event-other", "mb", "op-other"))
        .process(b, "event-other")
        .status
        is CognitionOutcomeStatus.PERSISTED
    )
    child = proposal("event-two", "m2", "op-two", parents=(("m1", 1),))
    disabled = CognitionEngine(
        store,
        SelfStateStore(store),
        extractor=SyntheticMemoryExtractor(
            lambda _r: child, ExtractorIdentity("synthetic", "v1")
        ),
        capabilities=CognitionCapabilities(generic_memory_write=True),
        clock=lambda: NOW,
    )
    assert disabled.process(a, "event-two").reason.value == "capability_disabled"
    bad_id = replace(child, item_id="bad item")
    assert (
        engine(store, bad_id).process(a, "event-two").reason.value == "invalid_proposal"
    )
    missing = replace(child, parent_revisions=(("m1", 99),))
    assert (
        engine(store, missing).process(a, "event-two").reason.value == "stale_revision"
    )
    foreign = replace(child, parent_revisions=(("mb", 1),))
    assert (
        engine(store, foreign).process(a, "event-two").reason.value == "stale_revision"
    )

    # The extractor receives m1 at revision 1, then a competing writer wins
    # after the snapshot recheck and before the child's transaction.
    original = GenericMemoryStore.commit
    competing = proposal("event-three", "m1", "op-revise", expected=1)

    def race(self, context, value, **kwargs):
        original(self, context, competing, **kwargs)
        return original(self, context, value, **kwargs)

    monkeypatch.setattr(GenericMemoryStore, "commit", race)
    assert engine(store, child).process(a, "event-two").reason.value == "stale_revision"


def test_generic_snapshot_change_during_inference_and_retrieval_hint(tmp_path):
    store = EventStore(tmp_path / "events.db")
    a = principal("a")
    append_event(store, a, "one")
    append_event(store, a, "two")
    assert (
        engine(store, proposal("event-one", "m1", "op-one"))
        .process(a, "event-one")
        .status
        is CognitionOutcomeStatus.PERSISTED
    )
    selection = MemoryContextSelector(store, SelfStateStore(store)).select(
        a,
        RetrievalRequest(
            RetrievalPurpose.CONVERSATION,
            "memory",
            32000,
            16,
            invalidated_event_ids=("event-one",),
        ),
    )
    assert selection.records == () and selection.records_omitted

    def write_during_extract(_request):
        commit_direct(store, proposal("event-two", "m2", "op-two"))
        return proposal("event-two", "m3", "op-three")

    worker = CognitionEngine(
        store,
        SelfStateStore(store),
        extractor=SyntheticMemoryExtractor(
            write_during_extract, ExtractorIdentity("synthetic", "v1")
        ),
        capabilities=CognitionCapabilities(generic_memory_write=True),
        clock=lambda: NOW,
    )
    assert worker.process(a, "event-two").reason.value == "stale_revision"
    assert {read.view.item_id for read in GenericMemoryStore(store).current(a)} == {
        "m1",
        "m2",
    }


def test_runtime_requires_exact_trusted_cognition_configuration(tmp_path):
    store, _ledger, gate, fake, _ = runtime_setup.__wrapped__(tmp_path)
    with pytest.raises(TypeError, match="trusted cognition capabilities"):
        Stage0ConversationRuntime(store, gate, fake, cognition_capabilities=True)


@pytest.mark.parametrize(
    "kind",
    (
        MemoryKind.UNRESOLVED_QUESTION,
        MemoryKind.CANDIDATE_USEFUL_MEMORY,
        MemoryKind.CONTRADICTION_COUNTEREVIDENCE,
    ),
)
def test_every_supported_generic_kind_is_durable(tmp_path, kind):
    store = EventStore(tmp_path / "events.db")
    a = principal("a")
    append_event(store, a, "one")
    value = replace(proposal("event-one", "m1", "op-one"), memory_kind=kind)
    assert (
        engine(store, value).process(a, "event-one").status
        is CognitionOutcomeStatus.PERSISTED
    )
    reopened = EventStore(store.database_path)
    assert GenericMemoryStore(reopened).current(a)[0].view.memory_kind is kind


def test_review_state_cannot_be_upgraded_and_revision_history_is_bounded(tmp_path):
    store = EventStore(tmp_path / "events.db")
    a = principal("a")
    append_event(store, a, "first")
    value = proposal("event-first", "m1", "op-first")
    assert value.review_state is GenericReviewState.PENDING_REVIEW
    assert commit_direct(store, value) == ("m1", 1)
    assert (
        engine(store, replace(value, review_state="reviewed"))
        .process(a, "event-first")
        .reason.value
        == "store_rejected"
    )
    for revision in range(2, 33):
        name = f"rev-{revision}"
        append_event(store, a, name)
        assert commit_direct(
            store, proposal(f"event-{name}", "m1", f"op-{name}", expected=revision - 1)
        ) == ("m1", revision)
    append_event(store, a, "overflow")
    with pytest.raises(GenericMemoryError, match="generic_revision_bound"):
        commit_direct(
            store, proposal("event-overflow", "m1", "op-overflow", expected=32)
        )
    assert GenericMemoryStore(store).current(a)[0].view.revision == 32


def test_genuine_v6_migration_rolls_back_atomically_then_preserves_rows(tmp_path):
    path = tmp_path / "events.db"
    store = EventStore(path)
    append_event(store, principal("a"), "one")
    with sqlite3.connect(path) as db:
        remove_v7(db)
        db.execute("UPDATE schema_metadata SET version=6")
        db.execute("PRAGMA user_version=6")
        before = schema_objects(db)
        input_rows = db.execute("SELECT * FROM event_envelopes").fetchall()
    assert _schema_version(path) == 6

    def interrupt(point):
        if point is FaultPoint.BEFORE_V7_MIGRATION_COMMIT:
            raise RuntimeError("synthetic migration interruption")

    with pytest.raises(RuntimeError, match="synthetic migration interruption"):
        EventStore(path, fault_injector=interrupt)
    with sqlite3.connect(path) as db:
        assert schema_objects(db) == before
        assert db.execute("PRAGMA user_version").fetchone() == (6,)
        assert db.execute("SELECT * FROM event_envelopes").fetchall() == input_rows
    upgraded = EventStore(path)
    assert upgraded.diagnostics().schema_version == 7
    assert GenericMemoryStore(upgraded).current(principal("a")) == ()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM event_envelopes").fetchall() == input_rows


def consolidation_exit_child(path, phase):
    import os
    import app.cognition.generic_store as module
    from acceptance.guards import install

    guard = pytest.MonkeyPatch()
    install(guard)
    store = EventStore(path)
    if phase == "inside_transaction":
        original = module.validate_generic_graph
        calls = 0

        def exit_before_commit(db):
            nonlocal calls
            calls += 1
            if calls == 2:
                os._exit(23)
            return original(db)

        module.validate_generic_graph = exit_before_commit
    commit_direct(
        store, proposal("event-three", "m3", "op-three", parents=(("m1", 1), ("m2", 1)))
    )
    if phase == "after_commit":
        os._exit(23)
    raise AssertionError("process exit checkpoint not reached")


@pytest.mark.parametrize(
    "phase,persisted",
    (
        ("inside_transaction", False),
        ("after_commit", True),
    ),
)
def test_real_process_exit_at_generic_commit_boundary(tmp_path, phase, persisted):
    store = EventStore(tmp_path / "events.db")
    a = principal("a")
    for name in ("one", "two", "three"):
        append_event(store, a, name)
    for name, item in (("one", "m1"), ("two", "m2")):
        assert commit_direct(store, proposal("event-" + name, item, "op-" + name)) == (
            item,
            1,
        )
    code = (
        "import sys; sys.path.insert(0,'tests'); "
        "from test_generic_memory import consolidation_exit_child; "
        "consolidation_exit_child(sys.argv[1],sys.argv[2])"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(store.database_path), phase],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 23, result.stderr.decode()
    reopened = EventStore(store.database_path)
    assert (
        "m3" in {r.view.item_id for r in GenericMemoryStore(reopened).current(a)}
    ) is persisted
    assert commit_direct(
        reopened,
        proposal("event-three", "m3", "op-three", parents=(("m1", 1), ("m2", 1))),
    ) == ("m3", 1)
    with sqlite3.connect(store.database_path) as db:
        assert db.execute(
            "SELECT count(*) FROM generic_memory_operations WHERE operation_id='op-three'"
        ).fetchone() == (1,)


def test_later_parent_revision_deletion_invalidates_earlier_descendant_reply(tmp_path):
    store, _ledger, gate, fake, _ = runtime_setup.__wrapped__(tmp_path)
    proposals = []

    def extract(r):
        source = r.experience.event_id
        index = len(proposals)
        if index == 0:
            value = proposal(source, "m1", "op-one")
        elif index == 1:
            value = proposal(source, "m2", "op-two")
        elif index == 2:
            value = proposal(source, "m3", "op-three", parents=(("m1", 1), ("m2", 1)))
        else:
            value = NoChangeProposal(source, "No update", (source,))
        proposals.append(value)
        return value

    runtime = Stage0ConversationRuntime(
        store,
        gate,
        fake,
        memory_extractor=SyntheticMemoryExtractor(
            extract, ExtractorIdentity("synthetic", "v1")
        ),
        cognition_capabilities=CognitionCapabilities(
            generic_memory_write=True, private_consolidation=True
        ),
    )
    for name in ("one", "two", "three", "later"):
        runtime.submit(principal("a"), name, "Синтетическое наблюдение")
    last = gate.retrieve_private_events(principal("a"))[-1].event_id
    reply = gate.read_reply(principal("a"), runtime._operation_id(last))
    assert "op-three" in reply.operation.generic_operation_ids
    runtime.submit(principal("b"), "unrelated", "Независимый запрос")
    b_event = gate.retrieve_private_events(principal("b"))[-1].event_id
    b_reply = gate.read_reply(principal("b"), runtime._operation_id(b_event))
    append_event(store, principal("a"), "four", "New revision evidence")
    assert (
        engine(store, proposal("event-four", "m1", "op-revise", expected=1))
        .process(principal("a"), "event-four")
        .status
        is CognitionOutcomeStatus.PERSISTED
    )
    assert "event-four" not in reply.operation.source_event_ids
    assert (
        EventStoreErasureAdapter(store)
        .erase(request(content_reference="event-four"))
        .blockers
        == ()
    )
    assert gate.read_reply(principal("a"), runtime._operation_id(last)) is None
    assert gate.read_reply(principal("b"), runtime._operation_id(b_event)) == b_reply
    assert [
        read.view.item_id for read in GenericMemoryStore(store).current(principal("a"))
    ] == ["m2"]


def generic_reply(source_ids, generic_ids):
    return ReplyOperation(
        "reply-generic",
        "event-three",
        "chat-a",
        "correlation-three",
        "synthetic",
        "v1",
        "core",
        tuple(sorted(source_ids)),
        generic_operation_ids=tuple(sorted(generic_ids)),
    )


def test_reply_requires_complete_exact_generic_dependencies(tmp_path):
    store = populated_graph(tmp_path / "events.db")
    outputs = AgentOutputStore(store)
    sources = ("event-one", "event-two", "event-three")
    with pytest.raises(EventValidationError):
        outputs.reserve_reply(
            principal("a"), generic_reply(("event-three",), ("op-three",))
        )
    with pytest.raises(EventValidationError):
        outputs.reserve_reply(principal("a"), generic_reply(sources, ("op-other",)))
    with pytest.raises(EventValidationError):
        outputs.reserve_reply(principal("a"), generic_reply(sources, ("missing",)))
    with pytest.raises(EventValidationError):
        outputs.reserve_reply(
            principal("a"), generic_reply(sources, ("op-three", "op-three"))
        )
    saved = outputs.reserve_reply(principal("a"), generic_reply(sources, ("op-three",)))
    assert saved.operation.generic_operation_ids == ("op-three",)
    assert (
        AgentOutputStore(EventStore(store.database_path)).read_reply(
            principal("a"), "reply-generic"
        )
        == saved
    )


def test_generic_combined_budget_omits_whole_memory_and_its_edges(tmp_path):
    store = EventStore(tmp_path / "events.db")
    a = principal("a")
    append_event(store, a, "one")
    append_event(store, a, "two")
    assert commit_direct(store, proposal("event-one", "m1", "op-one")) == ("m1", 1)
    with store._open() as db:
        db.execute("BEGIN IMMEDIATE")
        full, sources = MemoryConversationAssembler(
            store, ContextLimits(32000, 99)
        ).build(a, "event-two", db)
        assert full.generic_operation_ids == ("op-one",)
        assert "event-one" in sources
        current_only = replace(
            full,
            memories=(),
            generic_operation_ids=(),
            conversation=replace(
                full.conversation, conversation=full.conversation.conversation[-1:]
            ),
        )
        budget = len(current_only.serialize().encode()) + 100
        limited, protected = MemoryConversationAssembler(
            store, ContextLimits(budget, 1)
        ).build(a, "event-two", db)
        assert limited.memories == ()
        assert limited.memories_omitted
        assert limited.generic_operation_ids == ()
        assert protected == ("event-two",)


@pytest.mark.parametrize("mutation", ("missing_link", "foreign_link", "missing_source"))
def test_forged_reply_generic_graph_blocks_release(tmp_path, mutation):
    store = populated_graph(tmp_path / "events.db")
    sources = ("event-one", "event-two", "event-three")
    AgentOutputStore(store).reserve_reply(
        principal("a"), generic_reply(sources, ("op-three",))
    )
    with sqlite3.connect(store.database_path) as db:
        if mutation == "missing_link":
            db.execute(
                "DELETE FROM protected_reply_generic WHERE operation_id='reply-generic'"
            )
        elif mutation == "foreign_link":
            db.execute(
                "UPDATE protected_reply_generic SET generic_operation_id='op-other' WHERE operation_id='reply-generic'"
            )
        else:
            db.execute(
                "DELETE FROM agent_reply_sources WHERE operation_id='reply-generic' AND event_id='event-one'"
            )
            db.execute(
                "UPDATE agent_reply_operations SET source_count=2 WHERE operation_id='reply-generic'"
            )
    with sqlite3.connect(store.database_path) as db:
        with pytest.raises(ValueError, match="invalid_generic_graph"):
            validate_generic_graph(db)
    if mutation == "missing_link":
        assert (
            AgentOutputStore(store).read_reply(principal("a"), "reply-generic") is None
        )
    assert (
        EventStoreErasureAdapter(store)
        .erase(request(content_reference="event-two"))
        .blockers
    )
