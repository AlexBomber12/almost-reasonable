"""Authenticated local budget admission must never cross lane or key boundaries."""

import json
import os
import threading

import pytest

from app.trusted.shared_budget_service import (
    BudgetServer,
    RemoteBudget,
    open_monthly_budget,
)
from app.trusted.shared_monthly_budget import SharedWallet
from tests.test_shared_monthly_budget import NOV, PROJECT, USAGE, sources


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)


def config(tmp_path, socket_location, **changes):
    path = tmp_path / "route.json"
    path.write_text(
        json.dumps(
            dict(
                version="shared-budget-route-v1",
                project_id=PROJECT,
                lane="sol",
                socket_path=str(socket_location),
                server_uid=os.geteuid(),
            )
            | changes
        )
    )
    path.chmod(0o400)
    return path


def test_real_socket_reservations_settlement_and_restart(tmp_path):
    path = tmp_path / "shared.sqlite"
    SharedWallet.initialize(path, project_id=PROJECT, sources=sources(tmp_path))
    ledger = SharedWallet(path, project_id=PROJECT, clock=lambda: NOV)
    sockdir = tmp_path / "ipc"
    sockdir.mkdir(mode=0o700)
    server = BudgetServer(sockdir / "budget.sock", ledger, {os.geteuid(): "sol"})
    server.start()
    stopped = threading.Event()
    thread = threading.Thread(
        target=server.serve, kwargs={"should_stop": stopped.is_set}
    )
    thread.start()
    try:
        route = config(tmp_path, server.socket.socket_path)
        client = open_monthly_budget(route, lane="sol", project_id=PROJECT)
        assert isinstance(client, RemoteBudget)
        assert client.reserve("paid")
        assert not client.reserve("paid")
        client.settle("paid", USAGE)
        assert client.snapshot()["charged_microusd"] == 3850
        before = path.read_bytes()
        assert client.observation()["scope"] == "shared_conservative_project_wallet"
        assert path.read_bytes() == before
        from app.trusted.budget_telemetry import observe_budget
        from app.worker_telemetry.protocol import _budget

        observation = observe_budget(route)
        assert _budget(observation)["limit_microusd"] == 40_000_000
        assert path.read_bytes() == before
        wrong = RemoteBudget(route, lane="sol", project_id=PROJECT)
        wrong.route = wrong.route | dict(server_uid=os.geteuid() + 1)
        with pytest.raises(ValueError):
            wrong.snapshot()
        assert ledger.snapshot("sol")["charged_microusd"] == 3850
        wrong.route = wrong.route | dict(
            server_uid=os.geteuid(), project_id="proj_other"
        )
        with pytest.raises(ValueError):
            wrong.snapshot()
        with pytest.raises(ValueError):
            open_monthly_budget(route, lane="private_sol", project_id=PROJECT)
        with pytest.raises(ValueError):
            open_monthly_budget(route, lane="sol", project_id="proj_other")
    finally:
        stopped.set()
        thread.join(5)
        server.close()
    assert not thread.is_alive()
    with pytest.raises(ValueError):
        client.reserve("offline")
    assert observe_budget(route) == {"available": False}
    assert ledger.snapshot("sol")["charged_microusd"] == 3850


def test_service_authenticates_lane_and_closed_operation_before_mutation(tmp_path):
    path = tmp_path / "shared.sqlite"
    SharedWallet.initialize(path, project_id=PROJECT, sources=sources(tmp_path))
    ledger = SharedWallet(path, project_id=PROJECT, clock=lambda: NOV)
    server = BudgetServer(tmp_path / "budget.sock", ledger, {os.geteuid(): "sol"})
    for uid, request in [
        (os.geteuid() + 1, dict(op="reserve", operation="bad")),
        (os.geteuid(), dict(op="reserve", operation="bad", lane="private_sol")),
        (os.geteuid(), dict(op="refund", operation="bad")),
        (os.geteuid(), dict(op="reserve", operation=True)),
    ]:
        with pytest.raises(ValueError):
            server.dispatch(
                uid,
                dict(
                    version="shared-budget-route-v1",
                    project_id=PROJECT,
                    lane="sol",
                    request=request,
                ),
            )
    for request in (
        {},
        dict(
            version="shared-budget-route-v1",
            project_id=PROJECT,
            lane="research",
            request=dict(op="reserve", operation="bad"),
        ),
        dict(
            version="shared-budget-route-v1", project_id=PROJECT, lane="sol", request=[]
        ),
    ):
        with pytest.raises(ValueError):
            server.dispatch(os.geteuid(), request)
    assert ledger.snapshot("sol")["charged_microusd"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        dict(version="other"),
        dict(lane="private_sol"),
        dict(server_uid=True),
        dict(socket_path="relative.sock"),
        dict(extra=True),
    ],
)
def test_bad_route_is_rejected_without_any_sdk_call(tmp_path, changes):
    path = config(tmp_path, tmp_path / "budget.sock", **changes)
    with pytest.raises(ValueError):
        open_monthly_budget(path, lane="sol", project_id=PROJECT)


