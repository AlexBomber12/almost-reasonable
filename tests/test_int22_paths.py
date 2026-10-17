"""Path ancestry and pathname-redirection regressions for INT-22 authority."""

import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from app.core.store import EventStore
from app.cognition.conversation_context import MemoryModelRequest
from app.trusted import private_pilot, private_pilot_worker
from app.trusted.openai_transport import OpenAIResponsesTransport
from app.trusted.openai_worker_protocol import InferenceRequest
from app.trusted.private_pilot import (
    PilotAllowance,
    PilotAuthorityError,
    load_activation,
    protected_ancestry,
)
from app.trusted.private_pilot_worker import PilotStop, _require_provider_database
from test_int22_pilot import activation_file
from test_int22_controls import _stop
from test_openai_worker_protocol import model_request


@pytest.mark.parametrize("mode", [0o777, 0o770])
def test_writable_ancestor_rejects_all_worker_controls_and_state(tmp_path, mode):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    control = unsafe / "control"
    control.mkdir(mode=0o700)
    path, _ = activation_file(control)
    stop_path = path.parent / "stop.json"
    _stop(stop_path, False)
    state = unsafe / "state"
    state.mkdir(mode=0o700)
    provider = state / "provider.db"
    EventStore(provider)
    safe_activation, _ = activation_file(tmp_path)
    authority = load_activation(safe_activation)
    unsafe.chmod(mode)
    with pytest.raises(PilotAuthorityError):
        load_activation(path)
    assert PilotStop(stop_path).active()
    with pytest.raises(PilotAuthorityError):
        PilotAllowance.initialize(state / "allowance.db", authority)
    with pytest.raises(PilotAuthorityError):
        _require_provider_database(provider)


def test_symlink_ancestor_rejects_allowance_and_provider(tmp_path):
    path, _ = activation_file(tmp_path)
    authority = load_activation(path)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(PilotAuthorityError):
        PilotAllowance.initialize(alias / "allowance.db", authority)
    provider = real / "provider.db"
    EventStore(provider)
    with pytest.raises(PilotAuthorityError):
        _require_provider_database(alias / "provider.db")


def test_untrusted_ancestor_owner_is_rejected(tmp_path, monkeypatch):
    path, _ = activation_file(tmp_path)
    ancestor = tmp_path / "owner"
    ancestor.mkdir(mode=0o700)
    moved = ancestor / "control"
    moved.mkdir(mode=0o700)
    path.rename(moved / "activation.json")
    (moved / "activation.json").chmod(0o400)
    real_lstat = Path.lstat

    def synthetic_lstat(candidate):
        result = real_lstat(candidate)
        if candidate == ancestor:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_uid=os.geteuid() + 1000,
                st_dev=result.st_dev,
                st_ino=result.st_ino,
            )
        return result

    monkeypatch.setattr(Path, "lstat", synthetic_lstat)
    with pytest.raises(PilotAuthorityError):
        load_activation(moved / "activation.json")


def test_ancestor_change_and_old_snapshot_cannot_restore_allowance(tmp_path):
    path, _ = activation_file(tmp_path)
    authority = load_activation(path)
    root = tmp_path / "anchor"
    root.mkdir(mode=0o700)
    state = root / "state"
    state.mkdir(mode=0o700)
    db = state / "allowance.db"
    allowance = PilotAllowance.initialize(db, authority)
    snapshot = tmp_path / "old.db"
    shutil.copy2(db, snapshot)
    assert allowance.reserve("reply:" + "a" * 64)
    root.rename(tmp_path / "moved")
    root.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    shutil.copy2(snapshot, db)
    with pytest.raises(PilotAuthorityError):
        allowance.reserve("reply:" + "b" * 64)
    with pytest.raises(PilotAuthorityError):
        allowance.remaining()
    with pytest.raises(PilotAuthorityError):
        PilotAllowance.initialize(db, authority)


