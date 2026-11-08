"""A known local budget denial waits; uncertain inference is never replayed."""

from datetime import timedelta

import pytest

from app.core.public_rejection import PublicOutputRejected
from app.integration.public_window import PublicOperatingWindow
from app.threads.domain import InboundKind
from test_b11c_window import setup, spec, serve, finish, NO_ACTION
from test_int24_runtime import setup as direct_setup


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


def test_known_monthly_denial_stops_repeat_model_contacts_but_preserves_polling(
    tmp_path, monkeypatch
):
    public, worker, sdk, http, clock = setup(tmp_path, NO_ACTION)
    contacts = []
    original = public.model.complete

    def counted(*args):
        contacts.append(True)
        return original(*args)

    def denied(**_kwargs):
        raise PublicOutputRejected(
            "public_monthly_budget_exhausted", "synthetic local refusal"
        )

    monkeypatch.setattr(public.model, "complete", counted)
    monkeypatch.setattr(sdk, "create", denied)
    thread = serve(worker, 1)
    try:
        result = PublicOperatingWindow(
            public, spec(max_model_calls=3, max_poll_pages=1)
        ).run(wait=lambda seconds: clock.advance(timedelta(seconds=seconds)))
        finish(thread)
        assert result["state"] == "expired"
        assert len(contacts) == 1
        assert len(http.calls) == 1
        assert result["consumed"]["model_calls"] == 1
        assert result["consumed"]["inbound_events"] == 0
        assert result["wakeup_outcomes"]["budget_paused"] >= 1
        assert public.service.store.pending_inbound_events(
            public.account, InboundKind.REPLY, now=clock.now()
        )
    finally:
        worker.close()


def test_known_pre_sdk_denied_comment_can_get_one_fresh_authorized_attempt_next_month(
    tmp_path, monkeypatch
):
    public, worker, sdk, _, clock = direct_setup(tmp_path, NO_ACTION)
    worker.utc_now = clock.now
    public.poll(InboundKind.REPLY, correlation_id="comment")
    original = sdk.create

    def denied(**_kwargs):
        raise PublicOutputRejected(
            "public_monthly_budget_exhausted", "synthetic local refusal"
        )

    monkeypatch.setattr(sdk, "create", denied)
    thread = serve(worker, 1)
    try:
        assert public.process_inbound(InboundKind.REPLY) == ()
        finish(thread)
        with public.journal.transaction() as db:
            original_operation = db.execute("SELECT operation FROM intents").fetchone()[
                0
            ]
        assert public.process_inbound(InboundKind.REPLY) == ()
        later = clock.now().replace(day=1) + timedelta(days=32)
        later = later.replace(day=1)
        clock.advance(later - clock.now())
        monkeypatch.setattr(sdk, "create", original)
        thread = serve(worker, 1)
        completed = public.process_inbound(InboundKind.REPLY)
        assert len(completed) == 1
        assert completed[0] != original_operation
        finish(thread)
        assert public.journal.read(original_operation)["phase"] == "started"
        assert public.journal.read(completed[0])["decision"] == "no_action"
        assert public.process_inbound(InboundKind.REPLY) == ()
        assert len(sdk.calls) == 1
    finally:
        worker.close()


@pytest.fixture
def denied_comment(tmp_path, monkeypatch):
    public, worker, sdk, _, clock = direct_setup(tmp_path, NO_ACTION)
    worker.utc_now = clock.now
    public.poll(InboundKind.REPLY, correlation_id="known-denial")

    def denied(**_kwargs):
        raise PublicOutputRejected(
            "public_monthly_budget_exhausted", "synthetic local refusal"
        )

    monkeypatch.setattr(sdk, "create", denied)
    thread = serve(worker, 1)
    assert public.process_inbound(InboundKind.REPLY) == ()
    finish(thread)
    with public.journal.transaction() as db:
        operation, phase, raw = db.execute(
            "SELECT operation,phase,payload FROM intents"
        ).fetchone()
    import json

    origin = json.loads(raw)
    later = (clock.now().replace(day=1) + timedelta(days=32)).replace(day=1)
    clock.advance(later - clock.now())
    try:
        yield public, worker, sdk, clock, operation, phase, origin
    finally:
        worker.close()


def test_new_budget_policy_unpauses_only_attested_old_pre_sdk_denial(denied_comment):
    from app.integration.public_budget_pause import paused, retryable, CONFIG

    public, _, _, clock, operation, phase, origin = denied_comment
    with public.provider._event_store._open() as db:
        db.execute(
            "UPDATE provider_usage_attempts SET provider_config_version=?,started_at=? WHERE attempt_id=?",
            ("threads-public-worker-v1", clock.now().isoformat(), operation),
        )
    assert CONFIG != "threads-public-worker-v1"
    assert not paused(public)
    assert retryable(public, operation, phase, origin)
    with public.provider._event_store._open() as db:
        db.execute(
            "UPDATE provider_usage_attempts SET provider_response_id='resp_synthetic' WHERE attempt_id=?",
            (operation,),
        )
    assert not retryable(public, operation, phase, origin)


