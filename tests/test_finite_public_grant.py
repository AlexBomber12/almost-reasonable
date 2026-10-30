"""Offline real public runtime, finite grant authority and actual fake HTTP calls."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from app.integration.public_grant import PublicGrantSpec
from app.integration.public_window import PublicOperatingWindow
from app.threads.domain import OutboxState
from app.threads.service import PolicyDeniedError
from test_b11c_window import setup, spec, serve, finish
from test_int24_runtime import NOW

POST = {
    "decision": "propose_publication",
    "draft": "Проверяю основания своего суждения.",
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


def grant(window, **changes):
    return replace(
        PublicGrantSpec(
            grant_id="finite-public-grant-1",
            account_ref="account-1",
            window_id=window.window_id,
            expires_at=window.expires_at,
            max_publications=2,
            max_http_requests=10,
            publication_types=("post", "reply"),
        ),
        **changes,
    )


def run_window(app, worker, clock, window, calls):
    thread = serve(worker, calls)
    result = PublicOperatingWindow(app, window).run(
        wait=lambda seconds: clock.advance(timedelta(seconds=seconds))
    )
    finish(thread)
    return result


def test_finite_grant_runs_publication_without_per_draft_human_approval(tmp_path):
    app, worker, sdk, http, clock = setup(
        tmp_path, POST, autonomous_publication_enabled=True
    )
    window = spec(
        publication_grant="finite-public-grant-1",
        max_model_calls=1,
        max_poll_pages=0,
        max_inbound_events=0,
    )
    try:
        app.public_grants.issue(grant(window), window)
        result = run_window(app, worker, clock, window, 1)
        assert result["state"] == "exhausted"
        assert len(sdk.calls) == 1
        assert [method for method, _, _ in http.calls] == [
            "GET",
            "POST",
            "GET",
            "POST",
            "GET",
        ]
        with app.journal.transaction() as db:
            actions = [row[0] for row in db.execute("SELECT action FROM public_audit")]
        assert "approve" not in actions
        assert "grant_issue" in actions and "grant_admit" in actions
        operation = next(iter(PublicOperatingWindow(app, window).operations()))
        assert app.service.store.get(operation).state is OutboxState.CONFIRMED
        usage = app.public_grants.summary(window.publication_grant)
        assert (
            usage["publications_reserved"] == 1 and usage["http_requests_reserved"] == 5
        )
        assert "fake-token" not in json.dumps(usage)
    finally:
        worker.close()


def test_public_grants_default_to_disabled_and_do_not_issue_from_model_text(tmp_path):
    app, worker, _, http, _ = setup(tmp_path, POST)
    window = spec(publication_grant="finite-public-grant-1")
    try:
        with pytest.raises(PolicyDeniedError, match="disabled"):
            app.public_grants.issue(grant(window), window)
        assert http.calls == []
    finally:
        worker.close()


@pytest.mark.parametrize(
    "change",
    [
        {"max_publications": True},
        {"max_publications": 0},
        {"max_http_requests": 0},
        {"max_http_requests": 1000000},
        {"publication_types": ()},
        {"publication_types": ("delete",)},
        {"publication_types": ("post", "post")},
        {"expires_at": NOW.replace(tzinfo=None)},
    ],
)
def test_grant_shape_rejects_unbounded_or_ambiguous_authority(change):
    with pytest.raises(ValueError):
        grant(spec(), **change)


def test_grant_identity_cannot_be_reissued_to_reset_consumption(tmp_path):
    app, worker, _, _, _ = setup(tmp_path, POST, autonomous_publication_enabled=True)
    window = spec(publication_grant="finite-public-grant-1")
    try:
        authority = grant(window)
        app.public_grants.issue(authority, window)
        for replacement in (authority, replace(authority, max_publications=3)):
            with pytest.raises(ValueError, match="issued"):
                app.public_grants.issue(replacement, window)
        app.public_grants.revoke(authority.grant_id)
        assert app.public_grants.summary(authority.grant_id)["revoked"] is True
    finally:
        worker.close()


def varied_posts(sdk):
    original = sdk.create

    def create(**kwargs):
        sdk.proposal = {
            "decision": "propose_publication",
            "draft": (
                "Исследую неопределённость своих объяснений."
                if not sdk.calls
                else "Пересматриваю основания прежнего суждения."
            ),
        }
        return original(**kwargs)

    sdk.create = create


def test_exhaustion_never_replenishes_the_publication_allowance(tmp_path):
    app, worker, sdk, http, clock = setup(
        tmp_path, POST, autonomous_publication_enabled=True
    )
    window = spec(
        publication_grant="finite-public-grant-1",
        max_model_calls=2,
        max_poll_pages=0,
        max_inbound_events=0,
    )
    varied_posts(sdk)
    try:
        app.public_grants.issue(grant(window, max_publications=1), window)
        result = run_window(app, worker, clock, window, 2)
        assert result["state"] == "controls_denied"
        assert len(sdk.calls) == 2 and len(http.calls) == 5
        assert (
            app.public_grants.summary(window.publication_grant)["publications_reserved"]
            == 1
        )
        again = PublicOperatingWindow(app, window).run(wait=lambda _: None)
        assert again["state"] == "controls_denied"
        assert len(sdk.calls) == 2 and len(http.calls) == 5
    finally:
        worker.close()


@pytest.mark.parametrize("budget", [1, 2, 3, 4])
def test_every_actual_http_request_is_reserved_before_send_and_never_retried(
    tmp_path, budget
):
    app, worker, _, http, clock = setup(
        tmp_path, POST, autonomous_publication_enabled=True
    )
    window = spec(
        publication_grant="finite-public-grant-1",
        max_model_calls=1,
        max_poll_pages=0,
        max_inbound_events=0,
    )
    try:
        app.public_grants.issue(grant(window, max_http_requests=budget), window)
        run_window(app, worker, clock, window, 1)
        assert len(http.calls) == budget
        usage = app.public_grants.summary(window.publication_grant)
        assert usage["http_requests_reserved"] == budget
        operation = PublicOperatingWindow(app, window).operations()[0]
        item = app.service.store.get(operation)
        assert item.state is (OutboxState.UNKNOWN if budget == 4 else OutboxState.READY)
        PublicOperatingWindow(app, window).run(wait=lambda _: None)
        assert len(http.calls) == budget
        with pytest.raises(PolicyDeniedError):
            app.dispatch(operation)
        assert len(http.calls) == budget
    finally:
        worker.close()


@pytest.mark.parametrize(
    "boundary", ["before_dispatch", "after_account", "after_container"]
)
def test_grant_revocation_is_live_at_each_actual_http_boundary(tmp_path, boundary):
    app, worker, sdk, http, clock = setup(
        tmp_path, POST, autonomous_publication_enabled=True
    )
    window = spec(
        publication_grant="finite-public-grant-1",
        max_model_calls=1,
        max_poll_pages=0,
        max_inbound_events=0,
    )
    try:
        app.public_grants.issue(grant(window), window)
        if boundary == "before_dispatch":
            create = sdk.create

            def revoke_after_inference(**kwargs):
                result = create(**kwargs)
                app.public_grants.revoke(window.publication_grant)
                return result

            sdk.create = revoke_after_inference
        else:
            request = http.request

            def revoke_after_request(*args):
                response = request(*args)
                if len(http.calls) == (1 if boundary == "after_account" else 2):
                    app.public_grants.revoke(window.publication_grant)
                return response

            http.request = revoke_after_request
        run_window(app, worker, clock, window, 1)
        assert (
            len(http.calls)
            == {"before_dispatch": 0, "after_account": 1, "after_container": 2}[
                boundary
            ]
        )
        assert not any("threads_publish" in url for _, url, _ in http.calls)
        assert app.public_grants.summary(window.publication_grant)["revoked"] is True
    finally:
        worker.close()


def test_unknown_publication_pauses_the_account_before_another_model_call(tmp_path):
    app, worker, sdk, http, clock = setup(
        tmp_path, POST, autonomous_publication_enabled=True
    )
    window = spec(
        publication_grant="finite-public-grant-1",
        max_model_calls=2,
        max_poll_pages=0,
        max_inbound_events=0,
    )
    http.fail_readback = True
    try:
        app.public_grants.issue(grant(window), window)
        result = run_window(app, worker, clock, window, 1)
        assert result["state"] == "controls_denied"
        assert len(sdk.calls) == 1 and len(http.calls) == 5
        assert len(app.service.store.unresolved_for_account(app.account)) == 1
        PublicOperatingWindow(app, window).run(wait=lambda _: None)
        assert len(sdk.calls) == 1 and len(http.calls) == 5
    finally:
        worker.close()


@pytest.mark.parametrize(
    "change",
    [
        {"account_ref": "other-account"},
        {"window_id": "other-window"},
        {"expires_at": NOW},
        {"expires_at": NOW + timedelta(days=1)},
    ],
)
def test_issuance_requires_the_exact_account_window_and_future_finite_expiry(
    tmp_path, change
):
    app, worker, _, http, _ = setup(tmp_path, POST, autonomous_publication_enabled=True)
    window = spec(publication_grant="finite-public-grant-1")
    try:
        with pytest.raises(ValueError, match="finite reviewed window"):
            app.public_grants.issue(grant(window, **change), window)
        assert http.calls == []
    finally:
        worker.close()


def test_unissued_grant_fails_closed_without_model_or_provider_calls(tmp_path):
    app, worker, sdk, http, _ = setup(
        tmp_path, POST, autonomous_publication_enabled=True
    )
    try:
        result = PublicOperatingWindow(
            app, spec(publication_grant="missing-grant")
        ).run()
        assert result["state"] == "controls_denied"
        assert sdk.calls == http.calls == []
    finally:
        worker.close()


def test_default_window_preserves_supervised_drafts_even_when_grants_are_enabled(
    tmp_path,
):
    app, worker, _, http, clock = setup(
        tmp_path, POST, autonomous_publication_enabled=True
    )
    window = spec(max_model_calls=1, max_poll_pages=0, max_inbound_events=0)
    try:
        result = run_window(app, worker, clock, window, 1)
        assert len(result["pending_drafts"]) == 1
        assert http.calls == []
        assert window.maximum_work()["publication_posts"] == 0
        assert "publication_grant" not in json.loads(window.encoded())
    finally:
        worker.close()


@pytest.fixture
def pending_public(tmp_path):
    from test_int24_runtime import run_once

    app, worker, sdk, http, clock = setup(
        tmp_path, POST, autonomous_publication_enabled=True
    )
    window = spec(
        publication_grant="finite-public-grant-1",
        max_poll_pages=0,
        max_inbound_events=0,
    )
    authority = grant(window)
    app.public_grants.issue(authority, window)
    runner = PublicOperatingWindow(app, window)
    runner._initialize()
    app.operating_window = runner
    app.public_grants.activate_window(runner)
    try:
        run_once(app, worker)
        operation = app.wakeup_operation("wake-1")
        item = app.service.store.get(operation)
        assert item.state is OutboxState.AWAITING_APPROVAL
        yield app, worker, sdk, http, clock, runner, authority, item
    finally:
        app.operating_window = None
        worker.close()


@pytest.mark.parametrize(
    "change",
    [
        "content",
        "operation",
        "account",
        "reference",
        "missing_approval",
        "revoked",
        "purpose",
        "missing_admission",
        "finished",
        "disabled",
    ],
)
def test_recorded_grant_admission_cannot_be_rebound_or_used_after_revocation(
    pending_public, change
):
    app, _, _, http, _, _, authority, item = pending_public
    approval = app.public_grants._admit(authority.grant_id, item)
    assert approval is not None
    with app.journal.transaction() as db:
        if change == "content":
            item = replace(item, content="Другой публичный текст.")
        elif change == "operation":
            item = replace(item, operation_id="different-operation")
        elif change == "account":
            item = replace(item, account_ref="different-account")
        elif change == "reference":
            approval = replace(approval, reference="different-reference")
        elif change == "missing_approval":
            db.execute("DELETE FROM approvals WHERE reference=?", (approval.reference,))
        elif change == "revoked":
            db.execute(
                "UPDATE approvals SET revoked=1 WHERE reference=?",
                (approval.reference,),
            )
        elif change == "purpose":
            db.execute(
                "UPDATE approvals SET purpose='publication' WHERE reference=?",
                (approval.reference,),
            )
        elif change == "missing_admission":
            db.execute(
                "DELETE FROM public_grant_admissions WHERE operation=?",
                (item.operation_id,),
            )
        elif change == "finished":
            db.execute(
                "UPDATE public_grant_admissions SET phase='finished' WHERE operation=?",
                (item.operation_id,),
            )
        else:
            app.autonomous_publication_enabled = False
        assert app.public_grants.current_in(db, item, approval.reference) is None
    assert http.calls == []


def test_reserved_admission_is_idempotent_without_replenishing_allowance(
    pending_public,
):
    app, _, _, http, _, _, authority, item = pending_public
    one = app.public_grants._admit(authority.grant_id, item)
    two = app.public_grants._admit(authority.grant_id, item)
    assert one == two
    assert app.public_grants.summary(authority.grant_id)["publications_reserved"] == 1
    assert http.calls == []


def test_existing_human_approval_does_not_consume_or_inherit_grant_authority(
    pending_public,
):
    app, _, _, http, clock, _, authority, item = pending_public
    approved = app.approve(
        item.operation_id, expires_at=clock.now() + timedelta(seconds=30)
    )
    item = app.service.store.get(item.operation_id)
    assert app.public_grants._admit(authority.grant_id, item) is None
    assert app.public_grants.summary(authority.grant_id)["publications_reserved"] == 0
    assert item.approval_reference == approved.approval_reference
    assert http.calls == []


@pytest.mark.parametrize("scope", ["origin", "kind", "replacement", "capacity"])
def test_grant_admission_checks_scope_and_capacity_before_reserving(
    pending_public, monkeypatch, scope
):
    import app.integration.public_grant as module

    app, _, _, http, _, _, authority, item = pending_public
    if scope == "replacement":
        item = replace(item, replacement_of="some-unknown-publication")
    elif scope == "origin":
        with app.journal.transaction() as db:
            db.execute(
                "DELETE FROM public_window_work WHERE operation=?", (item.operation_id,)
            )
    elif scope == "kind":
        with app.journal.transaction() as db:
            value = json.loads(
                db.execute(
                    "SELECT spec FROM public_grants WHERE id=?", (authority.grant_id,)
                ).fetchone()[0]
            )
            value["publication_types"] = ["reply"]
            db.execute(
                "UPDATE public_grants SET spec=? WHERE id=?",
                (json.dumps(value), authority.grant_id),
            )
    else:
        monkeypatch.setattr(module, "MAX_INTENTS", 0)
    with pytest.raises(PolicyDeniedError):
        app.public_grants._admit(authority.grant_id, item)
    assert app.public_grants.summary(authority.grant_id)["publications_reserved"] == 0
    assert http.calls == []


def test_control_replacement_permanently_invalidates_observed_grant(pending_public):
    app, _, _, http, _, runner, authority, _ = pending_public
    before = app.policy.scopes
    app.policy.scopes = before | {"different_scope"}
    with pytest.raises(PolicyDeniedError):
        app.public_grants.activate_window(runner)
    app.policy.scopes = before
    with pytest.raises(PolicyDeniedError):
        app.public_grants.activate_window(runner)
    assert app.public_grants.summary(authority.grant_id)["revoked"] is True
    assert http.calls == []


def test_expiry_observation_and_clock_watermark_survive_restart(pending_public):
    from app.integration.public_grant import PublicPublicationGrants

    app, _, _, http, clock, runner, authority, _ = pending_public
    clock.advance(timedelta(minutes=2))
    with pytest.raises(PolicyDeniedError):
        app.public_grants.activate_window(runner)
    clock.advance(-timedelta(minutes=2))
    app.public_grants = PublicPublicationGrants(app)
    with pytest.raises(PolicyDeniedError):
        app.public_grants.activate_window(runner)
    assert (
        app.public_grants.summary(authority.grant_id)["last_seen"]
        > authority.expires_at.isoformat()
    )
    assert http.calls == []


def test_grant_operator_commands_require_explicit_activation_and_review(
    monkeypatch, capsys
):
    from types import SimpleNamespace
    from app.ops import int24_threads_runtime as module

    window = spec(publication_grant="finite-public-grant-1")
    authority = grant(window)
    config = {
        "activation_reviewed": True,
        "operating_window_reviewed": True,
        "public_mission_path": "/protected/mission.json",
        "account_ref": "account-1",
        "enable_autonomous_publication": True,
        "autonomous_publication_reviewed": True,
        "operating_window": json.loads(window.encoded()),
        "public_grant": json.loads(authority.encoded()),
    }
    calls = []
    runtime = SimpleNamespace(
        public_grants=SimpleNamespace(
            issue=lambda g, w: calls.append(("issue", g, w)) or {"issued": g.grant_id},
            revoke=lambda g: calls.append(("revoke", g)),
            summary=lambda g: {"grant_id": g},
        ),
        operator=SimpleNamespace(authenticate=lambda: "owner"),
    )
    monkeypatch.setattr(module, "_config", lambda _: config)
    monkeypatch.setattr(module, "_runtime", lambda _: runtime)
    with pytest.raises(SystemExit):
        module.main(["issue-grant", "--config", "/protected/config"])
    assert calls == []
    assert module._grant_spec(config) == authority
    with pytest.raises(ValueError, match="reviewed"):
        module._grant_spec(config | {"autonomous_publication_reviewed": False})
    assert module.main(["window-plan", "--config", "/protected/config"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert (
        plan["active"] is False
        and plan["maximum_work"]["publication_http_requests"] == 10
    )
    assert calls == []
    assert (
        module.main(["issue-grant", "--config", "/protected/config", "--activate-live"])
        == 0
    )
    assert calls == [("issue", authority, window)]
    assert (
        module.main(
            [
                "grant-status",
                authority.grant_id,
                "--config",
                "/protected/config",
                "--activate-live",
            ]
        )
        == 0
    )
    assert (
        module.main(
            [
                "revoke-grant",
                authority.grant_id,
                "--config",
                "/protected/config",
                "--activate-live",
            ]
        )
        == 0
    )
    assert calls[-1] == ("revoke", authority.grant_id)


def test_crash_after_possible_container_send_never_allows_autonomous_takeover(
    pending_public,
):
    from app.integration.public_grant import PublicPublicationGrants

    app, _, _, http, _, runner, authority, item = pending_public
    request = http.request

    def crash_after_send(*args):
        result = request(*args)
        if args[0] == "POST":
            raise KeyboardInterrupt
        return result

    http.request = crash_after_send
    with pytest.raises(KeyboardInterrupt):
        app.public_grants.dispatch(authority.grant_id, item.operation_id)
    assert app.service.store.get(item.operation_id).state is OutboxState.SENDING
    assert len(http.calls) == 2
    app.public_grants = PublicPublicationGrants(app)
    with pytest.raises(PolicyDeniedError, match="paused"):
        app.public_grants.drain(runner)
    assert app.public_grants.dispatch(authority.grant_id, item.operation_id) is None
    assert len(http.calls) == 2


def test_http_admission_requires_the_durable_attempt_boundary(pending_public):
    app, _, _, http, _, _, authority, item = pending_public
    approval = app.public_grants._admit(authority.grant_id, item)
    with pytest.raises(PolicyDeniedError, match="dispatch admission unavailable"):
        app.public_grants._reserve_http(authority.grant_id, item, approval)
    assert app.public_grants.summary(authority.grant_id)["http_requests_reserved"] == 0
    assert http.calls == []


def test_concurrent_dispatch_consumes_one_slot_and_at_most_one_http_sequence(
    pending_public, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock

    app, _, _, http, _, _, authority, item = pending_public
    barrier, lock = Barrier(2), Lock()
    authorize = app.service.authorize

    def synchronize_authorization(operation, approval, **kwargs):
        with lock:
            current = app.service.store.get(operation)
            if current.state is OutboxState.AWAITING_APPROVAL:
                current = authorize(operation, approval, **kwargs)
        barrier.wait(timeout=5)
        return current

    monkeypatch.setattr(app.service, "authorize", synchronize_authorization)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [
            pool.submit(
                app.public_grants.dispatch, authority.grant_id, item.operation_id
            )
            for _ in range(2)
        ]
        values = [job.result(timeout=10) for job in jobs]
    assert sum(value is not None for value in values) == 1
    assert len(http.calls) == 5
    usage = app.public_grants.summary(authority.grant_id)
    assert usage["publications_reserved"] == 1 and usage["http_requests_reserved"] == 5


def test_unavailable_research_or_mission_invalidates_authority(pending_public):
    app, _, _, http, _, runner, authority, _ = pending_public
    app.research_enabled = True
    with pytest.raises(PolicyDeniedError, match="research authority unavailable"):
        app.public_grants._controls()
    with pytest.raises(PolicyDeniedError):
        app.public_grants.activate_window(runner)
    assert app.public_grants.summary(authority.grant_id)["revoked"]
    app.mission = None
    with pytest.raises(PolicyDeniedError, match="mission unavailable"):
        app.public_grants._controls()
    assert http.calls == []


def test_grant_issue_requires_current_scopes_and_has_finite_journal_capacity(
    tmp_path, monkeypatch
):
    import app.integration.public_grant as module

    app, worker, _, http, _ = setup(tmp_path, POST, autonomous_publication_enabled=True)
    window = spec(publication_grant="finite-public-grant-1")
    authority = grant(window)
    second = replace(authority, grant_id="second-grant")
    try:
        original = app.policy.scopes
        app.policy.scopes = original - {"threads_content_publish"}
        with pytest.raises(PolicyDeniedError, match="scope"):
            app.public_grants.issue(authority, window)
        app.policy.scopes = original
        app.public_grants.issue(authority, window)
        monkeypatch.setattr(module, "MAX_INTENTS", 1)
        with pytest.raises(ValueError, match="capacity"):
            app.public_grants.issue(
                second, replace(window, publication_grant=second.grant_id)
            )
        for method in (app.public_grants.revoke, app.public_grants.summary):
            with pytest.raises(ValueError, match="missing"):
                method("missing-grant")
        assert http.calls == []
    finally:
        worker.close()


def test_no_action_under_grant_consumes_no_publication_or_http_allowance(tmp_path):
    app, worker, _, http, clock = setup(
        tmp_path,
        {"decision": "no_action", "reason_category": "not_warranted"},
        autonomous_publication_enabled=True,
    )
    window = spec(
        publication_grant="finite-public-grant-1",
        max_model_calls=1,
        max_poll_pages=0,
        max_inbound_events=0,
    )
    try:
        app.public_grants.issue(grant(window), window)
        assert run_window(app, worker, clock, window, 1)["state"] == "exhausted"
        usage = app.public_grants.summary(window.publication_grant)
        assert usage["publications_reserved"] == usage["http_requests_reserved"] == 0
        assert http.calls == []
    finally:
        worker.close()


def test_revocation_between_policy_check_and_http_reservation_has_no_effect(
    pending_public, monkeypatch
):
    app, _, _, http, _, _, authority, item = pending_public
    approval = app.public_grants._admit(authority.grant_id, item)
    check = app.service._check_ready_policy

    def revoke_after_policy(*args, **kwargs):
        current = check(*args, **kwargs)
        app.public_grants.revoke(authority.grant_id)
        return current

    monkeypatch.setattr(app.service, "_check_ready_policy", revoke_after_policy)
    with pytest.raises(PolicyDeniedError):
        app.public_grants._reserve_http(authority.grant_id, item, approval)
    assert app.public_grants.summary(authority.grant_id)["http_requests_reserved"] == 0
    assert http.calls == []


def test_transport_budget_context_is_scoped_and_rejects_nesting(pending_public):
    app, _, _, http, _, _, _, _ = pending_public
    transport = app.simulator
    with pytest.raises(ValueError):
        with transport.bounded_requests(None):
            pytest.fail("invalid callback admitted")
    with transport.bounded_requests(lambda: None):
        with pytest.raises(ValueError):
            with transport.bounded_requests(lambda: None):
                pytest.fail("nested authority admitted")
    transport.verify_account()
    assert len(http.calls) == 1


def test_grant_cannot_supply_recovery_or_replacement_authority(pending_public):
    from app.threads.domain import ApprovalPurpose

    app, _, _, http, _, _, authority, item = pending_public
    approval = app.public_grants._admit(authority.grant_id, item)
    with pytest.raises(PolicyDeniedError, match="replacement"):
        app.service._check_ready_policy(
            replace(item, replacement_of="unknown-target"),
            approval,
            now=app.clock.now(),
        )
    with pytest.raises(ValueError, match="purpose"):
        app.service._check_ready_policy(
            item,
            replace(approval, purpose=ApprovalPurpose.RECOVER_RECONCILIATION),
            now=app.clock.now(),
        )
    assert http.calls == []


def test_reserved_grant_admission_survives_actual_application_restart(
    pending_public, tmp_path, monkeypatch
):
    from app.integration.threads_runtime import ThreadsRuntime

    app, worker, sdk, http, clock, runner, authority, item = pending_public
    original = app.public_grants._admit(authority.grant_id, item)
    app.operating_window = None
    restarted = ThreadsRuntime(
        tmp_path / "journal.sqlite",
        tmp_path / "scheduler.sqlite",
        tmp_path / "threads.sqlite",
        model=app.model,
        transport=app.simulator,
        policy=app.policy,
        owner=app.owner,
        provider=app.provider,
        ledger=app.ledger,
        clock=clock,
        wakeup_policy=app.scheduler.policy,
        operator=app.operator,
        mission=app.mission,
        enabled=True,
        live_enabled=True,
        autonomous_publication_enabled=True,
    )
    worker.attest = restarted.attest_worker_request
    resumed = PublicOperatingWindow(restarted, runner.spec)
    resumed._initialize()
    restarted.operating_window = resumed
    try:
        restarted.public_grants.activate_window(resumed)
        monkeypatch.setattr(
            type(app.operator),
            "authenticate",
            lambda _: pytest.fail("per-draft owner approval requested"),
        )
        assert (
            restarted.public_grants._admit(authority.grant_id, item).reference
            == original.reference
        )
        result = restarted.public_grants.dispatch(authority.grant_id, item.operation_id)
        assert result.state is OutboxState.CONFIRMED
        assert len(sdk.calls) == 1 and len(http.calls) == 5
        assert (
            restarted.public_grants.dispatch(authority.grant_id, item.operation_id)
            is None
        )
        assert (
            restarted.public_grants.summary(authority.grant_id)["publications_reserved"]
            == 1
        )
    finally:
        restarted.operating_window = None


@pytest.mark.parametrize("flag", [1, "true"])
def test_autonomous_mode_requires_a_real_boolean(tmp_path, flag):
    with pytest.raises(ValueError, match="autonomous publication mode"):
        setup(tmp_path, POST, autonomous_publication_enabled=flag)


def test_ready_but_not_attempted_admission_resumes_without_new_authority(
    pending_public,
):
    app, _, sdk, http, clock, _, authority, item = pending_public
    approval = app.public_grants._admit(authority.grant_id, item)
    app.service.authorize(item.operation_id, approval, now=clock.now())
    result = app.public_grants.dispatch(authority.grant_id, item.operation_id)
    assert result.state is OutboxState.CONFIRMED
    assert len(sdk.calls) == 1 and len(http.calls) == 5
    assert app.public_grants.summary(authority.grant_id)["publications_reserved"] == 1


def test_owner_stop_between_requests_prevents_the_next_send(pending_public):
    app, _, _, http, _, _, authority, item = pending_public
    request = http.request

    def stop_after_account(*args):
        response = request(*args)
        with app.owner._connection() as db:
            db.execute("UPDATE owner_stop SET active=1")
        return response

    http.request = stop_after_account
    with pytest.raises(PolicyDeniedError):
        app.public_grants.dispatch(authority.grant_id, item.operation_id)
    result = app.service.store.get(item.operation_id)
    assert result.state is OutboxState.READY
    assert app.owner.active()
    assert len(http.calls) == 1 and http.calls[0][0] == "GET"
    assert app.public_grants.summary(authority.grant_id)["http_requests_reserved"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"grant_id": "different-grant"},
        {"window_id": "different-window"},
        {"account_ref": "different-account"},
        {"expires_at": NOW + timedelta(hours=2)},
    ],
)
def test_inactive_planning_rejects_a_grant_bound_to_different_authority(change):
    from app.ops.int24_threads_runtime import _window_plan

    window = spec(publication_grant="finite-public-grant-1")
    value = grant(window, **change)
    config = {
        "account_ref": "account-1",
        "enable_autonomous_publication": True,
        "autonomous_publication_reviewed": True,
        "public_grant": json.loads(value.encoded()),
    }
    with pytest.raises(ValueError, match="binding mismatch"):
        _window_plan(config, window)


def test_grant_composes_with_free_mission_mode_without_requiring_research(
    tmp_path, monkeypatch
):
    from test_free_bounded_mission import install_choices

    app, worker, sdk, http, clock = setup(
        tmp_path,
        POST,
        autonomous_publication_enabled=True,
        mission_evolution_enabled=True,
        free_missions_enabled=True,
    )
    window = spec(
        publication_grant="finite-public-grant-1",
        mission_evolution=True,
        max_model_calls=1,
        max_poll_pages=0,
        max_inbound_events=0,
    )
    chosen = install_choices(sdk, monkeypatch)
    chosen["value"] = None
    try:
        app.public_grants.issue(grant(window), window)
        result = run_window(app, worker, clock, window, 1)
        assert result["state"] == "exhausted"
        assert app.research_client is None
        assert json.loads(sdk.calls[0]["input"][1]["content"])["mission_evolution"] == [
            "free-mission-v1"
        ]
        assert len(http.calls) == 5
    finally:
        worker.close()


def test_runtime_configuration_cannot_replace_an_issued_grant(pending_public):
    app, _, _, http, _, _, authority, _ = pending_public
    app.public_grants.require_spec(authority)
    for changed in (
        replace(authority, max_publications=1),
        replace(authority, grant_id="not-issued"),
    ):
        with pytest.raises(PolicyDeniedError, match="configuration"):
            app.public_grants.require_spec(changed)
    assert http.calls == []


def test_run_window_checks_the_reviewed_issued_grant_before_entering_runner(
    monkeypatch,
):
    from types import SimpleNamespace
    from app.ops import int24_threads_runtime as module

    window = spec(publication_grant="finite-public-grant-1")
    authority = grant(window)
    config = {
        "operating_window_reviewed": True,
        "public_mission_path": "/protected/mission",
        "enable_autonomous_publication": True,
        "autonomous_publication_reviewed": True,
        "operating_window": json.loads(window.encoded()),
        "public_grant": json.loads(authority.encoded()),
    }
    calls = []

    def refuse(specification):
        assert specification == authority
        calls.append("checked")
        raise PolicyDeniedError("grant configuration changed")

    runtime = SimpleNamespace(public_grants=SimpleNamespace(require_spec=refuse))
    monkeypatch.setattr(module, "_config", lambda _: config)
    monkeypatch.setattr(module, "_runtime", lambda _: runtime)
    monkeypatch.setattr(
        module,
        "PublicOperatingWindow",
        lambda *_: pytest.fail("runner entered before grant validation"),
    )
    with pytest.raises(PolicyDeniedError, match="configuration"):
        module.main(["run-window", "--config", "/protected/config", "--activate-live"])
    assert calls == ["checked"]


def test_unreviewed_autonomous_config_is_rejected_before_reading_credentials(
    monkeypatch,
):
    from app.ops import int24_threads_runtime as module

    monkeypatch.setattr(
        module,
        "read_protected_worker_file",
        lambda *_args, **_kwargs: pytest.fail("credential read before grant review"),
    )
    with pytest.raises(ValueError, match="reviewed"):
        module._runtime({"enable_autonomous_publication": True})


def test_current_grant_approval_lookup_rechecks_live_revocation(pending_public):
    from app.threads.domain import ApprovalPurpose

    app, _, _, http, clock, _, authority, item = pending_public
    approval = app.public_grants._admit(authority.grant_id, item)

    def current():
        return app.authorization.current_approval(
            account_ref=app.account,
            operation_id=item.operation_id,
            approval_reference=approval.reference,
            now=clock.now(),
        )

    assert current().purpose is ApprovalPurpose.AUTONOMOUS_PUBLICATION
    app.public_grants.revoke(authority.grant_id)
    assert current() is None
    assert http.calls == []
