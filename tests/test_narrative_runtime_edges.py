"""Fail-closed transitions around the optional narrative provider operation."""

from datetime import UTC, datetime
from types import SimpleNamespace
from threading import Event, Thread

import pytest

from app.cognition.narrative_store import NarrativeConflict
from app.cognition.worker_narrative import NarrativeProposal
from app.core.openai_worker_client import (
    CLIENT_COMMUNICATION_FAILURE,
    CLIENT_OUTCOME_UNCERTAIN,
    OpenAIWorkerClient,
)
from app.core.provider_spend import AttemptReservationStatus, ProviderStateStore
from app.core.agent_output_store import ResultState
from app.core.store import EventStore
from app.private_web.narrative_audit import NarrativeAudit
from app.private_web.runtime import WorkerConversationRuntime
from app.privacy.application import PrivacyReadBlocked, PrivacyReadGate
from app.privacy.ledger import PrivacyLedger
from test_cognition_pipeline import principal


def _caps():
    return SimpleNamespace(
        emergency_stop_active=False,
        provider_spend_suspended=False,
        private_reply_drafts_enabled=True,
    )


def _runtime_double(case):
    state = {"authorized": 0, "current": 0, "worker": 0, "finished": [], "commits": []}
    proposal = (
        NarrativeProposal("create", None, "Новая сцена.")
        if case == "full_create"
        else NarrativeProposal("revise", "h0123456789abcdef", "Новая сцена.")
        if case == "unshown_revision"
        else NarrativeProposal("no_change", None, None)
    )
    audit = NarrativeAudit(
        "reserved",
        sources=("source",),
        request=SimpleNamespace(create_allowed=case != "full_create"),
    )

    def authorize(fn):
        state["authorized"] += 1
        if (
            (case == "admission_lost" and state["authorized"] == 1)
            or (case == "preflight_lost" and state["authorized"] == 2)
            or (case == "receipt_lost" and state["authorized"] == 3)
        ):
            raise PrivacyReadBlocked("authority_changed")
        return fn()

    def reserve(*_args):
        if case == "no_receipt":
            return None
        if case in {"received", "terminal"}:
            return NarrativeAudit(
                "received" if case == "received" else "unavailable",
                sources=("source",),
                proposal=proposal,
            )
        return audit

    def current(*_args):
        state["current"] += 1
        return not (
            (case == "preflight_changed" and state["current"] == 1)
            or (case == "receipt_changed" and state["current"] == 2)
        )

    def infer(*_args, **_kwargs):
        state["worker"] += 1
        if case == "malformed_worker_result":
            raise ValueError("malformed worker result")
        outcome = {
            "unknown": CLIENT_OUTCOME_UNCERTAIN,
            "transport_failed": CLIENT_COMMUNICATION_FAILURE,
            "rejected": "invalid_response",
        }.get(case, "completed")
        return SimpleNamespace(
            outcome=outcome, narrative=proposal if outcome == "completed" else None
        )

    fake = SimpleNamespace(
        _capabilities=_caps,
        _current_request=SimpleNamespace(get=lambda: lambda: True),
        _narrative_permission=lambda: case != "permission_lost",
        _narrative_audit=SimpleNamespace(
            reserve=reserve,
            receive=lambda *_args: None,
            finish=lambda operation, status: state["finished"].append(
                (operation, status)
            ),
        ),
        _gate=SimpleNamespace(authorized=authorize),
        _provider=SimpleNamespace(
            reserve_attempt=lambda *_args: (
                AttemptReservationStatus.DUPLICATE
                if case == "attempt_duplicate"
                else AttemptReservationStatus.CREATED
            )
        ),
        _worker=SimpleNamespace(infer=infer),
        _narrative_current=current,
        _clock=lambda: datetime(2026, 10, 20, tzinfo=UTC),
        _checkpoint=lambda *_args: None,
        _commit_narrative=lambda *args: state["commits"].append(args),
    )
    return fake, state


@pytest.mark.parametrize(
    "case,expected_status,worker_calls,commits",
    [
        ("permission_lost", None, 0, 0),
        ("admission_lost", None, 0, 0),
        ("no_receipt", None, 0, 0),
        ("received", None, 0, 0),
        ("terminal", None, 0, 0),
        ("attempt_duplicate", "unavailable", 0, 0),
        ("preflight_lost", "unavailable", 0, 0),
        ("preflight_changed", "unavailable", 0, 0),
        ("malformed_worker_result", "rejected", 1, 0),
        ("unknown", "uncertain", 1, 0),
        ("transport_failed", "unavailable", 1, 0),
        ("rejected", "rejected", 1, 0),
        ("full_create", "rejected", 1, 0),
        ("unshown_revision", "rejected", 1, 0),
        ("receipt_lost", "unavailable", 1, 0),
        ("receipt_changed", "unavailable", 1, 0),
        ("completed", None, 1, 0),
    ],
)
def test_narrative_operation_stops_at_each_authority_boundary(
    case, expected_status, worker_calls, commits
):
    fake, state = _runtime_double(case)
    WorkerConversationRuntime._evolve_narrative_once(
        fake,
        principal("a"),
        "source",
        "Расскажи историю",
        "correlation",
        "reply:source",
        SimpleNamespace(operation_id="reply:source"),
        object(),
    )
    assert state["worker"] == worker_calls
    assert len(state["commits"]) == commits
    assert state["finished"] == (
        [] if expected_status is None else [("narrative:source", expected_status)]
    )


