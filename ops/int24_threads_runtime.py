"""Explicit local operator entrypoint for the unactivated INT-24 public runtime.

The default readiness invocation reads no credential and makes no network call.
Live operations require both reviewed configuration and --activate-live.
"""

import argparse
from datetime import UTC, datetime, time, timedelta
import json
from pathlib import Path
from uuid import uuid4

from app.behavior.scheduler import WakeupEvent, WakeupPolicy
from app.behavior.worker_client import PublicBehaviorClient
from app.core.provider_spend import ProviderStateStore
from app.core.public_behavior_mission import PublicMissionContext
from app.core.store import EventStore
from app.integration.threads_runtime import LocalOperator, ThreadsRuntime
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
        reply_thread_id=config["reply_thread_id"],
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
        enabled=True,
        live_enabled=True,
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "readiness",
            "wakeup",
            "poll",
            "process",
            "draft",
            "approve",
            "revoke",
            "dispatch",
            "reconcile",
        ),
    )
    parser.add_argument("value", nargs="?")
    parser.add_argument("--config")
    parser.add_argument("--activate-live", action="store_true")
    parser.add_argument("--expires-minutes", type=int, default=15)
    args = parser.parse_args(argv)
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
    app = _runtime(_config(args.config))
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
    else:
        result = app.reconcile(
            args.value,
            correlation_id="reconcile-" + uuid4().hex,
        )
    print(json.dumps(result, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
