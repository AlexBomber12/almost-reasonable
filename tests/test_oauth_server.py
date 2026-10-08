import io
import os
import runpy
from pathlib import Path

import pytest

from app import oauth_server


class CapturingHandler(oauth_server.Handler):
    def __init__(self, path):
        self.path = path
        self.wfile = io.BytesIO()
        self.status = None
        self.headers = []
        self.ended_headers = False

    def send_response(self, status, message=None):
        self.status = status

    def send_header(self, name, value):
        self.headers.append((name, value))

    def end_headers(self):
        self.ended_headers = True


@pytest.fixture
def oauth_files(tmp_path, monkeypatch):
    state = tmp_path / "state"
    code = tmp_path / "code"
    monkeypatch.setattr(oauth_server, "STATE", state)
    monkeypatch.setattr(oauth_server, "CODE", code)
    monkeypatch.setattr(oauth_server.time, "time", lambda: 1_000.0)
    return state, code


def request(path):
    handler = CapturingHandler(path)
    handler.do_GET()
    return handler


def assert_response(handler, status, body):
    payload = f"{body}\n".encode()
    assert handler.status == status
    assert handler.wfile.getvalue() == payload
    assert handler.ended_headers
    assert handler.headers == [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(payload))),
        ("Cache-Control", "no-store"),
        ("Referrer-Policy", "no-referrer"),
        ("X-Content-Type-Options", "nosniff"),
    ]


@pytest.mark.parametrize(
    ("path", "status", "body"),
    [
        ("/healthz", 200, "ok"),
        ("/unknown", 404, "Not found"),
        ("/" + "x" * 4096, 414, "Request too long"),
    ],
)
def test_non_callback_routes(path, status, body):
    assert_response(request(path), status, body)


@pytest.mark.parametrize(
    "path",
    [
        "/callback",
        "/callback?state=wrong",
        "/callback?state=%ZZ",
        "/callback?" + "&".join(f"field{number}=x" for number in range(11)),
    ],
)
def test_invalid_or_malformed_state_is_rejected(oauth_files, path):
    state, _ = oauth_files
    state.write_text("expected")

    assert_response(request(path), 403, "Invalid OAuth state")
    assert state.exists()


def test_missing_state_file_is_rejected(oauth_files):
    state, _ = oauth_files

    assert_response(request("/callback?state=expected"), 403, "Invalid OAuth state")
    assert not state.exists()


def test_unreadable_state_file_is_rejected(oauth_files, monkeypatch):
    state, _ = oauth_files
    state.write_text("expected")

    def deny_read(path, *args, **kwargs):
        if path == state:
            raise PermissionError("synthetic unreadable state")
        return original_read_text(path, *args, **kwargs)

    original_read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", deny_read)

    assert_response(request("/callback?state=expected"), 403, "Invalid OAuth state")


@pytest.mark.parametrize(
    ("mtime", "contents"), [(0.0, "expected"), (1_001.0, "expected"), (1_000.0, "")]
)
def test_expired_future_dated_or_empty_state_is_rejected(oauth_files, mtime, contents):
    state, _ = oauth_files
    state.write_text(contents)
    os.utime(state, (mtime, mtime))

    assert_response(request("/callback?state=expected"), 403, "Invalid OAuth state")
    assert state.exists()


@pytest.mark.parametrize(
    "query",
    [
        "state=expected",
        "state=expected&code=",
        "state=expected&code=first&code=second",
        "state=expected&code=" + "x" * 2049,
    ],
)
def test_invalid_authorization_code_consumes_state(oauth_files, query):
    state, code = oauth_files
    state.write_text("expected")
    os.utime(state, (1_000.0, 1_000.0))

    assert_response(request(f"/callback?{query}"), 400, "Invalid authorization code")
    assert not state.exists()
    assert not code.exists()


def test_authorization_error_consumes_state(oauth_files):
    state, code = oauth_files
    state.write_text("expected")
    os.utime(state, (1_000.0, 1_000.0))

    response = request("/callback?state=expected&error=access_denied")

    assert_response(response, 400, "Authorization declined")
    assert not state.exists()
    assert not code.exists()


def test_existing_code_file_conflict_consumes_state(oauth_files):
    state, code = oauth_files
    state.write_text("expected")
    code.write_text("previous")
    os.utime(state, (1_000.0, 1_000.0))

    response = request("/callback?state=expected&code=new")

    assert_response(response, 409, "Authorization code already stored")
    assert not state.exists()
    assert code.read_text() == "previous"


def test_valid_callback_persists_code_once_with_private_permissions(oauth_files):
    state, code = oauth_files
    state.write_text("expected")
    os.utime(state, (1_000.0, 1_000.0))

    response = request("/callback?state=expected&code=synthetic-code")

    assert_response(response, 200, "Authorization received. You can close this page.")
    assert code.read_text() == "synthetic-code"
    assert code.stat().st_mode & 0o777 == 0o600
    assert not state.exists()

    assert_response(
        request("/callback?state=expected&code=second"), 403, "Invalid OAuth state"
    )
    assert code.read_text() == "synthetic-code"


def test_log_message_is_silent(oauth_files, capsys):
    handler = CapturingHandler("/healthz")

    assert handler.log_message("sensitive %s", "value") is None
    assert capsys.readouterr() == ("", "")


def test_main_starts_expected_server_without_a_listener():
    events = []

    class FakeServer:
        def __init__(self, address, handler_class):
            events.append((address, handler_class))

        def serve_forever(self):
            events.append("served")

    oauth_server.main(server_class=FakeServer)

    assert events == [(("0.0.0.0", 8000), oauth_server.Handler), "served"]


def test_script_entrypoint_starts_expected_server_without_a_listener(monkeypatch):
    events = []

    class FakeServer:
        def __init__(self, address, handler_class):
            events.append((address, handler_class.__name__))

        def serve_forever(self):
            events.append("served")

    monkeypatch.setattr("http.server.HTTPServer", FakeServer)

    runpy.run_path(oauth_server.__file__, run_name="__main__")

    assert events == [(("0.0.0.0", 8000), "Handler"), "served"]
