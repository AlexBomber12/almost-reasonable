import os
import builtins
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from app.trusted import openai_credential_files as files
from app.trusted.openai_transport import (
    OPENAI_API_BASE_URL,
    TrustedTransportConfigurationError,
)


KEY = b"sk-TEST_ONLY_SYNTHETIC_KEY_123456789"


def protected_file(path: Path, content: bytes = KEY) -> Path:
    path.write_bytes(content)
    path.chmod(0o400)
    return path


def test_worker_file_factory_uses_exact_secret_and_billing_scope(tmp_path, monkeypatch):
    secret = protected_file(tmp_path / "openai-api-key")
    monkeypatch.setattr(files, "OPENAI_CREDENTIAL_PATH", secret)
    created = []

    def factory(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(responses=object())

    transport = files.build_worker_openai_transport(
        project_id="proj_synthetic",
        organization_id="org-synthetic",
        client_factory=factory,
    )

    assert created == [
        {
            "api_key": KEY.decode(),
            "base_url": OPENAI_API_BASE_URL,
            "max_retries": 0,
            "organization": "org-synthetic",
            "project": "proj_synthetic",
        }
    ]
    assert KEY.decode() not in repr(transport)


@pytest.mark.parametrize(
    ("project", "organization"),
    [
        ("wrong", "org-synthetic"),
        ("proj_synthetic", "wrong"),
        ("bad scope", "org-synthetic"),
    ],
)
def test_worker_file_factory_rejects_incorrect_billing_scope(
    tmp_path, monkeypatch, project, organization
):
    monkeypatch.setattr(
        files, "OPENAI_CREDENTIAL_PATH", protected_file(tmp_path / "secret")
    )

    with pytest.raises(TrustedTransportConfigurationError, match="billing scope"):
        files.build_worker_openai_transport(
            project_id=project,
            organization_id=organization,
            client_factory=lambda **_kwargs: object(),
        )


@pytest.mark.parametrize("content", [b"", b" key ", b"not-a-key", b"sk-short"])
def test_worker_file_factory_rejects_empty_or_malformed_secret(
    tmp_path, monkeypatch, content
):
    secret = protected_file(tmp_path / "secret", content)
    monkeypatch.setattr(files, "OPENAI_CREDENTIAL_PATH", secret)

    with pytest.raises(TrustedTransportConfigurationError):
        files.build_worker_openai_transport(
            project_id="proj_synthetic",
            organization_id="org-synthetic",
            client_factory=lambda **_kwargs: object(),
        )


def test_protected_reader_rejects_missing_symlink_wrong_mode_and_directory(
    tmp_path,
):
    missing = tmp_path / "missing"
    target = protected_file(tmp_path / "target")
    link = tmp_path / "link"
    link.symlink_to(target)
    wrong_mode = protected_file(tmp_path / "wrong-mode")
    wrong_mode.chmod(0o440)
    directory = tmp_path / "directory"
    directory.mkdir(mode=0o400)

    for path in (missing, link, wrong_mode, directory):
        with pytest.raises(TrustedTransportConfigurationError):
            files.read_protected_worker_file(path, maximum_bytes=512)


def test_protected_reader_validates_path_and_bound(tmp_path):
    with pytest.raises(TrustedTransportConfigurationError, match="path"):
        files.read_protected_worker_file(Path("relative"), maximum_bytes=1)
    with pytest.raises(TrustedTransportConfigurationError, match="bound"):
        files.read_protected_worker_file(tmp_path / "unused", maximum_bytes=0)


def test_protected_reader_rejects_unsafe_parent_mode_and_owner(tmp_path, monkeypatch):
    secret = protected_file(tmp_path / "secret")
    tmp_path.chmod(0o750)
    with pytest.raises(TrustedTransportConfigurationError, match="parent"):
        files.read_protected_worker_file(secret, maximum_bytes=512)

    tmp_path.chmod(0o700)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(TrustedTransportConfigurationError, match="parent"):
        files.read_protected_worker_file(secret, maximum_bytes=512)


def test_factory_fails_closed_when_sdk_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        files, "OPENAI_CREDENTIAL_PATH", protected_file(tmp_path / "secret")
    )
    real_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "openai":
            raise ImportError
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)

    with pytest.raises(TrustedTransportConfigurationError, match="SDK"):
        files.build_worker_openai_transport(
            project_id="proj_synthetic", organization_id="org-synthetic"
        )


def test_protected_reader_sanitizes_os_read_failure(tmp_path, monkeypatch):
    secret = protected_file(tmp_path / "secret")

    def fail_read(_descriptor, _size):
        raise OSError("sensitive path detail")

    monkeypatch.setattr(os, "read", fail_read)
    with pytest.raises(TrustedTransportConfigurationError) as caught:
        files.read_protected_worker_file(secret, maximum_bytes=512)
    assert "sensitive" not in str(caught.value)


def test_protected_reader_rejects_short_read(tmp_path, monkeypatch):
    secret = protected_file(tmp_path / "secret")
    monkeypatch.setattr(os, "read", lambda _descriptor, _size: b"short")

    with pytest.raises(TrustedTransportConfigurationError, match="unsafe"):
        files.read_protected_worker_file(secret, maximum_bytes=512)


def test_factory_imports_available_sdk_when_no_factory_is_injected(
    tmp_path, monkeypatch
):
    secret = protected_file(tmp_path / "secret")
    monkeypatch.setattr(files, "OPENAI_CREDENTIAL_PATH", secret)

    class Client:
        def __init__(self, **_kwargs):
            self.responses = object()

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=Client))
    transport = files.build_worker_openai_transport(
        project_id="proj_synthetic", organization_id="org-synthetic"
    )

    assert repr(transport) == "OpenAIResponsesTransport()"


def test_factory_sanitizes_sdk_constructor_failure(tmp_path, monkeypatch):
    secret = protected_file(tmp_path / "secret")
    monkeypatch.setattr(files, "OPENAI_CREDENTIAL_PATH", secret)

    def fail(**kwargs):
        raise RuntimeError(kwargs["api_key"])

    with pytest.raises(TrustedTransportConfigurationError) as caught:
        files.build_worker_openai_transport(
            project_id="proj_synthetic",
            organization_id="org-synthetic",
            client_factory=fail,
        )

    assert KEY.decode() not in str(caught.value)
    assert KEY.decode() not in repr(caught.value)
