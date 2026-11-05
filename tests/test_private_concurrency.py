"""Loopback-only HTTP concurrency and bounded capacity, without provider calls."""

from http.client import HTTPConnection, RemoteDisconnected
from threading import Event, Thread

import pytest
from wsgiref.simple_server import make_server

from test_private_web_browser import running


def test_a_slow_conversation_does_not_block_another_http_request():
    from app.private_web.server import PrivateWSGIServer, PrivateWSGIRequestHandler

    entered, release = Event(), Event()
    observations = []

    def app(environ, start):
        observations.append(environ["wsgi.multithread"])
        if environ["PATH_INFO"] == "/slow":
            entered.set()
            assert release.wait(5)
        start("200 OK", [("Content-Type", "text/plain")])
        return [b"ready"]

    with make_server(
        "127.0.0.1",
        0,
        app,
        server_class=PrivateWSGIServer,
        handler_class=PrivateWSGIRequestHandler,
    ) as server:
        with running(server):
            first = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            replies = []

            def slow():
                first.request("GET", "/slow")
                replies.append(first.getresponse().read())

            thread = Thread(target=slow)
            thread.start()
            try:
                assert entered.wait(5)
                other = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                other.request("GET", "/status")
                assert other.getresponse().read() == b"ready"
                other.close()
            finally:
                release.set()
                thread.join(5)
                first.close()
            assert replies == [b"ready"] and observations == [True, True]


def test_http_capacity_is_bounded_and_recovered_after_handler_completion():
    from app.private_web.server import PrivateWSGIServer, PrivateWSGIRequestHandler

    class LimitedServer(PrivateWSGIServer):
        max_threads = 1

    entered, release = Event(), Event()

    def app(_environ, start):
        entered.set()
        assert release.wait(5)
        start("200 OK", [])
        return [b"ready"]

    with make_server(
        "127.0.0.1",
        0,
        app,
        server_class=LimitedServer,
        handler_class=PrivateWSGIRequestHandler,
    ) as server:
        with running(server):
            active = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            active.request("GET", "/slow")
            assert entered.wait(5)
            excess = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            excess.request("GET", "/excess")
            try:
                with pytest.raises((RemoteDisconnected, ConnectionResetError)):
                    excess.getresponse()
            finally:
                release.set()
                excess.close()
            assert active.getresponse().read() == b"ready"
            active.close()


def test_failed_thread_start_returns_its_capacity(monkeypatch):
    from socketserver import ThreadingMixIn
    from app.private_web.server import PrivateWSGIServer

    with make_server(
        "127.0.0.1", 0, lambda _e, _s: [], server_class=PrivateWSGIServer
    ) as server:

        def failed(*_args):
            raise RuntimeError("synthetic failed thread start")

        monkeypatch.setattr(ThreadingMixIn, "process_request", failed)
        with pytest.raises(RuntimeError):
            server.process_request(None, None)
        for _ in range(server.max_threads):
            assert server._slots.acquire(blocking=False)
        assert not server._slots.acquire(blocking=False)
