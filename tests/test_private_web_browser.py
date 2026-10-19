"""Real-browser coverage for the private chat's native HTTPS login form."""

from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import ssl
import subprocess
from threading import Thread
from wsgiref.simple_server import WSGIRequestHandler, make_server

from playwright.sync_api import expect, sync_playwright

from app.core.context import ContextLimits, PrivateContextAssembler
from app.core.store import EventStore
from app.private_web.auth import AuthStore
from app.private_web.conversation import ConversationService, SyntheticInferenceAdapter
from app.private_web.wsgi import PrivateWebApplication, PrivateWebConfig
from app.private_web.runtime import Stage0ConversationRuntime
from app.private_web.privacy import ParticipantPrivacyControls
from app.trusted.openai_transport import FakeOpenAITransport
from test_private_conversation_runtime import response as fake_response
import test_private_conversation_runtime as runtime_tests


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
                    page.get_by_label("Имя пользователя").fill("synthetic")
                    page.get_by_label("Пароль").fill("synthetic test password")
                    page.get_by_role("button", name="Войти").click()
                    assert observed_origins == [origin]
                    page.wait_for_url(f"{origin}/chat")
                    assert page.get_by_role(
                        "heading", name="Начните разговор"
                    ).is_visible()
                    assert observed_asset_referers == [None]
                    native = browser.new_page(
                        ignore_https_errors=True, java_script_enabled=False
                    )
                    try:
                        native.context.add_cookies(page.context.cookies())
                        native.goto(f"{origin}/chat")
                        native.get_by_label("Сообщение", exact=True).fill(
                            "synthetic private draft"
                        )
                        with native.expect_response(
                            lambda reply: reply.url.startswith(origin)
                            and reply.request.method == "POST"
                        ) as reply:
                            native.get_by_role(
                                "button", name="Отправить сообщение"
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
                    page.get_by_role("button", name="Выйти").click()
                    page.wait_for_url(f"{origin}/login")
                    assert page.goto(f"{origin}/session").json() == {
                        "authenticated": False
                    }
                finally:
                    browser.close()


def test_chromium_conversation_layout_history_retry_and_stop(tmp_path):
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
    store, ledger, gate, _, _ = runtime_tests.setup.__wrapped__(tmp_path)
    fake = FakeOpenAITransport(
        [
            fake_response("reply", "Синтетический ответ <script>текст</script>"),
            fake_response("reply", "Второй синтетический ответ"),
        ]
    )
    runtime = Stage0ConversationRuntime(store, gate, fake)
    auth = AuthStore(tmp_path / "browser-auth.db")
    auth.provision_account(
        "synthetic", "synthetic test password", "synthetic-participant"
    )
    observed_ids = []

    def application(environ, start_response):
        return private_app(environ, start_response)

    with make_server(
        "127.0.0.1", 0, application, handler_class=QuietWSGIHandler
    ) as private:
        origin = f"https://localhost:{private.server_port}"
        private_app = PrivateWebApplication(
            auth,
            runtime,
            PrivateWebConfig(
                frozenset({f"localhost:{private.server_port}"}), frozenset({origin})
            ),
            privacy=ParticipantPrivacyControls(
                auth, store, ledger, gate, clock=lambda: datetime.now(UTC)
            ),
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        private.socket = context.wrap_socket(private.socket, server_side=True)
        with running(private), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    ignore_https_errors=True, viewport={"width": 1280, "height": 800}
                )
                page.goto(f"{origin}/login")
                page.get_by_label("Имя пользователя").fill("synthetic")
                page.get_by_label("Пароль").fill("synthetic test password")
                page.get_by_role("button", name="Войти").click()
                page.wait_for_url(f"{origin}/privacy")
                page.on("dialog", lambda dialog: dialog.accept())
                page.get_by_role("button", name="Подтвердить уведомление").click()
                page.get_by_text("Подтверждено", exact=False).wait_for()
                page.goto(f"{origin}/chat")
                assert page.get_by_role("heading", name="Начните разговор").is_visible()
                assert page.locator("#narrative-disclosure").is_visible()
                assert page.locator("#history li").count() == 0
                page.get_by_role("link", name="Приватность").click()
                page.wait_for_url(f"{origin}/privacy")
                page.goto(f"{origin}/chat")

                def lose_response(route):
                    observed_ids.append(route.request.post_data_json["message_id"])
                    route.fetch()
                    route.abort()
                    page.unroute("**/api/messages", lose_response)

                page.route("**/api/messages", lose_response)
                editor = page.get_by_label("Сообщение", exact=True)
                editor.fill("Первый вопрос")
                page.get_by_role("button", name="Отправить сообщение").click()
                page.get_by_text(
                    "Результат отправки неизвестен.", exact=False
                ).wait_for()
                assert editor.input_value() == "Первый вопрос"
                assert page.locator("#history li").count() == 0

                def record_request(route):
                    observed_ids.append(route.request.post_data_json["message_id"])
                    route.continue_()

                page.route("**/api/messages", record_request)
                page.get_by_role("button", name="Отправить сообщение").click()
                page.get_by_text(
                    "Синтетический ответ <script>текст</script>"
                ).wait_for()
                assert observed_ids[0] == observed_ids[1]
                assert len(fake.calls) == 1
                assert page.locator("#history li").count() == 2
                assert page.locator("#history script").count() == 0

                editor.fill("Второй вопрос")
                before_composition = len(observed_ids)
                editor.dispatch_event("keydown", {"key": "Enter", "isComposing": True})
                assert len(observed_ids) == before_composition
                editor.press("Shift+Enter")
                assert editor.input_value() == "Второй вопрос\n"
                editor.fill("Второй вопрос")
                editor.press("Enter")
                page.get_by_text("Второй синтетический ответ").wait_for()
                assert len(fake.calls) == 2
                assert [
                    part.inner_text() for part in page.locator(".message__text").all()
                ] == [
                    "Первый вопрос",
                    "Синтетический ответ <script>текст</script>",
                    "Второй вопрос",
                    "Второй синтетический ответ",
                ]
                page.reload()
                assert page.locator("#history li").count() == 4
                screenshot_dir = os.environ.get("UI01_SCREENSHOT_DIR")
                if screenshot_dir:
                    Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(Path(screenshot_dir) / "desktop.png"))
                mobile_context = browser.new_context(
                    ignore_https_errors=True,
                    viewport={"width": 390, "height": 760},
                    is_mobile=True,
                    has_touch=True,
                )
                mobile_context.add_cookies(page.context.cookies())
                mobile = mobile_context.new_page()
                mobile.goto(f"{origin}/chat")
                assert mobile.evaluate("matchMedia('(pointer: coarse)').matches")
                assert mobile.get_by_role(
                    "button", name="Отправить сообщение"
                ).is_visible()
                if screenshot_dir:
                    mobile.screenshot(
                        path=str(Path(screenshot_dir) / "mobile-emulation.png")
                    )
                mobile_context.close()
                runtime._stop = lambda: True
                editor.fill("После остановки")
                page.get_by_role("button", name="Отправить сообщение").click()
                page.get_by_text("Ответ остановлен.", exact=False).wait_for()
                expect(page.locator("#history li")).to_have_count(5)
                assert page.get_by_text("Второй синтетический ответ").is_visible()

                def reject_once(route):
                    observed_ids.append(route.request.post_data_json["message_id"])
                    route.fulfill(status=503, body="service unavailable")
                    page.unroute("**/api/messages", reject_once)

                page.route("**/api/messages", reject_once)
                editor.fill("Черновик при ошибке")
                page.get_by_role("button", name="Отправить сообщение").click()
                page.get_by_text("Не удалось отправить.", exact=False).wait_for()
                assert editor.input_value() == "Черновик при ошибке"
                page.get_by_role("button", name="Отправить сообщение").click()
                page.get_by_text("Черновик при ошибке", exact=True).wait_for()
                assert observed_ids[-2] == observed_ids[-1]

                editor.fill("я" * 5000)
                page.get_by_role("button", name="Отправить сообщение").click()
                page.get_by_text("Сообщение не принято.", exact=False).wait_for()
                assert editor.input_value() == "я" * 5000
                rejected_id = observed_ids[-1]
                editor.fill("Исправленный вопрос")
                page.get_by_role("button", name="Отправить сообщение").click()
                page.get_by_text("Исправленный вопрос", exact=True).wait_for()
                assert observed_ids[-1] != rejected_id

                long_url = "https://example.test/" + "a" * 900
                page.evaluate(
                    """async (longUrl) => {
                      for (let i = 0; i < 8; i++) {
                        const response = await fetch('/api/messages', {
                          method: 'POST', credentials: 'same-origin',
                          headers: {'Content-Type': 'application/json',
                                    'X-CSRF-Token': document.querySelector('#message-form').dataset.csrf},
                          body: JSON.stringify({message_id: crypto.randomUUID(), content: longUrl + i})
                        });
                        if (!response.ok) throw new Error('history setup failed');
                      }
                    }""",
                    long_url,
                )
                page.reload()
                assert page.locator("#history li").count() == 15
                assert page.evaluate(
                    "document.querySelector('#conversation').scrollHeight > document.querySelector('#conversation').clientHeight"
                )
                page.evaluate("document.querySelector('#conversation').scrollTop = 0")
                editor.fill("Пока читаю старые сообщения")
                page.get_by_role("button", name="Отправить сообщение").click()
                page.get_by_text("Пока читаю старые сообщения", exact=True).wait_for()
                assert (
                    page.evaluate("document.querySelector('#conversation').scrollTop")
                    < 30
                )

                for width in (360, 390, 768, 1280):
                    page.set_viewport_size({"width": width, "height": 700})
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    )
                    assert page.get_by_role(
                        "button", name="Отправить сообщение"
                    ).is_visible()
                    assert page.get_by_label("Сообщение", exact=True).is_visible()
                    assert page.evaluate(
                        "document.querySelector('#send-button').getBoundingClientRect().bottom <= innerHeight"
                    )
                page.set_viewport_size({"width": 360, "height": 350})
                assert page.get_by_role(
                    "button", name="Отправить сообщение"
                ).is_visible()
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                assert page.evaluate(
                    "document.querySelector('#send-button').getBoundingClientRect().bottom <= innerHeight"
                )
                page.get_by_role("button", name="Выйти").click()
                page.wait_for_url(f"{origin}/login")
                page.get_by_label("Имя пользователя").fill("synthetic")
                page.get_by_label("Пароль").fill("synthetic test password")
                page.get_by_role("button", name="Войти").click()
                page.wait_for_url(f"{origin}/chat")
                session = next(
                    cookie["value"]
                    for cookie in page.context.cookies()
                    if cookie["name"] == "ar_session"
                )
                auth.revoke(session)
                page.get_by_label("Сообщение", exact=True).fill(
                    "Несохранённый черновик"
                )
                page.get_by_role("button", name="Отправить сообщение").click()
                page.get_by_text("Сеанс завершён.", exact=False).wait_for()
                assert (
                    page.get_by_label("Сообщение", exact=True).input_value()
                    == "Несохранённый черновик"
                )
            finally:
                browser.close()
