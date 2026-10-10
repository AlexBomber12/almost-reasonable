"""Operator-only account provisioning and local WSGI server commands."""

import argparse
from collections.abc import Callable, Sequence
from getpass import getpass
from typing import Any
from wsgiref.simple_server import make_server

from app.core.context import ContextLimits, PrivateContextAssembler
from app.core.store import EventStore

from .auth import AuthStore
from .conversation import ConversationService
from .wsgi import PrivateWebApplication, PrivateWebConfig


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
    serve.add_argument("--listen-host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--allowed-host", required=True)
    serve.add_argument("--allowed-origin", required=True)
    serve.add_argument(
        "--insecure-http",
        action="store_true",
        help="omit Secure cookie flag for loopback-only HTTP development",
    )
    return parser


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

    auth = AuthStore(args.auth_db)
    event_store = EventStore(args.event_db)
    assembler = PrivateContextAssembler(
        event_store,
        ContextLimits(max_utf8_bytes=256 * 1024, max_history_events=50),
    )
    conversations = ConversationService(event_store, assembler)
    application = PrivateWebApplication(
        auth,
        conversations,
        PrivateWebConfig(
            allowed_hosts=frozenset({args.allowed_host}),
            allowed_origins=frozenset({args.allowed_origin}),
            secure_cookies=not args.insecure_http,
        ),
    )
    server = server_factory(args.listen_host, args.port, application)
    print(f"Serving private chat on {args.listen_host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
