"""Scoped application guards; not an OS sandbox or spawned-child egress proof."""

import http.client
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
