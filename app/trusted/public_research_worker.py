"""Explicit operator-started public research worker process."""

import argparse
import json
from pathlib import Path
import signal

from app.core.provider_spend import ProviderStateStore
from app.core.public_research_authority import load_operator_approval
from app.core.store import EventStore
from app.trusted.openai_credential_files import read_protected_worker_file
from app.trusted.openai_transport import TrustedTransportConfigurationError
from app.trusted.openai_worker_server import WorkerServerConfig
from app.trusted.public_research import PublicResearchBoundary, PublicResearchServer
from app.trusted.public_research_transport import PublicResearchTransport


class PublicResearchStop:
    """An absent, unsafe or changed stop file blocks paid research."""

    def __init__(self, path: Path):
        self.path = path

    def active(self):
        try:
            value = json.loads(read_protected_worker_file(self.path, maximum_bytes=256))
            return value != {"version": "public-research-stop-v1", "stop": False}
        except (TrustedTransportConfigurationError, UnicodeDecodeError, ValueError):
            return True


def build_worker(
    *,
    socket_path: Path,
    provider_database: Path,
    approval_path: Path,
    stop_path: Path,
    project_id: str,
    organization_id: str,
    trusted_ipc_gid: int | None = None,
    transport: PublicResearchTransport | None = None,
):
    if not provider_database.is_file():
        raise ValueError("existing research provider database required")
    approval = load_operator_approval(approval_path, role="worker")
    stop = PublicResearchStop(stop_path)
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
    boundary.install_operator_authorization(approval, approval_path=approval_path)
    return PublicResearchServer(
        WorkerServerConfig(
            socket_path,
            frozenset({approval.peer_uid}),
            trusted_ipc_gid=trusted_ipc_gid,
        ),
        boundary,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run bounded public research worker")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--provider-database", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--socket-group-gid", type=int)
    args = parser.parse_args(argv)
    worker = build_worker(
        socket_path=args.socket,
        provider_database=args.provider_database,
        approval_path=args.approval,
        stop_path=args.stop_file,
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
