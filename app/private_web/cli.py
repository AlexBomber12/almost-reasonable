"""Operator-only account provisioning and local WSGI server commands."""

import argparse
from collections.abc import Callable, Sequence
from getpass import getpass
from ipaddress import ip_address
from socket import AF_INET6
from typing import Any
from urllib.parse import urlsplit
from wsgiref.simple_server import make_server, WSGIServer

from app.core.context import ContextLimits, PrivateContextAssembler
from app.core.store import EventStore

from .auth import AuthStore
from .conversation import ConversationService
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
        web_config,
    )
    server_options = (
        {"server_class": IPv6WSGIServer} if listener_address.version == 6 else {}
    )
    server = server_factory(args.listen_host, args.port, application, **server_options)
    print(f"Serving private chat on {args.listen_host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
