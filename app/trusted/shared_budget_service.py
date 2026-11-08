"""Peer-authenticated local budget broker; no credentials, network, or prompts."""

import argparse
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
from time import monotonic

from app.trusted.openai_credential_files import read_protected_worker_file
from app.trusted.openai_worker_protocol import receive_frame, send_frame
from app.trusted.public_monthly_budget import MonthlyBudget
from app.trusted.shared_monthly_budget import SharedWallet, SHARED_POLICIES
from app.trusted.worker_telemetry import (
    WorkerTelemetryServer,
    _peer_uid,
    _safe_socket_directory,
)


VERSION = "shared-budget-route-v1"


def _payload(value, route, key):
    if set(value) != {"version", "project_id", "lane", key} or any(
        value[k] != route[k] for k in ("version", "project_id", "lane")
    ):
        raise ValueError("invalid shared budget envelope")
    return value[key]


def _decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate budget field")
            result[key] = value
        return result

    if len(raw) > 4096:
        raise ValueError("oversized budget message")
    result = json.loads(raw, object_pairs_hook=unique)
    if type(result) is not dict:
        raise ValueError("invalid budget message")
    return result


class RemoteBudget:
    """A lane-bound client; only the service can mutate the shared ledger."""

    def __init__(self, path, *, lane, project_id):
        value = _decode(read_protected_worker_file(Path(path), maximum_bytes=4096))
        if (
            set(value) != {"version", "project_id", "lane", "socket_path", "server_uid"}
            or value["version"] != VERSION
            or value["project_id"] != project_id
            or value["lane"] != lane
            or lane not in SHARED_POLICIES
            or type(value["server_uid"]) is not int
            or not 0 <= value["server_uid"] < 2**32 - 1
            or type(value["socket_path"]) is not str
            or not Path(value["socket_path"]).is_absolute()
        ):
            raise ValueError("invalid shared budget route")
        self.route = value
        self.lane, self.policy = lane, SHARED_POLICIES[lane]

    def _call(self, request):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2)
                connection.connect(self.route["socket_path"])
                if _peer_uid(connection) != self.route["server_uid"]:
                    raise ValueError("shared budget service identity changed")
                send_frame(
                    connection,
                    json.dumps(
                        {k: self.route[k] for k in ("version", "project_id", "lane")}
                        | dict(request=request)
                    ).encode(),
                    deadline=monotonic() + 2,
                    io_timeout=2,
                )
                connection.shutdown(socket.SHUT_WR)
                response = _decode(
                    receive_frame(connection, deadline=monotonic() + 2, io_timeout=2)
                )
            return _payload(response, self.route, "result")
        except (OSError, ValueError) as error:
            raise ValueError("shared budget service unavailable") from error

    def reserve(self, operation):
        result = self._call(dict(op="reserve", operation=operation))
        if type(result) is not bool:
            raise ValueError("invalid shared budget admission")
        return result

    def settle(self, operation, usage):
        if self._call(dict(op="settle", operation=operation, usage=usage)) is not None:
            raise ValueError("invalid shared budget settlement")

    def snapshot(self):
        result = self._call(dict(op="snapshot"))
        if (
            type(result) is not dict
            or any(
                type(result.get(k)) is not int or result[k] < 0
                for k in ("charged_microusd", "limit_microusd", "attempts")
            )
            or result["limit_microusd"] != 40_000_000
        ):
            raise ValueError("invalid shared budget snapshot")
        return result

    def observation(self):
        value = self._call(dict(op="observe"))
        return {
            k: v for k, v in value.items() if k not in ("charged_microusd", "attempts")
        } | dict(
            available=True,
            scope="shared_conservative_project_wallet",
            billing_authority=False,
            collected_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            lane=self.lane,
        )


def open_monthly_budget(path, *, lane, project_id, clock=lambda: datetime.now(UTC)):
    """Existing SQLite remains supported; a new route is protected operator input."""
    path = Path(path)
    if path.suffix == ".json":
        return RemoteBudget(path, lane=lane, project_id=project_id)
    return MonthlyBudget(path, lane=lane, project_id=project_id, clock=clock)


def observe_remote_budget(path):
    route = _decode(read_protected_worker_file(path, maximum_bytes=4096))
    return RemoteBudget(
        path, lane=route["lane"], project_id=route["project_id"]
    ).observation()