@pytest.mark.parametrize(
    "case,expected",
    [
        ("reply_missing", "unavailable"),
        ("authority_changed", "unavailable"),
        ("unknown_revision", "unavailable"),
        ("stale_write", "unavailable"),
    ],
)
def test_narrative_receipt_commit_requires_current_reply_and_revision(case, expected):
    state = {"finished": [], "writes": 0}
    operation = SimpleNamespace(
        operation_id="reply:source",
        narrative_operation_ids=("narrative:old",),
        source_event_ids=("source",),
    )
    reply = SimpleNamespace(operation=operation, result_state=ResultState.PERSISTED)
    proposal = NarrativeProposal(
        "revise" if case == "unknown_revision" else "create",
        "h0123456789abcdef" if case == "unknown_revision" else None,
        "Я видел маяк.",
    )
    audit = NarrativeAudit(
        "received",
        selected=(("h0123456789abcdef", "narrative:old"),),
        sources=("source",),
        proposal=proposal,
    )

    def write(*_args, **_kwargs):
        state["writes"] += 1
        if case == "stale_write":
            raise NarrativeConflict("stale revision")

    fake = SimpleNamespace(
        _outputs=SimpleNamespace(
            read_reply=lambda *_args: None if case == "reply_missing" else reply,
            attach_narrative=lambda *_args: None,
        ),
        _narrative_current=lambda *_args, **_kwargs: case != "authority_changed",
        _narrative_store=SimpleNamespace(
            current=lambda *_args: (),
            committed=lambda *_args, **_kwargs: False,
        ),
        _write_narrative=write,
        _gate=SimpleNamespace(authorized=lambda fn: fn()),
        _clock=lambda: datetime(2026, 10, 20, tzinfo=UTC),
        _checkpoint=lambda *_args: None,
        _narrative_audit=SimpleNamespace(
            finish=lambda operation_id, status: state["finished"].append(
                (operation_id, status)
            )
        ),
    )
    WorkerConversationRuntime._commit_narrative(
        fake, principal("a"), "source", "narrative:source", audit
    )
    assert state["finished"] == [("narrative:source", expected)]
    assert state["writes"] == (1 if case == "stale_write" else 0)


def test_narrative_reconciliation_ignores_conflicting_receipt_and_commits_received(
    tmp_path,
):
    state = []
    fake = SimpleNamespace(
        _research_evidence=lambda _operation: None,
        _event_store=SimpleNamespace(database_path=tmp_path / "events.db"),
        _narrative_audit=SimpleNamespace(
            read=lambda *_args: (_ for _ in ()).throw(ValueError("conflict"))
        ),
        _commit_narrative=lambda *args: state.append(args),
    )
    fake._reconcile_narrative_once = lambda *args: (
        WorkerConversationRuntime._reconcile_narrative_once(fake, *args)
    )
    WorkerConversationRuntime._reconcile_narrative(
        fake, principal("a"), "source", "История", "reply:source"
    )
    assert state == []
    fake._narrative_audit.read = lambda *_args: NarrativeAudit("received")
    fake._gate = SimpleNamespace(authorized=lambda fn: fn())
    fake._outputs = SimpleNamespace(
        read_reply=lambda *_args: SimpleNamespace(result_state=ResultState.PERSISTED)
    )
    WorkerConversationRuntime._reconcile_narrative(
        fake, principal("a"), "source", "История", "reply:source"
    )
    assert len(state) == 1


def test_received_narrative_waits_for_existing_reply_attempt(tmp_path):
    finished = []
    fake = SimpleNamespace(
        _research_evidence=lambda _operation: None,
        _event_store=SimpleNamespace(database_path=tmp_path / "events.db"),
        _narrative_audit=SimpleNamespace(
            read=lambda *_args: NarrativeAudit("received"),
            finish=lambda *_args: finished.append(True),
        ),
        _gate=SimpleNamespace(authorized=lambda fn: fn()),
        _outputs=SimpleNamespace(
            read_reply=lambda *_args: SimpleNamespace(result_state=ResultState.PENDING)
        ),
        _provider=SimpleNamespace(attempt_exists=lambda *_args: True),
    )
    fake._reconcile_narrative_once = lambda *args: (
        WorkerConversationRuntime._reconcile_narrative_once(fake, *args)
    )
    WorkerConversationRuntime._reconcile_narrative(
        fake, principal("a"), "source", "История", "reply:source"
    )
    assert finished == []


