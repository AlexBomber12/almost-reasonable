"""Explicit, local-only owner console composition for the private pilot."""

import argparse
from contextlib import closing
from ipaddress import ip_address
from pathlib import Path
import os
import sqlite3
import stat
from urllib.parse import urlsplit
from wsgiref.simple_server import make_server
from app.private_web.server import PrivateWSGIRequestHandler

from app.core.store import EventStore
from app.owner_control.store import OwnerStore
from app.owner_control.wsgi import OwnerApplication
from app.private_web.cli import IPv6WSGIServer
from app.private_web.wsgi import PrivateWebConfig, _valid_origin
from app.privacy.ledger import PrivacyLedger


def _existing_private_file(value):
    path = Path(value)
    try:
        entry = path.lstat()
    except OSError:
        raise ValueError("required application store is missing") from None
    if (
        not stat.S_ISREG(entry.st_mode)
        or entry.st_uid != os.geteuid()
        or stat.S_IMODE(entry.st_mode) != 0o600
        or entry.st_nlink != 1
    ):
        raise ValueError("unsafe application store")
    return path


def _existing_schema(path, *, privacy=False):
    # Read-only admission prevents startup from initializing an empty file.
    with closing(
        sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
    ) as connection:
        if privacy:
            if not PrivacyLedger._has_valid_schema(connection):
                raise ValueError("privacy ledger is not initialized")
        else:
            EventStore._verify_current_schema(connection)


def build_application(args):
    """Validate existing app-owned stores before any constructor can create one."""
    try:
        if not ip_address(args.listen_host).is_loopback:
            raise ValueError("owner listener must bind loopback")
        origin = urlsplit(args.allowed_origin)
        participant = urlsplit(args.participant_origin)
        if (
            origin.scheme != "https"
            or origin.netloc != args.allowed_host
            or participant.scheme != "https"
            or not participant.netloc
            or participant.path
            or participant.query
            or participant.fragment
            or not _valid_origin(args.participant_origin)
            or (origin.scheme, origin.hostname, origin.port or 443)
            == (participant.scheme, participant.hostname, participant.port or 443)
        ):
            raise ValueError("owner requires a separate HTTPS origin")
        owner_db = Path(args.owner_directory) / "owner.sqlite"
        for path in (args.event_db, args.ipc_state_db, args.privacy_ledger, owner_db):
            _existing_private_file(path)
        if (
            len(
                {
                    str(Path(path).resolve())
                    for path in (
                        args.event_db,
                        args.ipc_state_db,
                        args.privacy_ledger,
                        owner_db,
                    )
                }
            )
            != 4
        ):
            raise ValueError("application stores must be distinct")
        _existing_schema(Path(args.event_db))
        _existing_schema(Path(args.ipc_state_db))
        _existing_schema(Path(args.privacy_ledger), privacy=True)
        config = PrivateWebConfig(
            frozenset({args.allowed_host}),
            frozenset({args.allowed_origin}),
            trusted_proxy_addresses=frozenset(args.trusted_proxy),
        )
        owner = OwnerStore(args.owner_directory)
        events = EventStore(args.event_db)
        ipc = EventStore(args.ipc_state_db)
        ledger = PrivacyLedger(args.privacy_ledger)
        events.diagnostics()
        ipc.diagnostics()
        ledger.control_generation()
        return OwnerApplication(
            owner,
            events,
            config,
            privacy_ledger=ledger,
            allowed_clients=frozenset(args.allowed_client),
            pilot_provider_events=ipc,
        )
    except (OSError, RuntimeError, PermissionError, sqlite3.Error) as error:
        raise ValueError("application stores are unavailable or unsafe") from error


def main(argv=None, *, server_factory=make_server):
    parser = argparse.ArgumentParser(description="Local private-pilot owner console")
    parser.add_argument("--event-db", required=True)
    parser.add_argument("--ipc-state-db", required=True)
    parser.add_argument("--privacy-ledger", required=True)
    parser.add_argument("--owner-directory", required=True)
    parser.add_argument("--allowed-host", required=True)
    parser.add_argument("--allowed-origin", required=True)
    parser.add_argument("--participant-origin", required=True)
    parser.add_argument("--allowed-client", action="append", required=True)
    parser.add_argument("--trusted-proxy", action="append", default=[])
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18026)
    args = parser.parse_args(argv)
    try:
        application = build_application(args)
    except ValueError as error:
        parser.error(str(error))
    server_options = (
        {"server_class": IPv6WSGIServer}
        if ip_address(args.listen_host).version == 6
        else {}
    )
    server_options["handler_class"] = PrivateWSGIRequestHandler
    server = server_factory(args.listen_host, args.port, application, **server_options)
    print(f"Owner console listening on {args.listen_host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
