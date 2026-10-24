"""INT-22 pilot authority and durable allowance regressions."""

from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import json
import os
import sqlite3

import pytest

from app.trusted.private_pilot import (
    PilotAuthorityError,
    PilotAllowance,
    load_activation,
)
from app.trusted.private_pilot_worker import PilotRequestGate, PilotStop
from app.trusted import private_pilot_worker
from app.ops.private_pilot_ops import prepare
from app.core.store import EventStore
from app.cognition.worker_evolution import EvolutionModelRequest
from app.cognition.worker_typed import TypedModelRequest
from app.trusted.openai_transport import OpenAIResponsesTransport
from app.trusted.openai_worker_protocol import InferenceRequest
from app.ops import private_pilot_ops


def activation_file(tmp_path):
    directory = tmp_path / "control"
    directory.mkdir(mode=0o700)
    path = directory / "activation.json"
    now = datetime.now(UTC).replace(microsecond=0)
    value = {
        "version": "private-pilot-activation-v1",
        "pilot_id": "synthetic-int22-a",
        "application_peer_uid": os.getuid(),
        "provider_config_version": "openai-private-pilot-v1",
        "model": "gpt-6-luna",
        "project_id": "proj_synthetic",
        "organization_id": "org-synthetic",
        "not_before": (now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(seconds=590)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "max_operations": 5,
        "max_attempts_per_operation": 1,
    }
    path.write_text(json.dumps(value))
    path.chmod(0o400)
    return path, value


def test_b03d_activation_is_opt_in_and_does_not_raise_ordinary_pilot_cap(tmp_path):
    path, value = activation_file(tmp_path)
    value.update(
        version="private-pilot-activation-b03d-v1",
        task="synthetic-private-robot-memory-b03d",
        max_operations=27,
    )
    value["expires_at"] = (
        datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=30)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.chmod(0o600)
    path.write_text(json.dumps(value))
    path.chmod(0o400)
    with pytest.raises(PilotAuthorityError):
        load_activation(path)
    memory = load_activation(path, allow_memory_acceptance=True)
    assert memory.max_operations == 27 and memory.memory_acceptance
    allowance = PilotAllowance.initialize(tmp_path / "memory-allowance.db", memory)
    gate = PilotRequestGate(
        memory,
        allowance,
        activation_path=path,
        stop=PilotStop(tmp_path / "unused-stop.json"),
    )
    assert gate.current()
    for index in range(27):
        assert allowance.reserve("reply:" + f"{index:064x}")
    assert not allowance.reserve("reply:" + "f" * 64)
    value["task"] = "different-task"
    path.chmod(0o600)
    path.write_text(json.dumps(value))
    path.chmod(0o400)
    with pytest.raises(PilotAuthorityError):
        load_activation(path, allow_memory_acceptance=True)
    assert not gate.current()


@pytest.mark.parametrize("change", ["task", "cap", "window", "flag"])
def test_b03d_activation_rejects_wrong_scope_and_bounds(tmp_path, change):
    path, value = activation_file(tmp_path)
    value.update(
        version="private-pilot-activation-b03d-v1",
        task="synthetic-private-robot-memory-b03d",
        max_operations=27,
    )
    value["expires_at"] = (
        datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=30)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    if change == "task":
        value["task"] = "wrong-task"
    elif change == "cap":
        value["max_operations"] = 28
    elif change == "window":
        value["expires_at"] = (
            datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.chmod(0o600)
    path.write_text(json.dumps(value))
    path.chmod(0o400)
    with pytest.raises(PilotAuthorityError):
        load_activation(
            path,
            allow_memory_acceptance=1 if change == "flag" else True,
        )


@pytest.mark.parametrize(
    "scope",
    [
        "ordinary",
        "missing_evolution",
        "missing_typed",
        "extraction",
        "narrative",
        "shared",
    ],
)
def test_b03d_worker_scope_requires_both_only_memory_permissions(tmp_path, scope):
    path, value = activation_file(tmp_path)
    if scope != "ordinary":
        value.update(
            version="private-pilot-activation-b03d-v1",
            task="synthetic-private-robot-memory-b03d",
            max_operations=27,
        )
        value["expires_at"] = (
            datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=30)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        path.chmod(0o600)
        path.write_text(json.dumps(value))
        path.chmod(0o400)
    selected = {
        "evolution_permission_path": None if scope == "missing_evolution" else path,
        "typed_permission_path": None if scope == "missing_typed" else path,
    }
    if scope in {"extraction", "narrative", "shared"}:
        selected[
            scope + "_permission_path"
            if scope != "extraction"
            else "extraction_permission_path"
        ] = path
    with pytest.raises(PilotAuthorityError, match="invalid memory acceptance scope"):
        private_pilot_worker.build_worker(
            socket_path=tmp_path / "unused.sock",
            provider_database=tmp_path / "unused-provider.db",
            allowance_database=tmp_path / "unused-allowance.db",
            activation_path=path,
            memory_acceptance=True,
            **selected,
        )


def test_b03d_allowance_preparation_requires_explicit_opt_in(tmp_path):
    path, value = activation_file(tmp_path)
    with pytest.raises(
        PilotAuthorityError, match="memory acceptance activation required"
    ):
        prepare(path, tmp_path / "uncreated.db", memory_acceptance=True)
    value.update(
        version="private-pilot-activation-b03d-v1",
        task="synthetic-private-robot-memory-b03d",
        max_operations=27,
    )
    value["expires_at"] = (
        datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=30)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.chmod(0o600)
    path.write_text(json.dumps(value))
    path.chmod(0o400)
    with pytest.raises(PilotAuthorityError):
        prepare(path, tmp_path / "uncreated.db")
    prepared = tmp_path / "prepared.db"
    prepare(path, prepared, memory_acceptance=True)
    assert (
        PilotAllowance(
            prepared, load_activation(path, allow_memory_acceptance=True)
        ).remaining()
        == 27
    )


def test_b03d_worker_cli_passes_explicit_scope_without_opening_credentials(
    tmp_path, monkeypatch
):
    seen = []

    class Worker:
        def start(self):
            seen.append("start")

        def serve(self, *, should_stop):
            assert not should_stop()
            seen.append("serve")

        def close(self):
            seen.append("close")

    def build_worker(**kwargs):
        assert kwargs["memory_acceptance"] is True
        assert kwargs["evolution_permission_path"] == tmp_path / "evolution.json"
        assert kwargs["typed_permission_path"] == tmp_path / "typed.json"
        return Worker()

    monkeypatch.setattr(private_pilot_worker, "build_worker", build_worker)
    assert (
        private_pilot_worker.main(
            [
                "--socket",
                str(tmp_path / "unused.sock"),
                "--provider-database",
                str(tmp_path / "provider.db"),
                "--allowance-database",
                str(tmp_path / "allowance.db"),
                "--socket-group-gid",
                "10",
                "--memory-evolution-permission",
                str(tmp_path / "evolution.json"),
                "--typed-memory-permission",
                str(tmp_path / "typed.json"),
                "--memory-acceptance",
            ]
        )
        == 0
    )
    assert seen == ["start", "serve", "close"]


def test_b03d_prepare_cli_passes_opt_in_without_opening_credentials(
    tmp_path, monkeypatch
):
    seen = []

    def prepared(activation, allowance, *, memory_acceptance):
        seen.append((activation, allowance, memory_acceptance))

    monkeypatch.setattr(private_pilot_ops, "prepare", prepared)
    activation = tmp_path / "activation.json"
    allowance = tmp_path / "allowance.db"
    assert (
        private_pilot_ops.main(
            [
                "prepare",
                "--activation",
                str(activation),
                "--allowance",
                str(allowance),
                "--memory-acceptance",
            ]
        )
        == 0
    )
    assert seen == [(activation, allowance, True)]


def test_b03d_worker_accepts_two_separately_permitted_operations(tmp_path, monkeypatch):
    path, value = activation_file(tmp_path)
    value.update(
        version="private-pilot-activation-b03d-v1",
        task="synthetic-private-robot-memory-b03d",
        max_operations=27,
    )
    value["expires_at"] = (
        datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=30)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.chmod(0o600)
    path.write_text(json.dumps(value))
    path.chmod(0o400)
    activation = load_activation(path, allow_memory_acceptance=True)
    allowance = tmp_path / "allowance.db"
    PilotAllowance.initialize(allowance, activation)
    provider = tmp_path / "provider.db"
    EventStore(provider)
    stop = path.parent / "stop.json"
    stop.write_text(
        json.dumps({"version": "private-pilot-emergency-stop-v1", "stop": False})
    )
    stop.chmod(0o400)
    permissions = []
    for name, version in (
        ("evolution", "private-memory-evolution-permission-v1"),
        ("typed", "private-typed-memory-permission-v1"),
    ):
        permission = path.parent / (name + ".json")
        permission.write_text(
            json.dumps(
                {
                    "version": version,
                    "pilot_id": activation.pilot_id,
                    "max_operations": 27,
                }
            )
        )
        permission.chmod(0o400)
        permissions.append(permission)
    monkeypatch.setattr(
        private_pilot_worker,
        "build_worker_openai_transport",
        lambda **_scope: OpenAIResponsesTransport(SimpleNamespace(responses=object())),
    )
    worker = private_pilot_worker.build_worker(
        socket_path=tmp_path / "worker.sock",
        provider_database=provider,
        allowance_database=allowance,
        activation_path=path,
        stop_path=stop,
        evolution_permission_path=permissions[0],
        typed_permission_path=permissions[1],
        memory_acceptance=True,
    )
    assert worker._evolution_enabled and worker._typed_enabled
    assert (
        worker._request_guard(
            InferenceRequest(
                "evolution:" + "a" * 64,
                "corr",
                EvolutionModelRequest("Synthetic input", ()),
            )
        )
        is None
    )
    assert (
        worker._request_guard(
            InferenceRequest(
                "typed:" + "b" * 64, "corr", TypedModelRequest("Synthetic input", ())
            )
        )
        is None
    )
    assert worker._evolution_commit_guard() and worker._typed_commit_guard()


def test_allowance_is_reserved_before_contact_and_survives_restart(tmp_path):
    path, _value = activation_file(tmp_path)
    authority = load_activation(path)
    db = tmp_path / "allowance.sqlite3"
    allowance = PilotAllowance.initialize(db, authority)
    first = "reply:" + "a" * 64
    assert allowance.reserve(first)
    assert not allowance.reserve(first)
    assert PilotAllowance(db, authority).remaining() == 4
    for index in range(2, 6):
        assert allowance.reserve("reply:" + str(index) * 64)
    assert not allowance.reserve("reply:" + "f" * 64)
    assert allowance.remaining() == 0


def test_activation_change_cannot_restore_allowance(tmp_path):
    path, value = activation_file(tmp_path)
    authority = load_activation(path)
    db = tmp_path / "allowance.sqlite3"
    assert PilotAllowance.initialize(db, authority).reserve("reply:" + "a" * 64)
    value["pilot_id"] = "synthetic-int22-b"
    path.chmod(0o600)
    path.write_text(json.dumps(value))
    path.chmod(0o400)
    changed = load_activation(path)
    with pytest.raises(PilotAuthorityError):
        PilotAllowance(db, changed)


def _rewrite(path, value):
    path.chmod(0o600)
    path.write_text(json.dumps(value))
    path.chmod(0o400)


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.update(extra=True),
        lambda v: v.update(version="wrong"),
        lambda v: v.update(pilot_id="bad pilot"),
        lambda v: v.update(application_peer_uid=True),
        lambda v: v.update(application_peer_uid=-1),
        lambda v: v.update(provider_config_version="wrong"),
        lambda v: v.update(model="wrong"),
        lambda v: v.update(project_id="wrong"),
        lambda v: v.update(organization_id="wrong"),
        lambda v: v.update(max_operations=6),
        lambda v: v.update(max_operations=True),
        lambda v: v.update(max_attempts_per_operation=2),
        lambda v: v.update(max_attempts_per_operation=True),
        lambda v: v.update(not_before="bad"),
        lambda v: v.update(not_before=v["expires_at"]),
        lambda v: v.update(expires_at="2099-01-01T00:00:00Z"),
        lambda v: v.update(expires_at="2000-01-01T00:00:00Z"),
    ],
)
def test_malformed_or_unbounded_activation_fails_closed(tmp_path, change):
    path, value = activation_file(tmp_path)
    change(value)
    _rewrite(path, value)
    with pytest.raises(PilotAuthorityError):
        load_activation(path)