class BudgetServer:
    """Only OS-authenticated workers may reserve or settle their own lane."""

    def __init__(self, socket_path, wallet, peers, *, socket_group_gid=None):
        if not peers or any(
            type(uid) is not int or lane not in SHARED_POLICIES
            for uid, lane in peers.items()
        ):
            raise ValueError("invalid shared budget peers")
        self.wallet, self.peers = wallet, peers.copy()
        self.lease = None
        # Reuse the established safe socket lifecycle, not telemetry operations.
        self.socket = WorkerTelemetryServer(
            socket_path,
            wallet.path,
            wallet.path,
            frozenset(peers),
            socket_group_gid=socket_group_gid,
        )

    def start(self):
        if self.lease is not None:
            raise RuntimeError("shared budget broker already started")
        self.lease = os.open(
            self.wallet.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        try:
            info = os.fstat(self.lease)
            if (info.st_dev, info.st_ino) != self.wallet.identity[-2:]:
                raise ValueError("shared budget lease state replaced")
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._recover_socket()
            self.socket.start()
        except BaseException:
            self.close()
            raise

    def _recover_socket(self):
        path, gid = self.socket.socket_path, self.socket.socket_group_gid
        _safe_socket_directory(path, gid, require_absent=False)
        try:
            before = path.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISSOCK(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != (0o600 if gid is None else 0o660)
            or (gid is not None and before.st_gid != gid)
        ):
            raise ValueError("unsafe stale budget socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                after = path.lstat()
                if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError("budget socket changed during recovery")
                path.unlink()
            else:
                raise ValueError("budget socket is still listening")

    def close(self):
        self.socket.close()
        if self.lease is not None:
            os.close(self.lease)
            self.lease = None

    def dispatch(self, uid, request):
        if uid not in self.peers or type(request) is not dict:
            raise ValueError("unauthorized budget peer")
        lane = self.peers[uid]
        request = _payload(
            request,
            dict(
                version=VERSION,
                project_id=json.loads(self.wallet.encoding)[1],
                lane=lane,
            ),
            "request",
        )
        if type(request) is not dict:
            raise ValueError("invalid shared budget request")
        op = request.get("op")
        if op == "snapshot" and set(request) == {"op"}:
            return self.wallet.snapshot(lane)
        if op == "observe" and set(request) == {"op"}:
            return self.wallet.observe(lane)
        if op == "reserve" and set(request) == {"op", "operation"}:
            return self.wallet.reserve(lane, request["operation"])
        if op == "settle" and set(request) == {"op", "operation", "usage"}:
            return self.wallet.settle(lane, request["operation"], request["usage"])
        raise ValueError("invalid shared budget operation")

    def serve_once(self):
        if self.socket.listener is None:
            raise RuntimeError("shared budget server not started")
        try:
            connection, _ = self.socket.listener.accept()
        except TimeoutError:
            return False
        with connection:
            try:
                uid = _peer_uid(connection)
                if uid not in self.peers:
                    return True
                request = _decode(
                    receive_frame(connection, deadline=monotonic() + 2, io_timeout=2)
                )
                result = self.dispatch(uid, request)
                response = dict(
                    version=VERSION,
                    project_id=json.loads(self.wallet.encoding)[1],
                    lane=self.peers[uid],
                    result=result,
                )
                send_frame(
                    connection,
                    json.dumps(response).encode(),
                    deadline=monotonic() + 2,
                    io_timeout=2,
                )
            except (OSError, ValueError, sqlite3.Error):
                pass
        return True

    def serve(self, *, should_stop):
        while not should_stop():
            self.serve_once()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="One local project budget; no provider access"
    )
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--wallet", required=True, type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--peer", required=True, action="append", help="UID:lane")
    parser.add_argument("--socket-group-gid", type=int)
    args = parser.parse_args(argv)
    peers = {}
    for item in args.peer:
        uid, lane = item.split(":")
        if int(uid) in peers:
            raise ValueError("duplicate shared budget peer")
        peers[int(uid)] = lane
    server = BudgetServer(
        args.socket,
        SharedWallet(args.wallet, project_id=args.project_id),
        peers,
        socket_group_gid=args.socket_group_gid,
    )
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    before = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        server.start()
        server.serve(should_stop=lambda: stopping)
    finally:
        server.close()
        for sig, handler in before.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
