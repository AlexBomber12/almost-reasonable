"""Scoped application guards; not an OS sandbox or spawned-child egress proof."""

import http.client
import multiprocessing
import socket
import urllib.request


def install(monkeypatch, *, local_ipc=False):
    original = socket.socket

    def controlled_socket(family=socket.AF_INET, *args, **kwargs):
        if not local_ipc or family != socket.AF_UNIX:
            raise AssertionError("acceptance forbids network sockets")
        return original(family, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("acceptance forbids live network or credentials")

    # Import SSL/HTTP classes before replacing their socket base class.
    monkeypatch.setattr(http.client.HTTPConnection, "connect", forbidden)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(socket, "socket", controlled_socket)
    for name in (
        "socket.create_connection",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.gethostbyaddr",
        "app.trusted.openai_transport.OpenAIResponsesTransport.from_environment",
        "app.trusted.openai_credential_files.read_protected_worker_file",
        "app.trusted.openai_credential_files.build_worker_openai_transport",
    ):
        monkeypatch.setattr(name, forbidden)


def _guarded_spawn_target(target, args, kwargs):
    """Install the same application guards in a fresh spawn interpreter."""
    from pytest import MonkeyPatch

    with MonkeyPatch.context() as patch:
        install(patch, local_ipc=True)
        target(*args, **kwargs)


def install_spawn_guard(monkeypatch):
    """Wrap mapped spawn targets without changing their production composition."""
    context = multiprocessing.get_context("spawn")
    original = context.Process

    def guarded_process(*, target, args=(), kwargs=None, **options):
        return original(
            target=_guarded_spawn_target,
            args=(target, args, kwargs or {}),
            **options,
        )

    monkeypatch.setattr(context, "Process", guarded_process)