def test_missing_unsafe_duplicate_or_expired_activation_fails_closed(tmp_path):
    path, value = activation_file(tmp_path)
    with pytest.raises(PilotAuthorityError):
        load_activation(path, now=datetime.now(UTC) + timedelta(hours=1))
    with pytest.raises(PilotAuthorityError):
        load_activation(path, now="not a clock")
    path.chmod(0o600)
    with pytest.raises(PilotAuthorityError):
        load_activation(path)
    path.chmod(0o400)
    path.unlink()
    with pytest.raises(PilotAuthorityError):
        load_activation(path)
    path.write_text(json.dumps(value)[:-1] + ', "pilot_id": "duplicate"}')
    path.chmod(0o400)
    with pytest.raises(PilotAuthorityError):
        load_activation(path)


def test_parallel_reservations_and_file_loss_cannot_rearm(tmp_path):
    path, _ = activation_file(tmp_path)
    authority = load_activation(path)
    db = tmp_path / "allowance.sqlite3"
    allowance = PilotAllowance.initialize(db, authority)
    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = list(
            executor.map(
                lambda i: allowance.reserve("reply:" + format(i, "064x")), range(12)
            )
        )
    assert sum(outcomes) == 5
    assert allowance.remaining() == 0
    db.unlink()
    with pytest.raises(PilotAuthorityError):
        PilotAllowance(db, authority)