def test_policy_upgrade_retry_passes_the_actual_credential_worker_attestor(
    tmp_path, monkeypatch
):
    from app.ops.int24_public_worker import _attestor

    public, worker, sdk, _, clock = setup(tmp_path, NO_ACTION)
    public.poll(InboundKind.REPLY, correlation_id="policy-upgrade")
    window = PublicOperatingWindow(public, spec(max_poll_pages=0))
    window._initialize()
    public.operating_window = window
    worker.attest = _attestor(public.account)
    original = sdk.create

    def denied(**kwargs):
        raise PublicOutputRejected(
            "public_monthly_budget_exhausted", "synthetic pre-SDK denial"
        )

    monkeypatch.setattr(sdk, "create", denied)
    try:
        thread = serve(worker, 1)
        assert public.process_inbound(InboundKind.REPLY) == ()
        finish(thread)
        with public.provider._event_store._open() as db:
            db.execute(
                "UPDATE provider_usage_attempts SET provider_config_version=? WHERE provider_error_code=?",
                ("threads-public-worker-v1", "public_monthly_budget_exhausted"),
            )
        monkeypatch.setattr(sdk, "create", original)
        thread = serve(worker, 1)
        completed = public.process_inbound(InboundKind.REPLY)
        finish(thread)
        assert len(completed) == 1
        assert public.journal.read(completed[0])["decision"] == "no_action"
        assert len(sdk.calls) == 1
        assert public.process_inbound(InboundKind.REPLY) == ()
    finally:
        worker.close()


@pytest.mark.parametrize(
    "case",
    [
        "unknown_error",
        "provider_response",
        "token_usage",
        "foreign_configuration",
        "foreign_runtime",
        "finished_phase",
        "same_month",
        "outbox",
        "research",
        "intellectual",
        "revision",
    ],
)
def test_comment_is_never_retried_without_complete_no_effect_evidence(
    denied_comment, monkeypatch, case
):
    from app.integration.public_budget_pause import retryable
    from app.threads.domain import PublicationType, SourceProvenance

    public, _, _, clock, operation, phase, origin = denied_comment
    if case in {
        "unknown_error",
        "provider_response",
        "token_usage",
        "foreign_configuration",
    }:
        column, value = {
            "unknown_error": ("provider_error_code", "unclassified_error"),
            "provider_response": ("provider_response_id", "resp_synthetic"),
            "token_usage": ("input_tokens", 0),
            "foreign_configuration": ("provider_config_version", "foreign-worker-v1"),
        }[case]
        with public.provider._event_store._open() as db:
            db.execute(
                f"UPDATE provider_usage_attempts SET {column}=? WHERE attempt_id=?",
                (value, operation),
            )
    elif case == "foreign_runtime":
        origin["runtime_version"] = "foreign"
        with public.journal.transaction() as db:
            db.execute(
                "UPDATE intents SET payload=json_set(payload,'$.runtime_version','foreign') WHERE operation=?",
                (operation,),
            )
    elif case == "finished_phase":
        phase = "rejected"
        with public.journal.transaction() as db:
            db.execute(
                "UPDATE intents SET phase='rejected' WHERE operation=?", (operation,)
            )
    elif case == "same_month":
        with public.provider._event_store._open() as db:
            db.execute(
                "UPDATE provider_usage_attempts SET started_at=? WHERE attempt_id=?",
                (clock.now().isoformat(), operation),
            )
    elif case == "outbox":
        public.service.propose(
            operation_id=operation,
            publication_type=PublicationType.POST,
            parent_message_id=None,
            draft_id="synthetic-existing-draft",
            content="Существующий черновик.",
            source_provenance=SourceProvenance.PUBLIC,
            privacy_authorized=True,
            russian_output_authorized=True,
            correlation_id=operation,
            now=clock.now(),
        )
    elif case == "research":
        with public.journal.transaction() as db:
            db.execute(
                "INSERT INTO public_research_ops VALUES(?,?,?,?,?,NULL)",
                (operation, "research-synthetic", "topic", "0" * 64, "started"),
            )
    elif case == "intellectual":
        with public.journal.transaction() as db:
            db.execute(
                "UPDATE agent_intellectual_operations SET status='no_change' WHERE operation=?",
                (operation,),
            )
    else:
        with public.journal.transaction() as db:
            db.execute(
                "INSERT INTO agent_intellectual_revisions VALUES(?,?,?,?,?,?,?)",
                (
                    "public",
                    public.account,
                    "synthetic-revision",
                    1,
                    operation,
                    "{}",
                    "{}",
                ),
            )
    assert not retryable(public, operation, phase, origin)

    def forbidden(*_args):
        raise AssertionError("unexpected model contact for a held event")

    monkeypatch.setattr(public.model, "complete", forbidden)
    assert public.process_inbound(InboundKind.REPLY) == ()