def test_legacy_sqlite_wallet_still_opens_without_broker(tmp_path):
    path = sources(tmp_path)["sol"]
    assert open_monthly_budget(path, lane="sol", project_id=PROJECT).lane == "sol"


def test_wrong_server_identity_is_rejected_before_request(tmp_path):
    path = config(tmp_path, tmp_path / "budget.sock", server_uid=os.geteuid() + 1)
    client = open_monthly_budget(path, lane="sol", project_id=PROJECT)
    with pytest.raises(ValueError):
        client.snapshot()


@pytest.mark.parametrize(
    "raw", [b'{"op":"reserve","op":"observe"}', b"[]", b" " * 4097]
)
def test_ambiguous_or_oversized_protocol_messages_are_rejected(raw):
    from app.trusted.shared_budget_service import _decode

    with pytest.raises(ValueError):
        _decode(raw)


@pytest.mark.parametrize(
    "method,result",
    [
        ("reserve", 1),
        ("settle", {}),
        ("snapshot", []),
        ("snapshot", {}),
        ("snapshot", dict(charged_microusd=0, limit_microusd=1, attempts=0)),
    ],
)
def test_malformed_service_results_never_grant_money(
    tmp_path, monkeypatch, method, result
):
    path = config(tmp_path, tmp_path / "budget.sock")
    client = open_monthly_budget(path, lane="sol", project_id=PROJECT)
    monkeypatch.setattr(client, "_call", lambda request: result)
    args = dict(reserve=("op",), settle=("op", USAGE), snapshot=())[method]
    with pytest.raises(ValueError):
        getattr(client, method)(*args)


def test_service_cannot_start_with_ambiguous_peers_or_serve_before_start(tmp_path):
    from types import SimpleNamespace

    for peers in ({}, {True: "sol"}, {os.geteuid(): "other"}):
        with pytest.raises(ValueError):
            BudgetServer(
                tmp_path / "budget.sock",
                SimpleNamespace(path=tmp_path / "ledger"),
                peers,
            )
    server = BudgetServer(
        tmp_path / "budget.sock",
        SimpleNamespace(path=tmp_path / "ledger"),
        {os.geteuid(): "sol"},
    )
    with pytest.raises(RuntimeError):
        server.serve_once()


def test_unauthorized_real_peer_cannot_reserve(tmp_path):
    path = tmp_path / "shared.sqlite"
    SharedWallet.initialize(path, project_id=PROJECT, sources=sources(tmp_path))
    wallet = SharedWallet(path, project_id=PROJECT, clock=lambda: NOV)
    server = BudgetServer(
        tmp_path / "budget.sock",
        wallet,
        {os.geteuid() + 1: "sol"},
    )
    server.start()
    thread = threading.Thread(target=server.serve_once)
    thread.start()
    try:
        client = open_monthly_budget(
            config(tmp_path, server.socket.socket_path), lane="sol", project_id=PROJECT
        )
        with pytest.raises(ValueError):
            client.reserve("not-authorized")
    finally:
        thread.join(4)
        server.close()
    assert not thread.is_alive()


@pytest.mark.parametrize("grouped", [False, True])
def test_broker_singleton_and_recovery_do_not_replenish_wallet(tmp_path, grouped):
    import socket

    path = tmp_path / "shared.sqlite"
    SharedWallet.initialize(path, project_id=PROJECT, sources=sources(tmp_path))
    wallet = SharedWallet(path, project_id=PROJECT, clock=lambda: NOV)
    assert wallet.reserve("sol", "survives-crash")
    directory = tmp_path / "ipc"
    directory.mkdir(mode=0o710 if grouped else 0o700)
    directory.chmod(0o710 if grouped else 0o700)
    sock = directory / "budget.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as abandoned:
        abandoned.bind(str(sock))
    sock.chmod(0o660 if grouped else 0o600)
    broker = BudgetServer(
        sock,
        wallet,
        {os.geteuid(): "sol"},
        socket_group_gid=os.getegid() if grouped else None,
    )
    broker.start()
    try:
        with pytest.raises(RuntimeError):
            broker.start()
        other = BudgetServer(sock, wallet, {os.geteuid(): "sol"})
        with pytest.raises((ValueError, OSError)):
            other.start()
        assert sock.exists()
        assert wallet.snapshot("sol")["charged_microusd"] == 400000
    finally:
        broker.close()
    broker.start()
    broker.close()
    assert wallet.snapshot("sol")["unresolved_attempts"] == 1


@pytest.mark.parametrize("existing", ["regular", "live"])
def test_broker_never_unlinks_foreign_or_live_socket(tmp_path, existing):
    import socket

    path = tmp_path / "shared.sqlite"
    SharedWallet.initialize(path, project_id=PROJECT, sources=sources(tmp_path))
    wallet = SharedWallet(path, project_id=PROJECT, clock=lambda: NOV)
    sock = tmp_path / "budget.sock"
    listener = None
    if existing == "regular":
        sock.write_text("preserve")
    else:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(sock))
        sock.chmod(0o600)
        listener.listen(1)
    identity = sock.lstat().st_ino
    broker = BudgetServer(sock, wallet, {os.geteuid(): "sol"})
    try:
        with pytest.raises(ValueError):
            broker.start()
        assert sock.lstat().st_ino == identity
    finally:
        broker.close()
        if listener is not None:
            listener.close()