def test_narrative_pair_reservation_is_atomic_and_reply_hold_is_one_use(tmp_path):
    path, value = activation_file(tmp_path)
    value["max_operations"] = 2
    path.chmod(0o600)
    path.write_text(json.dumps(value))
    path.chmod(0o400)
    allowance = PilotAllowance.initialize(
        tmp_path / "allowance.sqlite3", load_activation(path)
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda suffix: allowance.reserve(
                    "narrative:" + suffix * 64,
                    extraction_limit=2,
                    reserve_reply=True,
                ),
                ("a", "b"),
            )
        )
    assert sum(outcomes) == 1
    assert allowance.remaining() == 0
    winner = ("a", "b")[outcomes.index(True)]
    reply = "reply:" + winner * 64
    assert allowance.reserve(reply)
    assert not allowance.reserve(reply)
    assert allowance.remaining() == 0
    with pytest.raises(PilotAuthorityError):
        allowance.reserve("reply:" + "c" * 64, reserve_reply=True)
    with pytest.raises(PilotAuthorityError):
        allowance.reserve(
            "narrative:" + "c" * 64,
            extraction_limit=2,
            reserve_reply=1,
        )


def test_allowance_tamper_and_invalid_operations_fail_closed(tmp_path):
    path, _ = activation_file(tmp_path)
    authority = load_activation(path)
    db = tmp_path / "allowance.sqlite3"
    allowance = PilotAllowance.initialize(db, authority)
    with pytest.raises(PilotAuthorityError):
        PilotAllowance.initialize(db, authority)
    for invalid in ("reply:short", 1, "wrong:" + "a" * 64):
        with pytest.raises(PilotAuthorityError):
            allowance.reserve(invalid)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE authority SET digest='changed'")
    with pytest.raises(PilotAuthorityError):
        allowance.reserve("reply:" + "a" * 64)
    with pytest.raises(PilotAuthorityError):
        PilotAllowance(db, authority)
    db.chmod(0o644)
    with pytest.raises(PilotAuthorityError):
        allowance.remaining()


