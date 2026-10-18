"""Native Chromium owner login remains safe when its script is unavailable."""

from contextlib import contextmanager
from pathlib import Path
import ssl
import subprocess
from wsgiref.simple_server import make_server

from playwright.sync_api import expect, sync_playwright
import pytest

from app.core.store import EventStore
from app.owner_control.store import OwnerStore
from app.owner_control.wsgi import OwnerApplication
from app.private_web.server import PrivateWSGIRequestHandler
from app.private_web.wsgi import PrivateWebConfig
from test_private_web_browser import running


@contextmanager
def owner_server(tmp_path):
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
    store = OwnerStore(tmp_path / "owner")
    store.provision("synthetic-owner", "synthetic browser password")
    events = EventStore(tmp_path / "events.db")
    provider = EventStore(tmp_path / "ipc.db")
    observed = []

    def application(environ, start_response):
        observed.append(
            (environ["REQUEST_METHOD"], environ["PATH_INFO"], environ["QUERY_STRING"])
        )
        return owner(environ, start_response)

    with make_server(
        "127.0.0.1", 0, application, handler_class=PrivateWSGIRequestHandler
    ) as server:
        origin = f"https://localhost:{server.server_port}"
        owner = OwnerApplication(
            store,
            events,
            PrivateWebConfig(
                frozenset({f"localhost:{server.server_port}"}), frozenset({origin})
            ),
            pilot_provider_events=provider,
        )
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        with running(server):
            yield origin, store, observed


def test_chromium_owner_login_stop_and_logout_have_no_credential_urls(tmp_path, capsys):
    with (
        owner_server(tmp_path) as (origin, store, observed),
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(ignore_https_errors=True)
            response = page.goto(origin)
            assert response.headers["referrer-policy"] == "no-referrer"
            page.get_by_label("Имя", exact=True).fill("synthetic-owner")
            page.get_by_label("Пароль", exact=True).fill("synthetic browser password")
            page.get_by_role("button", name="Войти", exact=True).click()
            expect(
                page.get_by_role("button", name="Снять Stop приложения")
            ).to_be_visible()
            page.on("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Снять Stop приложения").click()
            expect(page.locator("#message")).to_have_text("Stop приложения снят.")
            assert not store.active()
            page.get_by_role("button", name="Включить Stop приложения").click()
            expect(page.locator("#message")).to_have_text("Stop приложения включён.")
            assert store.active()
            page.get_by_role("button", name="Выйти").click()
            expect(page.get_by_role("button", name="Войти", exact=True)).to_be_visible()
            assert not any(
                cookie["name"] == "ar_owner_session"
                for cookie in page.context.cookies()
            )
            assert page.url.rstrip("/") == origin
        finally:
            browser.close()
    assert ("POST", "/login", "") in observed
    assert all(query == "" for _, _, query in observed)
    assert "synthetic browser password" not in "".join(capsys.readouterr())


@pytest.mark.parametrize("failure", ["disabled", "blocked", "broken"])
def test_owner_script_failure_blocks_submit_and_never_sends_credentials_in_url(
    tmp_path, failure
):
    with (
        owner_server(tmp_path) as (origin, store, observed),
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                ignore_https_errors=True, java_script_enabled=failure != "disabled"
            )
            if failure == "blocked":
                page.route("**/owner.js", lambda route: route.abort())
            if failure == "broken":
                page.route(
                    "**/owner.js",
                    lambda route: route.fulfill(
                        content_type="text/javascript",
                        body="throw new Error('synthetic initialization failure');",
                    ),
                )
            page.goto(origin)
            page.get_by_label("Имя", exact=True).fill("synthetic-owner")
            page.get_by_label("Пароль", exact=True).fill("synthetic browser password")
            expect(
                page.get_by_role("button", name="Войти", exact=True)
            ).to_be_disabled()
            page.get_by_label("Пароль", exact=True).press("Enter")
            assert page.url.rstrip("/") == origin
            assert all(path != "/login" for _, path, _ in observed)
            assert all(query == "" for _, _, query in observed)
            assert store.active()
            assert not any(
                cookie["name"] == "ar_owner_session"
                for cookie in page.context.cookies()
            )
        finally:
            browser.close()


def test_native_form_fallback_uses_post_and_remains_unauthorized(tmp_path, capsys):
    with (
        owner_server(tmp_path) as (origin, store, observed),
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(ignore_https_errors=True)
            page.route("**/owner.js", lambda route: route.abort())
            page.goto(origin)
            page.get_by_label("Имя", exact=True).fill("synthetic-owner")
            page.get_by_label("Пароль", exact=True).fill("synthetic browser password")
            with page.expect_response(
                lambda response: response.url == origin + "/login"
                and response.request.method == "POST"
            ) as response:
                page.locator("#login-form").evaluate("form => form.submit()")
            assert response.value.status == 403
            page.wait_for_url(origin + "/login")
            assert all(query == "" for _, _, query in observed)
            assert store.active()
            assert not any(
                cookie["name"] == "ar_owner_session"
                for cookie in page.context.cookies()
            )
        finally:
            browser.close()
    assert ("POST", "/login", "") in observed
    assert "synthetic browser password" not in "".join(capsys.readouterr())
