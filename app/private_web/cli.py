"""Operator-only account provisioning and local WSGI server commands."""

import argparse
import json
from collections.abc import Callable, Sequence
from getpass import getpass
from ipaddress import ip_address
from pathlib import Path
from socket import AF_INET6
from typing import Any
from urllib.parse import urlsplit
from wsgiref.simple_server import make_server, WSGIServer

from app.core.context import ContextLimits, PrivateContextAssembler
from app.core.store import EventStore
from app.core.openai_worker_client import OpenAIWorkerClient
from app.core.provider_spend import ProviderStateStore
from app.core.public_research_authority import load_operator_approval
from app.core.public_research_profile import load_research_profile
from app.core.public_research_client import PublicResearchClient
from app.core.events import (
    AuthenticatedSourceReference,
    ParticipantId,
    TrustedPrincipalContext,
)
from app.owner_control.store import OwnerStore
from app.privacy.application import PrivacyReadGate
from app.privacy.ledger import PrivacyLedger
from app.cognition.shared import SharedIntelligence
from app.trusted.private_pilot import read_pilot_control, _unique, PilotAuthorityError
from app.trusted.openai_credential_files import TrustedTransportConfigurationError

from .auth import AuthStore
from .conversation import ConversationService, _utc_now
from .privacy import ParticipantPrivacyControls
from .runtime import WorkerConversationRuntime
from .public_research import PublicResearchBridge
from .server import PrivateWSGIRequestHandler
from .wsgi import PrivateWebApplication, PrivateWebConfig


class IPv6WSGIServer(WSGIServer):
    """WSGI reference server variant for an explicit IPv6 listener."""

    address_family = AF_INET6


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Almost Reasonable private web")
    commands = parser.add_subparsers(dest="command", required=True)

    provision = commands.add_parser("provision", help="provision a local account")
    provision.add_argument("--auth-db", required=True)
    provision.add_argument("--username", required=True)
    provision.add_argument("--participant-id", required=True)

    serve = commands.add_parser("serve", help="serve the local private interface")
    serve.add_argument("--auth-db", required=True)
    serve.add_argument("--event-db", required=True)
    serve.add_argument("--worker-socket")
    serve.add_argument("--worker-uid", type=int)
    serve.add_argument("--worker-history-events", type=int, default=99)
    serve.add_argument("--ipc-state-db")
    serve.add_argument("--enable-memory-extraction", action="store_true")
    serve.add_argument("--enable-memory-evolution", action="store_true")
    serve.add_argument("--enable-typed-memory", action="store_true")
    serve.add_argument("--enable-narrative", action="store_true")
    serve.add_argument("--narrative-permission")
    serve.add_argument("--narrative-pilot-id")
    serve.add_argument("--enable-shared-proposals", action="store_true")
    serve.add_argument("--shared-proposal-audience", action="append", default=[])
    serve.add_argument("--enable-public-research", action="store_true")
    serve.add_argument("--public-research-socket")
    serve.add_argument("--public-research-worker-uid", type=int)
    serve.add_argument("--public-research-approval")
    serve.add_argument("--public-research-profile")
    serve.add_argument("--public-research-scope")
    serve.add_argument("--privacy-ledger")
    serve.add_argument("--owner-directory")
    serve.add_argument("--listen-host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--allowed-host", required=True)
    serve.add_argument("--allowed-origin", required=True)
    serve.add_argument(
        "--trusted-proxy",
        action="append",
        default=[],
        help="numeric proxy address allowed to supply one X-Forwarded-For client IP",
    )
    serve.add_argument(
        "--insecure-http",
        action="store_true",
        help="omit Secure cookie flag for loopback-only HTTP development",
    )
    return parser


def _loopback_ip(value: str) -> bool:
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _loopback_http_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname is not None
        and _loopback_ip(parsed.hostname)
    )


def _https_origin(value: str) -> bool:
    try:
        return urlsplit(value).scheme == "https"
    except ValueError:
        return False


def _narrative_permission_current(path: Path, pilot_id: str) -> bool:
    try:
        value = json.loads(
            read_pilot_control(path, maximum_bytes=1024), object_pairs_hook=_unique
        )
        return (
            type(value) is dict
            and set(value) == {"version", "pilot_id", "max_operations"}
            and value["version"] == "private-narrative-permission-v1"
            and value["pilot_id"] == pilot_id
            and type(value["max_operations"]) is int
            # Every valid pilot permits at least one operation; this narrower
            # permission limit cannot exceed its active allowance.
            and value["max_operations"] == 1
        )
    except (
        PilotAuthorityError,
        TrustedTransportConfigurationError,
        UnicodeError,
        ValueError,
        TypeError,
    ):
        return False


