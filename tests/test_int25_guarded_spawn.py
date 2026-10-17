"""The mapped INT-23 spawn wrapper enforces offline rules in child interpreters."""

import multiprocessing
import socket
import urllib.request

from acceptance.guards import install_spawn_guard
from app.trusted.openai_transport import OpenAIResponsesTransport


def _probe_child(pipe):
    checks = (
        lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
        lambda: socket.getaddrinfo("example.org", 443),
        lambda: urllib.request.urlopen("https://example.org"),
        lambda: OpenAIResponsesTransport.from_environment(),
    )
    blocked = []
    for check in checks:
        try:
            check()
        except AssertionError:
            blocked.append(True)
        else:
            blocked.append(False)
    local = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    local.close()
    pipe.send((blocked, local.fileno()))
    pipe.close()


def test_spawned_mapped_worker_has_offline_guards(monkeypatch):
    install_spawn_guard(monkeypatch)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_probe_child, args=(child,))
    process.start()
    child.close()
    try:
        assert parent.poll(10)
        assert parent.recv() == ([True] * 4, -1)
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