def test_control_ancestry_becoming_unsafe_closes_authority(tmp_path):
    container = tmp_path / "container"
    container.mkdir(mode=0o700)
    path, _ = activation_file(container)
    stop_path = path.parent / "stop.json"
    _stop(stop_path, False)
    assert load_activation(path)
    assert not PilotStop(stop_path).active()
    container.chmod(0o777)
    with pytest.raises(PilotAuthorityError):
        load_activation(path)
    assert PilotStop(stop_path).active()


def test_provider_state_change_after_admission_is_detected(tmp_path):
    root = tmp_path / "anchor"
    root.mkdir(mode=0o700)
    state = root / "state"
    state.mkdir(mode=0o700)
    provider = state / "provider.db"
    EventStore(provider)
    admitted = _require_provider_database(provider)
    root.chmod(0o777)
    with pytest.raises(PilotAuthorityError):
        _require_provider_database(provider)
    root.chmod(0o700)
    provider.rename(state / "saved.db")
    EventStore(provider)
    assert _require_provider_database(provider) != admitted


def test_sticky_root_temp_ancestor_is_permitted(tmp_path):
    path, _ = activation_file(tmp_path)
    assert load_activation(path)
    assert protected_ancestry(path)
    assert os.stat("/tmp").st_mode & 0o1000


def test_invalid_or_insecure_immediate_parent_and_initialization_error(
    tmp_path, monkeypatch
):
    for invalid in (None, "relative", Path("/"), tmp_path / "missing" / "state.db"):
        with pytest.raises(PilotAuthorityError):
            protected_ancestry(invalid)
    parent = tmp_path / "loose"
    parent.mkdir(mode=0o755)
    with pytest.raises(PilotAuthorityError):
        protected_ancestry(parent / "state.db")
    path, _ = activation_file(tmp_path)
    authority = load_activation(path)
    monkeypatch.setattr(
        private_pilot.os, "open", lambda *_a, **_k: (_ for _ in ()).throw(OSError())
    )
    with pytest.raises(PilotAuthorityError):
        PilotAllowance.initialize(tmp_path / "state.db", authority)


def test_ancestry_swap_during_state_or_control_read_fails_closed(tmp_path, monkeypatch):
    path, _ = activation_file(tmp_path)
    original = private_pilot.protected_ancestry
    calls = 0

    def changed(candidate):
        nonlocal calls
        calls += 1
        result = original(candidate)
        return result if calls == 1 else result + ((99, 99),)

    monkeypatch.setattr(private_pilot, "protected_ancestry", changed)
    with pytest.raises(PilotAuthorityError):
        load_activation(path)
    monkeypatch.setattr(private_pilot, "protected_ancestry", original)
    authority = load_activation(path)
    database = tmp_path / "state.db"
    PilotAllowance.initialize(database, authority)
    monkeypatch.setattr(private_pilot, "protected_ancestry", changed)
    calls = 0
    with pytest.raises(PilotAuthorityError):
        private_pilot.protected_state_identity(database)


def test_provider_identity_is_rechecked_by_live_worker_gate(tmp_path, monkeypatch):
    path, _ = activation_file(tmp_path)
    stop = path.parent / "stop.json"
    _stop(stop, False)
    allowance = tmp_path / "allowance.db"
    PilotAllowance.initialize(allowance, load_activation(path))
    provider = tmp_path / "provider.db"
    EventStore(provider)
    responses = SimpleNamespace(create=lambda **_kwargs: pytest.fail("SDK contacted"))
    monkeypatch.setattr(
        private_pilot_worker,
        "build_worker_openai_transport",
        lambda **_kwargs: OpenAIResponsesTransport(
            SimpleNamespace(responses=responses)
        ),
    )
    worker = private_pilot_worker.build_worker(
        socket_path=tmp_path / "socket",
        provider_database=provider,
        allowance_database=allowance,
        activation_path=path,
        stop_path=stop,
    )
    provider.rename(tmp_path / "old-provider.db")
    request = InferenceRequest(
        "reply:" + "a" * 64,
        "synthetic-correlation",
        MemoryModelRequest(model_request(), (), False),
    )
    assert worker._request_guard(request).outcome == "authorization_rejected"
    EventStore(provider)
    assert worker._request_guard(request).outcome == "authorization_rejected"
    assert worker._gateway._emergency_stop()
