"""Explicit local operator entrypoint for the unactivated INT-24 public runtime.

The default readiness invocation reads no credential and makes no network call.
Live operations require both reviewed configuration and --activate-live.
"""

import argparse
from datetime import UTC, datetime, time, timedelta
import json
from pathlib import Path
import signal
from uuid import uuid4

from app.behavior.scheduler import WakeupEvent, WakeupPolicy
from app.behavior.worker_client import PublicBehaviorClient
from app.core.provider_spend import ProviderStateStore
from app.core.public_behavior_mission import PublicMissionContext
from app.core.public_research_client import PublicResearchClient
from app.core.store import EventStore
from app.integration.threads_runtime import LocalOperator, ThreadsRuntime
from app.integration.public_window import PublicOperatingWindow, WindowSpec
from app.integration.public_grant import PublicGrantSpec
from app.core.public_research_profile import PUBLIC_PURPOSES, load_research_profile
from app.owner_control.store import OwnerStore
from app.privacy.ledger import PrivacyLedger
from app.threads.domain import AccountPolicy, InboundKind, Stage
from app.threads.transport import HTTPSClient, ThreadsHTTPTransport
from app.trusted.openai_credential_files import read_protected_worker_file


class Clock:
    def now(self):
        return datetime.now(UTC)


def _config(path):
    resolved = Path(path).absolute()
    value = json.loads(read_protected_worker_file(resolved, maximum_bytes=8192))
    if type(value) is not dict or value.get("activation_reviewed") is not True:
        raise ValueError("reviewed activation is absent")
    return value


def _runtime(config):
    if config.get("enable_autonomous_publication") is True:
        _grant_spec(config)
    clock = Clock()
    token = (
        read_protected_worker_file(
            Path(config["threads_token_file"]), maximum_bytes=512
        )
        .decode()
        .strip()
    )
    transport = ThreadsHTTPTransport(
        account_ref=config["account_ref"],
        provider_user_id=config["provider_user_id"],
        reply_thread_id=config.get("reply_thread_id"),
        token=token,
        client=HTTPSClient(),
    )
    policy = AccountPolicy(
        config["account_ref"],
        Stage.STAGE_3,
        False,
        True,
        frozenset(config["scopes"]),
        True,
        True,
        True,
    )
    return ThreadsRuntime(
        config["journal_path"],
        config["scheduler_path"],
        config["threads_path"],
        model=PublicBehaviorClient(
            Path(config["worker_socket"]), expected_uid=config["worker_uid"]
        ),
        transport=transport,
        policy=policy,
        owner=OwnerStore(config["owner_directory"], clock=clock.now),
        provider=ProviderStateStore(EventStore(config["event_store_path"])),
        ledger=PrivacyLedger(config["ledger_path"]),
        clock=clock,
        wakeup_policy=WakeupPolicy(
            time(0),
            time(23, 59),
            timedelta(minutes=15),
            timedelta(minutes=1),
            timedelta(days=1),
            1,
            5,
            (),
        ),
        operator=LocalOperator(config["operator_uid"], config["operator_actor"]),
        mission=PublicMissionContext(
            Path(config["public_mission_path"]), clock=clock.now
        )
        if config.get("public_mission_path")
        else None,
        research_client=PublicResearchClient(
            Path(config["public_research_socket"]),
            expected_worker_uid=config["public_research_worker_uid"],
        )
        if config.get("enable_public_research")
        else None,
        research_profile_path=config.get("public_research_profile"),
        research_scope=config.get("public_research_scope"),
        research_enabled=config.get("enable_public_research", False),
        mission_evolution_enabled=config.get("enable_public_mission_evolution", False),
        free_missions_enabled=config.get("enable_free_missions", False),
        autonomous_publication_enabled=config.get(
            "enable_autonomous_publication", False
        ),
        enabled=True,
        live_enabled=True,
    )


def _window_spec(config):
    if config.get("operating_window_reviewed") is not True:
        raise ValueError("reviewed operating window is absent")
    if not config.get("public_mission_path"):
        raise ValueError("reviewed public mission is required")
    value = config["operating_window"]
    spec = WindowSpec(
        **(value | {"expires_at": datetime.fromisoformat(value["expires_at"])})
    )
    if spec.mission_evolution != config.get("enable_public_mission_evolution", False):
        raise ValueError(
            "window mission mode requires matching runtime (fixed or evolving)"
        )
    return spec


def _grant_spec(config):
    if (
        config.get("enable_autonomous_publication") is not True
        or config.get("autonomous_publication_reviewed") is not True
    ):
        raise ValueError("reviewed autonomous publication authority is absent")
    value = config["public_grant"]
    return PublicGrantSpec(
        **(
            value
            | {
                "expires_at": datetime.fromisoformat(value["expires_at"]),
                "publication_types": tuple(value["publication_types"]),
            }
        )
    )