def main(
    argv: Sequence[str] | None = None,
    *,
    password_reader: Callable[[str], str] = getpass,
    server_factory: Callable[..., Any] = make_server,
) -> int:
    args = _parser().parse_args(argv)
    if args.command == "provision":
        password = password_reader("Password: ")
        confirmation = password_reader("Confirm password: ")
        if password != confirmation:
            raise SystemExit("password confirmation does not match")
        AuthStore(args.auth_db).provision_account(
            args.username, password, args.participant_id
        )
        print(f"Provisioned local account: {args.username}")
        return 0

    if args.insecure_http and not (
        _loopback_ip(args.listen_host) and _loopback_http_origin(args.allowed_origin)
    ):
        raise SystemExit(
            "--insecure-http requires a numeric loopback HTTP bind and origin"
        )
    if not args.insecure_http and not _https_origin(args.allowed_origin):
        raise SystemExit("secure mode requires an HTTPS browser origin")
    try:
        listener_address = ip_address(args.listen_host)
    except ValueError:
        raise SystemExit("listen host must be a numeric IP address") from None

    web_config = PrivateWebConfig(
        allowed_hosts=frozenset({args.allowed_host}),
        allowed_origins=frozenset({args.allowed_origin}),
        secure_cookies=not args.insecure_http,
        trusted_proxy_addresses=frozenset(args.trusted_proxy),
    )
    if args.worker_socket is not None:
        if args.enable_narrative != (
            args.narrative_permission is not None
            and args.narrative_pilot_id is not None
        ) or (
            not args.enable_narrative
            and (
                args.narrative_permission is not None
                or args.narrative_pilot_id is not None
            )
        ):
            raise SystemExit("narrative mode requires its permission file and pilot ID")
        if (
            args.privacy_ledger is None
            or args.owner_directory is None
            or args.ipc_state_db is None
        ):
            raise SystemExit(
                "worker mode requires privacy ledger, owner directory and application IPC state database"
            )
        if args.worker_uid is None:
            raise SystemExit("worker mode requires an expected worker UID")
        if (
            not all(
                Path(path).is_file()
                for path in (
                    args.auth_db,
                    args.event_db,
                    args.privacy_ledger,
                    args.ipc_state_db,
                )
            )
            or not (Path(args.owner_directory) / "owner.sqlite").is_file()
        ):
            raise SystemExit("worker mode requires existing application stores")
        research_options = (
            args.public_research_socket,
            args.public_research_worker_uid,
        )
        if (
            args.enable_public_research
            and (
                not all(item is not None for item in research_options)
                or (args.public_research_approval is None)
                == (args.public_research_profile is None)
                or (args.public_research_profile is None)
                != (args.public_research_scope is None)
            )
        ) or (
            not args.enable_public_research
            and any(
                item is not None
                for item in (
                    *research_options,
                    args.public_research_approval,
                    args.public_research_profile,
                    args.public_research_scope,
                )
            )
        ):
            raise SystemExit(
                "public research requires explicit enablement, socket, UID and approval"
            )
    elif (
        args.privacy_ledger is not None
        or args.owner_directory is not None
        or args.worker_uid is not None
        or args.ipc_state_db is not None
        or args.enable_public_research
        or args.public_research_socket is not None
        or args.public_research_worker_uid is not None
        or args.public_research_approval is not None
        or args.public_research_profile is not None
        or args.public_research_scope is not None
        or args.enable_memory_extraction
        or args.enable_memory_evolution
        or args.enable_typed_memory
        or args.enable_narrative
        or args.narrative_permission is not None
        or args.narrative_pilot_id is not None
        or args.enable_shared_proposals
        or args.shared_proposal_audience
    ):
        raise SystemExit("privacy and owner stores require worker mode")
    auth = AuthStore(args.auth_db)
    event_store = EventStore(args.event_db)
    privacy = None
    if args.worker_socket is None:
        assembler = PrivateContextAssembler(
            event_store,
            ContextLimits(max_utf8_bytes=256 * 1024, max_history_events=50),
        )
        conversations = ConversationService(event_store, assembler)
    else:
        ledger = PrivacyLedger(args.privacy_ledger)
        gate = PrivacyReadGate(ledger, event_store)
        if not gate.reconcile():
            raise SystemExit("privacy reconciliation failed")
        owner = OwnerStore(args.owner_directory)
        shared = SharedIntelligence(event_store, gate, clock=_utc_now)
        research = None
        if args.enable_public_research:
            authority_path = (
                args.public_research_approval or args.public_research_profile
            )
            approval = (
                load_operator_approval(authority_path, role="application")
                if args.public_research_approval is not None
                else load_research_profile(authority_path, role="application")
            )
            research = PublicResearchBridge(
                event_store,
                gate,
                PublicResearchClient(
                    args.public_research_socket,
                    expected_worker_uid=args.public_research_worker_uid,
                ),
                enabled=True,
                clock=_utc_now,
                approval_path=authority_path,
                application_scope=args.public_research_scope,
            )
            principal = TrustedPrincipalContext(
                ParticipantId(approval.principal_id),
                AuthenticatedSourceReference(approval.authenticated_source_ref),
            )
            if args.public_research_profile is not None:
                research.install_profile(approval, principal)
            else:
                research.install_approval(approval, principal)
        conversations = WorkerConversationRuntime(
            event_store,
            gate,
            OpenAIWorkerClient(args.worker_socket, expected_worker_uid=args.worker_uid),
            provider_state=ProviderStateStore(EventStore(args.ipc_state_db)),
            emergency_stop=owner.active,
            shared_intelligence=None if args.enable_narrative else shared,
            research_bridge=research,
            memory_extraction_enabled=args.enable_memory_extraction,
            memory_evolution_enabled=args.enable_memory_evolution,
            typed_memory_enabled=args.enable_typed_memory,
            narrative_enabled=args.enable_narrative,
            narrative_permission_current=(
                lambda: _narrative_permission_current(
                    Path(args.narrative_permission), args.narrative_pilot_id
                )
            )
            if args.enable_narrative
            else None,
            shared_proposals_enabled=args.enable_shared_proposals,
            shared_proposal_audience=tuple(sorted(set(args.shared_proposal_audience))),
            context_limits=ContextLimits(32_000, args.worker_history_events),
        )
        privacy = ParticipantPrivacyControls(
            auth, event_store, ledger, gate, clock=conversations._clock
        )
    application = PrivateWebApplication(
        auth,
        conversations,
        web_config,
        privacy=privacy,
    )
    server_options = (
        {"server_class": IPv6WSGIServer} if listener_address.version == 6 else {}
    )
    server_options["handler_class"] = PrivateWSGIRequestHandler
    server = server_factory(args.listen_host, args.port, application, **server_options)
    print(f"Serving private chat on {args.listen_host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