def test_stop_is_strict_and_defaults_active(tmp_path):
    path, _ = activation_file(tmp_path)
    stop_path = path.parent / "stop.json"
    stop = PilotStop(stop_path)
    assert stop.active()
    for payload, expected in (
        ('{"version":"private-pilot-emergency-stop-v1","stop":false}', False),
        ('{"version":"private-pilot-emergency-stop-v1","stop":true}', True),
        ('{"version":"private-pilot-emergency-stop-v1","stop":0}', True),
        (
            '{"version":"private-pilot-emergency-stop-v1","stop":true,"stop":false}',
            True,
        ),
        ('{"version":"private-pilot-emergency-stop-v1"', True),
    ):
        if stop_path.exists():
            stop_path.chmod(0o600)
        stop_path.write_text(payload)
        stop_path.chmod(0o400)
        assert stop.active() is expected


def test_allowance_invalid_configuration_and_storage_error_fail_closed(tmp_path):
    path, _ = activation_file(tmp_path)
    authority = load_activation(path)
    with pytest.raises(PilotAuthorityError):
        PilotAllowance("relative", authority)
    with pytest.raises(PilotAuthorityError):
        PilotAllowance.initialize("relative", authority)
    with pytest.raises(PilotAuthorityError):
        PilotAllowance.initialize(tmp_path / "missing" / "allowance.db", authority)
    db = tmp_path / "allowance.sqlite3"
    allowance = PilotAllowance.initialize(db, authority)
    db.write_bytes(b"not sqlite")
    with pytest.raises(PilotAuthorityError):
        allowance.remaining()