def test_budget_observation_rejects_naive_and_regressed_clocks(denied_comment):
    from app.integration.public_budget_pause import _month, paused

    public, _, _, clock, *_ = denied_comment
    with pytest.raises(ValueError, match="aware"):
        _month(clock.now().replace(tzinfo=None))
    with public.provider._event_store._open() as db:
        db.execute(
            "UPDATE provider_usage_attempts SET started_at=?",
            ((clock.now() + timedelta(days=62)).isoformat(),),
        )
    with pytest.raises(ValueError, match="regressed"):
        paused(public)


def test_retry_claims_remain_scoped_to_stream_and_resume_mode(denied_comment):
    from app.integration.public_budget_pause import claims

    public, *_ = denied_comment
    assert claims(public, "mention", False) == []
    assert claims(public, "reply", True) == []
    assert len(claims(public, "reply", False)) == 1


@pytest.mark.parametrize("control", ["stop", "withdraw"])
def test_a_fresh_retry_still_requires_live_owner_and_event_controls(
    denied_comment, monkeypatch, control
):
    from app.threads.service import PolicyDeniedError

    public, _, _, _, _, _, origin = denied_comment

    def forbidden(*_args):
        raise AssertionError("unexpected model contact after control withdrawal")

    monkeypatch.setattr(public.model, "complete", forbidden)
    if control == "stop":
        with public.owner._connection() as db:
            db.execute("UPDATE owner_stop SET active=1")
        with pytest.raises(PolicyDeniedError):
            public.process_inbound(InboundKind.REPLY)
    else:
        with public.service.store._open() as db:
            db.execute(
                "DELETE FROM threads_inbound_events WHERE account_ref=? AND event_id=?",
                (public.account, origin["event"]),
            )
        assert public.process_inbound(InboundKind.REPLY) == ()


def test_month_boundary_uses_utc():
    from datetime import datetime, timezone
    from app.integration.public_budget_pause import _month

    assert (
        _month(datetime(2030, 3, 1, 0, 30, tzinfo=timezone(timedelta(hours=1))))
        == "2030-02"
    )


def test_inbound_uses_the_configured_execution_allowance_for_context_and_model(
    tmp_path,
):
    from dataclasses import replace

    public, worker, sdk, _, clock = setup(tmp_path, NO_ACTION)
    public.scheduler.policy = replace(public.scheduler.policy, max_execution_seconds=30)
    public.poll(InboundKind.REPLY, correlation_id="inbound-deadline")

    def slow_context(point):
        if point == "after_started":
            clock.advance(timedelta(seconds=6))

    public.checkpoint = slow_context
    window = PublicOperatingWindow(
        public, spec(max_model_calls=1, max_poll_pages=0, max_inbound_events=1)
    )
    window._initialize()
    public.operating_window = window
    thread = serve(worker, 1)
    try:
        operations = public.process_inbound(InboundKind.REPLY)
        assert len(operations) == 1
        finish(thread)
        assert len(sdk.calls) == 1
        assert public.journal.read(operations[0])["timeout"] == 30
    finally:
        public.operating_window = None
        worker.close()


def test_first_budget_denial_stops_the_remaining_inbound_batch(tmp_path, monkeypatch):
    public, worker, sdk, http, clock = setup(tmp_path, NO_ACTION)
    http.reply_rows = [
        {
            "id": f"reply-{index}",
            "text": f"Synthetic independent comment {index}",
            "timestamp": clock.now().isoformat(),
            "root_post": {"id": "root-1"},
            "replied_to": {"id": "root-1"},
            "is_reply_owned_by_me": False,
        }
        for index in (1, 2)
    ]
    public.poll(InboundKind.REPLY, correlation_id="two-pending-comments")
    contacts = []
    original = public.model.complete

    def counted(*args):
        contacts.append(True)
        if len(contacts) > 1:
            raise ValueError("unexpected repeated worker contact")
        return original(*args)

    def denied(**_kwargs):
        raise PublicOutputRejected("public_monthly_budget_exhausted", "local denial")

    monkeypatch.setattr(public.model, "complete", counted)
    monkeypatch.setattr(sdk, "create", denied)
    thread = serve(worker, 1)
    try:
        assert public.process_inbound(InboundKind.REPLY) == ()
        finish(thread)
        assert len(contacts) == 1
        assert (
            len(
                public.service.store.pending_inbound_events(
                    public.account, InboundKind.REPLY, now=clock.now()
                )
            )
            == 2
        )
        with public.journal.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 1
    finally:
        worker.close()