def test_socket_recovery_honors_a_changed_inode_and_releases_lease(
    tmp_path, monkeypatch
):
    from pathlib import Path
    import socket

    path = tmp_path / "shared.sqlite"
    SharedWallet.initialize(path, project_id=PROJECT, sources=sources(tmp_path))
    wallet = SharedWallet(path, project_id=PROJECT, clock=lambda: NOV)
    sock = tmp_path / "budget.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as abandoned:
        abandoned.bind(str(sock))
    sock.chmod(0o600)
    original = Path.lstat
    calls = []

    def changed(self):
        info = original(self)
        if self == sock:
            calls.append(True)
            if len(calls) > 1:
                values = list(info)
                values[1] += 1
                return os.stat_result(values)
        return info

    broker = BudgetServer(sock, wallet, {os.geteuid(): "sol"})
    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", changed)
        with pytest.raises(ValueError, match="changed during recovery"):
            broker.start()
    assert sock.exists() and broker.lease is None


def test_broker_cannot_lock_a_replaced_wallet(tmp_path):
    path = tmp_path / "shared.sqlite"
    SharedWallet.initialize(path, project_id=PROJECT, sources=sources(tmp_path))
    wallet = SharedWallet(path, project_id=PROJECT, clock=lambda: NOV)
    path.rename(tmp_path / "before.sqlite")
    path.write_bytes((tmp_path / "before.sqlite").read_bytes())
    path.chmod(0o600)
    broker = BudgetServer(tmp_path / "budget.sock", wallet, {os.geteuid(): "sol"})
    with pytest.raises(ValueError, match="lease state replaced"):
        broker.start()
    assert broker.lease is None


def test_cli_restores_signal_handlers_and_closes_on_failure(tmp_path, monkeypatch):
    import signal
    from app.trusted import shared_budget_service as service

    seen = []
    handlers = {}

    def register(sig, handler):
        handlers[sig] = handler
        return "previous-" + str(sig)

    monkeypatch.setattr(service.signal, "signal", register)
    monkeypatch.setattr(service, "SharedWallet", lambda *a, **k: "wallet")

    class Server:
        def __init__(self, *a, **k):
            seen.append(a)

        def start(self):
            seen.append("started")

        def serve(self, *, should_stop):
            assert not should_stop()
            handlers[signal.SIGTERM](None, None)
            assert should_stop()

        def close(self):
            seen.append("closed")

    monkeypatch.setattr(service, "BudgetServer", Server)
    args = [
        "--wallet",
        str(tmp_path / "ledger"),
        "--socket",
        str(tmp_path / "budget.sock"),
        "--project-id",
        PROJECT,
        "--peer",
        "11:sol",
    ]
    assert service.main(args) == 0
    assert seen[-1] == "closed"
    assert all(type(v) is str for v in handlers.values())
    with pytest.raises(ValueError, match="duplicate"):
        service.main(args + ["--peer", "11:research"])


def test_module_entrypoint_is_available_without_opening_wallets(monkeypatch):
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["shared-budget", "--help"])
    monkeypatch.delitem(sys.modules, "app.trusted.shared_budget_service")
    with pytest.raises(SystemExit) as ended:
        runpy.run_module("app.trusted.shared_budget_service", run_name="__main__")
    assert ended.value.code == 0


@pytest.mark.parametrize("raw", [None, "not-json", "{}"])
def test_missing_or_malformed_route_only_makes_observation_unavailable(tmp_path, raw):
    from app.trusted.budget_telemetry import observe_budget

    path = tmp_path / "bad.json"
    if raw is not None:
        path.write_text(raw)
        path.chmod(0o400)
    assert observe_budget(path) == {"available": False}


@pytest.mark.parametrize("object_usage", [False, True])
def test_sdk_fence_carries_provider_cached_usage_to_shared_wallet(object_usage):
    from types import SimpleNamespace
    from app.trusted.public_monthly_budget import BudgetedResponses
    from app.trusted.shared_monthly_budget import SHARED_POLICIES

    usage = USAGE | dict(input_tokens_details=dict(cached_tokens=1000))
    if object_usage:
        usage = SimpleNamespace(
            **(USAGE | dict(input_tokens_details=SimpleNamespace(cached_tokens=1000)))
        )
    settled = []
    budget = SimpleNamespace(
        lane="sol",
        policy=SHARED_POLICIES["sol"],
        reserve=lambda operation: True,
        settle=lambda operation, value: settled.append(value),
    )
    sdk = SimpleNamespace(create=lambda **kwargs: dict(usage=usage))
    BudgetedResponses(sdk, budget).create(
        model="gpt-6.1-sol",
        input="synthetic",
        tools=[],
        max_output_tokens=2048,
        store=False,
    )
    assert settled == [USAGE | dict(cached_input_tokens=1000)]
