"""Real local broker + fake SDK: no paid provider calls or production files."""

import json
import os
import threading
from types import SimpleNamespace

import pytest

from app.continuity_experiment import Lab, VERSION
from app.continuity_sol import Sol
from app.trusted import openai_credential_files as credentials
from app.trusted.private_pilot_worker import STOP_VERSION
from app.trusted.shared_budget_service import BudgetServer
from tests.test_continuity_experiment import post
from tests.test_continuity_sol import Responses
from tests.test_shared_budget_service import config as route_config
from tests.test_shared_monthly_budget import PROJECT, wallet


@pytest.fixture
def setup(tmp_path, monkeypatch):
    read = credentials.read_protected_worker_file
    from acceptance.guards import install

    install(monkeypatch, local_ipc=True)
    budget = wallet(tmp_path)
    ipc = tmp_path / "ipc"
    ipc.mkdir(mode=0o700)
    server = BudgetServer(ipc / "budget.sock", budget, {os.geteuid(): "private_sol"})
    server.start()
    stopped = threading.Event()
    thread = threading.Thread(
        target=server.serve, kwargs={"should_stop": stopped.is_set}
    )
    thread.start()
    route = route_config(tmp_path, server.socket.socket_path, lane="private_sol")
    lab = Lab.create(tmp_path / "lab")
    post(lab)
    stop = tmp_path / "stop.json"
    stop.write_text(json.dumps(dict(version=STOP_VERSION, stop=False)))
    stop.chmod(0o400)
    activation = tmp_path / "activation.json"
    settings = dict(
        version=VERSION,
        experiment_root=str(lab.root),
        project_id=PROJECT,
        organization_id="org-synthetic",
        budget_route=str(route),
        stop_path=str(stop),
        max_calls=2,
    )
    activation.write_text(json.dumps(settings))
    activation.chmod(0o400)
    # Only experiment configuration, routes and stop files may cross this read.
    allowed = {activation, route, stop}

    def protected(path, **kwargs):
        assert path in allowed
        return read(path, **kwargs)

    monkeypatch.setattr(credentials, "read_protected_worker_file", protected)
    # shared_budget_service imports the reader by value; explicitly scope it too.
    monkeypatch.setattr(
        "app.trusted.shared_budget_service.read_protected_worker_file", protected
    )
    monkeypatch.setattr(
        "app.trusted.private_pilot.read_protected_worker_file", protected
    )
    responses = Responses()
    responses.response.usage = SimpleNamespace(
        input_tokens=1000, output_tokens=100, total_tokens=1100
    )
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(_client=SimpleNamespace(responses=responses))

    monkeypatch.setattr(credentials, "build_worker_openai_transport", factory)
    try:
        yield lab, budget, activation, settings, responses, calls
    finally:
        stopped.set()
        thread.join(timeout=3)
        server.close()


def test_factory_uses_same_shared_wallet_and_never_resets_it(setup):
    lab, budget, activation, settings, responses, calls = setup
    assert budget.reserve("research", "existing-unknown")
    before = budget.snapshot("private_sol")
    model = Sol.from_config(activation, lab.root)
    assert budget.snapshot("private_sol") == before
    assert calls == [dict(project_id=PROJECT, organization_id="org-synthetic")]
    assert lab.step("live-fake", "post", model)["status"] == "decided"
    after = budget.snapshot("private_sol")
    assert after["limit_microusd"] == 40_000_000
    assert after["charged_microusd"] > before["charged_microusd"]
    assert after["unresolved_attempts"] == before["unresolved_attempts"]
    assert len(responses.requests) == 1
    assert budget.snapshot("research")["charged_microusd"] == after["charged_microusd"]


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "wrong"},
        {"experiment_root": "/other"},
        {"unexpected": True},
        {"max_calls": 0},
    ],
)
def test_bad_activation_cannot_build_sdk_or_spend(setup, changes):
    lab, budget, activation, settings, responses, calls = setup
    before = budget.snapshot("private_sol")
    activation.chmod(0o600)
    activation.write_text(json.dumps(settings | changes))
    activation.chmod(0o400)
    with pytest.raises(ValueError):
        Sol.from_config(activation, lab.root)
    assert budget.snapshot("private_sol") == before
    assert calls == []


def test_missing_broker_and_stop_prevent_sdk_requests(setup):
    lab, budget, activation, settings, responses, calls = setup
    stop = activation.parent / "stop.json"
    stop.chmod(0o600)
    stop.write_text(json.dumps(dict(version=STOP_VERSION, stop=True)))
    stop.chmod(0o400)
    model = Sol.from_config(activation, lab.root)
    before = budget.snapshot("private_sol")
    assert lab.step("stopped", "post", model)["error"] == "stopped"
    assert budget.snapshot("private_sol") == before
    assert responses.requests == []
