"""Real-browser coverage for the private chat's native HTTPS login form."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import ssl
import subprocess
from threading import Thread
from wsgiref.simple_server import WSGIRequestHandler, make_server

from playwright.sync_api import sync_playwright

from app.core.context import ContextLimits, PrivateContextAssembler
from app.core.store import EventStore
from app.private_web.auth import AuthStore
from app.private_web.conversation import ConversationService, SyntheticInferenceAdapter
from app.private_web.wsgi import PrivateWebApplication, PrivateWebConfig


class QuietWSGIHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class ReferralHandler(BaseHTTPRequestHandler):
    referers = None

    def do_GET(self):
        self.referers.append(self.headers.get("Referer"))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"destination")

    def log_message(self, *args):
        pass


@contextmanager
def running(server):
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_chromium_native_login_form_and_private_referrer(tmp_path):
    cert = Path(tmp_path, "cert.pem")
    key = Path(tmp_path, "key.pem")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    auth = AuthStore(tmp_path / "auth.sqlite3")
    auth.provision_account(
        "synthetic", "synthetic test password", "synthetic-participant"
    )
    events = EventStore(tmp_path / "events.sqlite3")
    service = ConversationService(
        events,
        PrivateContextAssembler(events, ContextLimits(32_000, 20)),
        SyntheticInferenceAdapter(),
    )
    observed_origins = []
    observed_asset_referers = []

    def application(environ, start_response):
        if environ["REQUEST_METHOD"] == "POST" and environ["PATH_INFO"] == "/login":
            observed_origins.append(environ.get("HTTP_ORIGIN"))
        if environ["PATH_INFO"] == "/assets/chat.js":
            observed_asset_referers.append(environ.get("HTTP_REFERER"))
        return private_app(environ, start_response)

    with make_server(
        "127.0.0.1", 0, application, handler_class=QuietWSGIHandler
    ) as private:
        port = private.server_port
        origin = f"https://localhost:{port}"
        private_app = PrivateWebApplication(
            auth,
            service,
            PrivateWebConfig(
                allowed_hosts=frozenset({f"localhost:{port}"}),
                allowed_origins=frozenset({origin}),
            ),
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        private.socket = context.wrap_socket(private.socket, server_side=True)

        referers = []
        ReferralHandler.referers = referers
        with HTTPServer(("127.0.0.1", 0), ReferralHandler) as external:
            external.socket = context.wrap_socket(external.socket, server_side=True)
            destination = f"https://127.0.0.1:{external.server_port}/arrival"
            with running(private), running(external), sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(ignore_https_errors=True)
                    response = page.goto(f"{origin}/login")
                    assert response.status == 200
                    page.get_by_label("Username").fill("synthetic")
                    page.get_by_label("Password").fill("synthetic test password")
                    page.get_by_role("button", name="Sign in").click()
                    assert observed_origins == [origin]
                    page.wait_for_url(f"{origin}/chat")
                    assert page.get_by_text("Authenticated as synthetic").is_visible()
                    assert observed_asset_referers == [None]
                    native = browser.new_page(
                        ignore_https_errors=True, java_script_enabled=False
                    )
                    try:
                        native.context.add_cookies(page.context.cookies())
                        native.goto(f"{origin}/chat")
                        native.get_by_label("Message").fill("synthetic private draft")
                        with native.expect_response(
                            lambda reply: reply.url.startswith(origin)
                            and reply.request.method == "POST"
                        ) as reply:
                            native.get_by_role(
                                "button", name="Send", exact=True
                            ).click()
                        assert reply.value.url == f"{origin}/api/messages"
                        assert reply.value.status == 403
                        native.wait_for_url(f"{origin}/api/messages")
                        assert native.url == f"{origin}/api/messages"
                        assert (
                            page.goto(f"{origin}/api/history").json()["messages"] == []
                        )
                    finally:
                        native.close()
                    assert page.goto(f"{origin}/session").json() == {
                        "authenticated": True,
                        "username": "synthetic",
                    }
                    page.goto(f"{origin}/chat")
                    page.evaluate(
                        "url => { const link = document.createElement('a'); "
                        "link.href = url; link.textContent = 'Leave'; "
                        "document.body.append(link); }",
                        destination,
                    )
                    page.get_by_role("link", name="Leave").click()
                    page.wait_for_url(destination)
                    assert referers == [None]
                    page.goto(f"{origin}/chat")
                    page.get_by_role("button", name="Log out").click()
                    page.wait_for_url(f"{origin}/login")
                    assert page.goto(f"{origin}/session").json() == {
                        "authenticated": False
                    }
                finally:
                    browser.close()
