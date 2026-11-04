"""An uncertain publication waits without new spending or process churn."""

from dataclasses import replace
from datetime import timedelta

import pytest

from acceptance.guards import install
from app.integration.public_standing import StandingPublicPolicy
from app.integration.public_window import PublicOperatingWindow
from app.threads.domain import OutboxState, PublicationType, SourceProvenance
from app.threads.service import PolicyDeniedError
from test_b11c_window import NO_ACTION, finish, serve
from test_finite_public_grant import POST
from test_standing_public import connected


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    install(monkeypatch, local_ipc=True)


def finite_leases(monkeypatch, calls):
    original = StandingPublicPolicy.lease

    def lease(self, now):
        window, grant = original(self, now)
        return replace(window, max_model_calls=calls, max_poll_pages=0), grant

    monkeypatch.setattr(StandingPublicPolicy, "lease", lease)


@pytest.mark.parametrize("cross_midnight", [False, True])
def test_standing_pause_resumes_after_verified_reconciliation_without_resend(
    tmp_path, monkeypatch, cross_midnight
):
    finite_leases(monkeypatch, 1 if cross_midnight else 2)
    (app, worker, sdk, http, clock), runner = connected(tmp_path)
    sdk.proposal = POST
    http.fail_readback = True
    waits = []
    thread = serve(worker, 2)
    try:

        def wait(seconds):
            if seconds == 0.25:
                # The dispatch returns UNKNOWN, then the next loop observes
                # the account pause before any additional model or HTTP work.
                assert app.service.store.unresolved_for_account(app.account)
                return
            assert seconds == 1
            assert len(sdk.calls) == 1 and len(http.calls) == 5
            assert app.operating_window is None
            unknown = app.service.store.unresolved_for_account(app.account)
            assert len(unknown) == 1 and unknown[0].provider_publication_id == "post-1"
            with app.journal.transaction() as db:
                assert (
                    db.execute("SELECT COUNT(*) FROM public_grants").fetchone()[0] == 1
                )
            waits.append(seconds)
            if cross_midnight and len(waits) == 1:
                clock.advance(timedelta(days=1))
                return
            http.fail_readback = False
            assert (
                app.reconcile(unknown[0].operation_id, "original-reconciliation").state
                is OutboxState.CONFIRMED
            )
            sdk.proposal = NO_ACTION
            clock.advance(timedelta(minutes=15))

        result = runner.run(wait=wait, should_stop=lambda: len(sdk.calls) == 2)
        finish(thread)
        assert result["state"] == "interrupted"
        assert len(waits) == (2 if cross_midnight else 1)
        assert len(sdk.calls) == 2 and len(http.calls) == 6
        assert sum(method == "POST" for method, _, _ in http.calls) == 2
        assert not app.service.store.unresolved_for_account(app.account)
        with app.journal.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM public_windows").fetchone()[0] == (
                2 if cross_midnight else 1
            )
        assert not app.standing_public and app.standing_guard is None
    finally:
        worker.close()


@pytest.mark.parametrize("control", ["interrupt", "owner_stop", "revoke"])
def test_standing_pause_honors_stop_and_never_retries_unknown(
    tmp_path, monkeypatch, control
):
    finite_leases(monkeypatch, 2)
    (app, worker, sdk, http, _clock), runner = connected(tmp_path)
    sdk.proposal = POST
    http.fail_readback = True
    stop = []
    thread = serve(worker, 1)
    try:

        def wait(seconds):
            if seconds == 0.25:
                assert app.service.store.unresolved_for_account(app.account)
                return
            assert seconds == 1 and len(sdk.calls) == 1 and len(http.calls) == 5
            assert app.service.store.unresolved_for_account(app.account)
            if control == "interrupt":
                stop.append(True)
            elif control == "owner_stop":
                with app.owner._connection() as db:
                    db.execute("UPDATE owner_stop SET active=1")
            else:
                runner.revoke()

        if control == "interrupt":
            assert (
                runner.run(wait=wait, should_stop=lambda: bool(stop))["state"]
                == "interrupted"
            )
        else:
            with pytest.raises(PolicyDeniedError):
                runner.run(wait=wait)
        finish(thread)
        assert len(sdk.calls) == 1 and len(http.calls) == 5
        unknown = app.service.store.unresolved_for_account(app.account)
        assert len(unknown) == 1 and unknown[0].attempt_count == 1
        assert not app.standing_public and app.standing_guard is None
    finally:
        worker.close()


def test_standing_pause_preserves_interrupted_sending_without_new_effects(
    tmp_path, monkeypatch
):
    finite_leases(monkeypatch, 2)
    (app, worker, sdk, http, _clock), runner = connected(tmp_path)
    sdk.proposal = POST
    request = http.request

    def crash_at_publish(method, url, *args):
        if method == "POST" and "threads_publish" in url:
            raise KeyboardInterrupt()
        return request(method, url, *args)

    http.request = crash_at_publish
    thread = serve(worker, 1)
    stop = []
    try:
        assert runner.run()["state"] == "interrupted"
        finish(thread)
        before = len(http.calls)
        assert app.service.store.has_sending_for_account(app.account)
        window, _ = runner.policy.lease(app.clock.now())
        assert PublicOperatingWindow(app, window).run()["state"] == "publication_paused"

        def wait(seconds):
            assert seconds == 1 and len(sdk.calls) == 1 and len(http.calls) == before
            stop.append(True)

        assert (
            runner.run(wait=wait, should_stop=lambda: bool(stop))["state"]
            == "interrupted"
        )
        assert app.service.store.has_sending_for_account(app.account)
        assert not app.service.store.unresolved_for_account(app.account)
    finally:
        worker.close()


def test_legacy_unknown_without_ids_is_never_cleared_or_given_new_authority(tmp_path):
    (app, worker, sdk, http, clock), runner = connected(tmp_path)
    operation = "legacy-container"
    app.service.propose(
        operation_id=operation,
        publication_type=PublicationType.POST,
        draft_id="legacy-draft",
        content="Синтетический черновик прежней версии.",
        source_provenance=SourceProvenance.PUBLIC,
        privacy_authorized=True,
        russian_output_authorized=True,
        correlation_id="legacy-attempt",
        now=clock.now(),
    )
    for state in (
        OutboxState.AWAITING_APPROVAL,
        OutboxState.READY,
        OutboxState.SENDING,
        OutboxState.UNKNOWN,
    ):
        app.service.store.transition(
            operation,
            state,
            now=clock.now(),
            reason="synthetic_legacy_record",
            dispatch_evidence="provider_transport_interrupted",
            increment_attempt=state is OutboxState.SENDING,
            reconciliation_status="required" if state is OutboxState.UNKNOWN else None,
        )
    original = app.service.store.get(operation)
    stop = []
    try:

        def wait(seconds):
            assert seconds == 1
            assert sdk.calls == http.calls == []
            assert app.service.store.get(operation) == original
            with app.journal.transaction() as db:
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='public_grants'"
                ).fetchone():
                    assert (
                        db.execute("SELECT COUNT(*) FROM public_grants").fetchone()[0]
                        == 0
                    )
            stop.append(True)

        assert (
            runner.run(wait=wait, should_stop=lambda: bool(stop))["state"]
            == "interrupted"
        )
        assert app.service.store.get(operation) == original
        assert (
            original.provider_container_id is original.provider_publication_id is None
        )
    finally:
        worker.close()