def _window_plan(config, spec):
    research_limit = 0
    if config.get("enable_public_research"):
        profile = load_research_profile(
            config["public_research_profile"], role="application"
        )
        if (
            profile.purpose not in PUBLIC_PURPOSES
            or profile.account_ref != config["account_ref"]
            or profile.application_scope != config["public_research_scope"]
        ):
            raise ValueError("public research authority mismatch")
        research_limit = profile.aggregate_limit
    result = {
        "active": False,
        "window": json.loads(spec.encoded()),
        "maximum_work": spec.maximum_work(research_limit=research_limit),
    }
    if spec.publication_grant is not None:
        grant = _grant_spec(config)
        if (
            grant.grant_id != spec.publication_grant
            or grant.window_id != spec.window_id
            or grant.account_ref != config["account_ref"]
            or grant.expires_at > spec.expires_at
        ):
            raise ValueError("public grant planning binding mismatch")
        result["public_grant"] = json.loads(grant.encoded())
        result["maximum_work"]["publication_posts"] = min(
            spec.max_model_calls, grant.max_publications
        )
        result["maximum_work"]["publication_http_requests"] = grant.max_http_requests
    return result


def _interrupt(_signal, _frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "readiness",
            "window-plan",
            "run-window",
            "issue-grant",
            "revoke-grant",
            "grant-status",
            "wakeup",
            "poll",
            "process",
            "draft",
            "approve",
            "revoke",
            "dispatch",
            "reconcile",
            "recover-sending",
            "approve-reconciliation-recovery",
            "recover-reconciliation",
        ),
    )
    parser.add_argument("value", nargs="?")
    parser.add_argument("reference", nargs="?")
    parser.add_argument("--config")
    parser.add_argument("--activate-live", action="store_true")
    parser.add_argument("--expires-minutes", type=int, default=15)
    args = parser.parse_args(argv)
    if args.command == "window-plan":
        if args.config is None:
            parser.error("reviewed configuration is required")
        config = _config(args.config)
        print(json.dumps(_window_plan(config, _window_spec(config))))
        return 0
    if not args.activate_live:
        if args.command != "readiness":
            parser.error("live operations require --activate-live")
        print(
            json.dumps(
                {
                    "ready": False,
                    "blockers": ["runtime_disabled", "live_activation_unreviewed"],
                }
            )
        )
        return 0
    if args.config is None:
        parser.error("reviewed configuration is required")
    config = _config(args.config)
    window = (
        _window_spec(config) if args.command in ("run-window", "issue-grant") else None
    )
    app = _runtime(config)
    if args.command == "readiness":
        result = {
            "ready": False,
            "policy_blockers": app.readiness(),
            "manual_checks": [
                "disclosure",
                "actual_account_and_scopes",
                "token_lifecycle",
                "authorized_live_smoke",
            ],
        }
    elif args.command == "issue-grant":
        result = app.public_grants.issue(_grant_spec(config), window)
    elif args.command == "revoke-grant":
        app.public_grants.revoke(args.value)
        result = {"grant_revoked": args.value}
    elif args.command == "grant-status":
        app.operator.authenticate()
        result = app.public_grants.summary(args.value)
    elif args.command == "run-window":
        if window.publication_grant is not None:
            app.public_grants.require_spec(_grant_spec(config))
        previous = signal.signal(signal.SIGTERM, _interrupt)
        try:
            result = PublicOperatingWindow(app, window).run()
        finally:
            signal.signal(signal.SIGTERM, previous)
    elif args.command == "wakeup":
        result = app.run(
            (WakeupEvent(args.value, app.clock.now(), "operator scheduled wakeup"),)
        )
    elif args.command == "poll":
        result = app.poll(
            InboundKind(args.value),
            correlation_id="poll-" + uuid4().hex,
            max_pages=1,
            max_events=10,
            timeout_seconds=10,
        )
    elif args.command == "process":
        result = app.process_inbound(InboundKind(args.value), limit=10)
    elif args.command == "draft":
        result = app.draft(args.value)
    elif args.command == "approve":
        if not 1 <= args.expires_minutes <= 60:
            parser.error("approval expiry must be 1 to 60 minutes")
        approval = app.approve(
            args.value,
            expires_at=app.clock.now() + timedelta(minutes=args.expires_minutes),
        )
        result = {
            "operation_id": approval.operation_id,
            "state": approval.state.value,
            "approval_reference": approval.approval_reference,
        }
    elif args.command == "revoke":
        app.revoke(args.value)
        result = {"revoked": args.value}
    elif args.command == "dispatch":
        result = app.dispatch(args.value)
    elif args.command == "recover-sending":
        result = app.recover_sending(args.value)
    elif args.command == "approve-reconciliation-recovery":
        if not 1 <= args.expires_minutes <= 60:
            parser.error("approval expiry must be 1 to 60 minutes")
        approval = app.approve_reconciliation_recovery(
            args.value,
            expires_at=app.clock.now() + timedelta(minutes=args.expires_minutes),
        )
        result = {
            "operation_id": approval.operation_id,
            "state": approval.state.value,
            "approval_reference": approval.approval_reference,
        }
    elif args.command == "recover-reconciliation":
        if args.reference is None:
            parser.error("recovery approval reference is required")
        result = app.recover_reconciliation_claim(args.value, args.reference)
    else:
        result = app.reconcile(
            args.value,
            correlation_id="reconcile-" + uuid4().hex,
        )
    print(json.dumps(result, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
