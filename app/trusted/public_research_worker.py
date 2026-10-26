"""Explicit operator-started public research worker process."""

import argparse
from contextlib import closing
import sqlite3
import json
import os
from pathlib import Path
import signal
import stat

from app.core.provider_spend import ProviderStateStore
from app.core.public_research_authority import (
    load_operator_approval,
    validate_trusted_directories,
)
from app.core.public_research_profile import load_research_profile
from app.core.store import EventStore
from app.trusted.openai_credential_files import read_protected_worker_file
from app.trusted.openai_transport import TrustedTransportConfigurationError
from app.trusted.openai_worker_server import WorkerServerConfig
from app.trusted.public_research import PublicResearchBoundary, PublicResearchServer
from app.trusted.public_research_transport import PublicResearchTransport
from app.trusted.research_authority_journal import AuthorityJournal
from app.trusted.private_pilot_worker import PilotStop


class PublicResearchStop:
    """An absent, unsafe or changed stop file blocks paid research."""

    def __init__(self, path: Path):
        self.path = path

    def active(self):
        try:
            validate_trusted_directories(self.path)

            def unique(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("duplicate stop field")
                    value[key] = item
                return value

            value = json.loads(
                read_protected_worker_file(self.path, maximum_bytes=256),
                object_pairs_hook=unique,
            )
            return (
                type(value) is not dict
                or set(value) != {"version", "stop"}
                or value["version"] != "public-research-stop-v1"
                or type(value["stop"]) is not bool
                or value["stop"]
            )
        except (
            TrustedTransportConfigurationError,
            UnicodeDecodeError,
            ValueError,
            OSError,
        ):
            return True


def _validate_provider_database(path: Path) -> None:
    """Keep durable reservations in a worker-owned, non-replaceable location."""
    if not path.is_absolute():
        raise ValueError("provider database path must be absolute")
    try:
        validate_trusted_directories(path)
        parent = path.parent.lstat()
        database = path.lstat()
        if (
            path.parent.resolve(strict=True) != path.parent
            or path.resolve(strict=True) != path
        ):
            raise ValueError("provider database path uses a symbolic link")
    except FileNotFoundError:
        raise ValueError("existing research provider database required") from None
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
        or not stat.S_ISREG(database.st_mode)
        or database.st_uid != os.geteuid()
        or stat.S_IMODE(database.st_mode) != 0o600
        or database.st_nlink != 1
    ):
        raise ValueError("unsafe research provider database")


def build_worker(
    *,
    socket_path: Path,
    provider_database: Path,
    approval_path: Path | None = None,
    profile_path: Path | None = None,
    authority_journal: Path | None = None,
    stop_path: Path | None = None,
    pilot_stop_path: Path | None = None,
    project_id: str,
    organization_id: str,
    trusted_ipc_gid: int | None = None,
    transport: PublicResearchTransport | None = None,
):
    if (stop_path is None) == (pilot_stop_path is None):
        raise ValueError("choose exactly one Stop contract")
    _validate_provider_database(provider_database)
    try:
        with closing(
            sqlite3.connect(provider_database.as_uri() + "?mode=ro", uri=True)
        ) as existing:
            EventStore._verify_current_schema(existing)
    except (sqlite3.Error, RuntimeError):
        raise ValueError(
            "existing initialized research provider database required"
        ) from None
    if (approval_path is None) == (profile_path is None):
        raise ValueError("choose exactly one public research authority")
    if (profile_path is None) != (authority_journal is None):
        raise ValueError("profile requires a separate authority journal")
    authority = (
        load_operator_approval(approval_path, role="worker")
        if approval_path is not None
        else load_research_profile(profile_path, role="worker")
    )
    stop = (
        PilotStop(pilot_stop_path)
        if pilot_stop_path is not None
        else PublicResearchStop(stop_path)
    )
    boundary = PublicResearchBoundary(
        store=ProviderStateStore(EventStore(provider_database)),
        transport=transport
        if transport is not None
        else PublicResearchTransport.from_protected_file(
            project_id=project_id, organization_id=organization_id
        ),
        enabled=True,
        emergency_stop=stop.active,
    )
    if approval_path is not None:
        boundary.install_operator_authorization(authority, approval_path=approval_path)
    else:
        boundary.install_profile(
            authority,
            profile_path=profile_path,
            journal=AuthorityJournal(authority_journal),
        )
    return PublicResearchServer(
        WorkerServerConfig(
            socket_path,
            frozenset({authority.peer_uid}),
            trusted_ipc_gid=trusted_ipc_gid,
        ),
        boundary,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run bounded public research worker")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--provider-database", type=Path, required=True)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--authority-journal", type=Path)
    stops = parser.add_mutually_exclusive_group(required=True)
    stops.add_argument("--stop-file", type=Path)
    stops.add_argument("--pilot-stop-file", type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--socket-group-gid", type=int)
    args = parser.parse_args(argv)
    worker = build_worker(
        socket_path=args.socket,
        provider_database=args.provider_database,
        approval_path=args.approval,
        profile_path=args.profile,
        authority_journal=args.authority_journal,
        stop_path=args.stop_file,
        pilot_stop_path=args.pilot_stop_file,
        project_id=args.project_id,
        organization_id=args.organization_id,
        trusted_ipc_gid=args.socket_group_gid,
    )
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    try:
        worker.start()
        worker.serve(should_stop=lambda: stopping)
    finally:
        worker.close()
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