def test_narrative_projection_refuses_unshown_revision_handle():
    audit = NarrativeAudit(
        "received",
        selected=(("h0123456789abcdef", "narrative:unshown"),),
        proposal=NarrativeProposal("revise", "h0123456789abcdef", "Новая сцена."),
    )
    context = SimpleNamespace(episodes=(), projected_narrative_operation_ids=())
    with pytest.raises(ValueError, match="projection unavailable"):
        WorkerConversationRuntime._project_narrative(context, audit, 32_000)


def test_active_narrative_operation_is_not_entered_twice(tmp_path):
    entered, release = Event(), Event()
    calls = []

    def once(*_args):
        calls.append("entered")
        entered.set()
        assert release.wait(5)

    fake = SimpleNamespace(
        _event_store=SimpleNamespace(database_path=tmp_path / "events.db"),
        _evolve_narrative_once=once,
    )
    args = (
        principal("a"),
        "source",
        "История",
        "corr",
        "reply:source",
        object(),
        object(),
    )
    worker = Thread(
        target=lambda: WorkerConversationRuntime._evolve_narrative(fake, *args)
    )
    worker.start()
    try:
        assert entered.wait(5)
        assert WorkerConversationRuntime._evolve_narrative(fake, *args) is None
    finally:
        release.set()
        worker.join(10)
    assert calls == ["entered"]


@pytest.mark.parametrize(
    "mode,expected",
    [
        ({"narrative_enabled": "yes"}, TypeError),
        ({"narrative_enabled": True}, TypeError),
        (
            {
                "narrative_enabled": True,
                "narrative_permission_current": lambda: True,
                "shared_proposals_enabled": True,
                "shared_intelligence": object(),
            },
            ValueError,
        ),
        (
            {
                "narrative_enabled": True,
                "narrative_permission_current": lambda: True,
                "shared_intelligence": object(),
            },
            ValueError,
        ),
    ],
)
def test_narrative_mode_rejects_implicit_combination(tmp_path, mode, expected):
    events = EventStore(tmp_path / "events.db")
    gate = PrivacyReadGate(PrivacyLedger(tmp_path / "ledger.db"), events)
    client = OpenAIWorkerClient(tmp_path / "missing.sock", expected_worker_uid=0)
    provider = ProviderStateStore(EventStore(tmp_path / "ipc.db"))
    with pytest.raises(expected):
        WorkerConversationRuntime(events, gate, client, provider_state=provider, **mode)


def test_private_cli_requires_both_narrative_authorities(tmp_path):
    from app.private_web.cli import main

    with pytest.raises(SystemExit, match="narrative mode requires"):
        main(
            [
                "serve",
                "--auth-db",
                str(tmp_path / "auth.db"),
                "--event-db",
                str(tmp_path / "events.db"),
                "--worker-socket",
                str(tmp_path / "worker.sock"),
                "--allowed-host",
                "localhost",
                "--allowed-origin",
                "http://127.0.0.1",
                "--insecure-http",
                "--enable-narrative",
            ]
        )


@pytest.mark.parametrize("case", ["missing_reply", "source_changed"])
def test_narrative_current_rejects_missing_reply_or_source_binding(case):
    operation = SimpleNamespace(
        operation_id="reply:source",
        source_event_ids=("source",),
        narrative_operation_ids=(),
    )
    fake = SimpleNamespace(
        _intellectual=None,
        _capabilities=_caps,
        _current_request=SimpleNamespace(get=lambda: lambda: True),
        _narrative_permission=lambda: True,
        _inference_consent_live=lambda *_args: True,
        _outputs=SimpleNamespace(
            read_reply=lambda *_args: None
            if case == "missing_reply"
            else SimpleNamespace(operation=operation)
        ),
    )
    audit = NarrativeAudit(
        "received", sources=("other",) if case == "source_changed" else ("source",)
    )
    assert not WorkerConversationRuntime._narrative_current(
        fake, principal("a"), "source", operation, audit
    )


def test_narrative_reply_commit_rechecks_permission():
    outcomes = []
    fake = SimpleNamespace(
        _intellectual=None,
        _capabilities=_caps,
        _worker=object(),
        _shared_consent_live=lambda *_args: True,
        _narrative_audit=object(),
        _narrative_permission=lambda: False,
        _outputs=SimpleNamespace(
            resolve_without_reply=lambda _principal,
            _operation,
            status: outcomes.append(status)
        ),
    )
    WorkerConversationRuntime._commit(
        fake,
        SimpleNamespace(principal=principal("a")),
        SimpleNamespace(operation_id="reply:source"),
        SimpleNamespace(outcome="completed"),
    )
    assert outcomes == ["unavailable"]
