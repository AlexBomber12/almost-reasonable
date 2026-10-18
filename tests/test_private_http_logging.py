"""The real HTTP server must not persist synthetic credentials or request data."""

from contextlib import closing
from http.client import HTTPConnection
from threading import Thread
from wsgiref.simple_server import make_server

import pytest

from app.private_web.server import PrivateWSGIRequestHandler


def test_request_and_error_logging_discard_sensitive_details(capsys):
    handler = PrivateWSGIRequestHandler.__new__(PrivateWSGIRequestHandler)
    handler.log_message('"%s" %s', "GET /?password=synthetic-secret HTTP/1.1", 400)
    handler.log_error("Cookie: %s", "synthetic-cookie")
    handler.get_stderr().write("synthetic-password synthetic-token traceback")
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("fail", [False, True])
def test_real_server_omits_query_headers_body_and_exception_from_logs(capsys, fail):
    marker = "synthetic-sensitive-request-marker"
    observed = []

    def application(environ, start_response):
        observed.append(environ["QUERY_STRING"])
        assert environ["HTTP_COOKIE"] == marker
        assert environ["HTTP_AUTHORIZATION"] == "Bearer " + marker
        assert (
            environ["wsgi.input"].read(int(environ["CONTENT_LENGTH"]))
            == marker.encode()
        )
        if fail:
            raise RuntimeError(marker)
        start_response("400 Bad Request", [("Content-Type", "text/plain")])
        return [b"Request rejected"]

    with make_server(
        "127.0.0.1", 0, application, handler_class=PrivateWSGIRequestHandler
    ) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with closing(
                HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            ) as client:
                client.request(
                    "POST",
                    "/login?password=" + marker,
                    body=marker,
                    headers={"Cookie": marker, "Authorization": "Bearer " + marker},
                )
                response = client.getresponse()
                assert response.status == (500 if fail else 400)
                response.read()
        finally:
            server.shutdown()
            thread.join()
    assert observed == ["password=" + marker]
    assert marker not in "".join(capsys.readouterr())
