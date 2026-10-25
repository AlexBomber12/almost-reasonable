"""Synthetic browser checks for the complete responsive UI under its real CSP."""

from contextlib import contextmanager
from datetime import UTC, datetime
import os
from pathlib import Path
import ssl
import subprocess
from wsgiref.simple_server import make_server

from playwright.sync_api import expect, sync_playwright
import pytest

from app.private_web.wsgi import PrivateWebConfig
from test_participant_privacy_http import journey as journey_fixture
from test_private_web_browser import QuietWSGIHandler, running
from test_owner_web_browser import owner_server


@pytest.fixture
def journey(tmp_path):
    return journey_fixture.__wrapped__(tmp_path)


@contextmanager
def ui_server(journey, tmp_path):
    app = journey[-2].app
    cert, key = tmp_path / "ui-cert.pem", tmp_path / "ui-key.pem"
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
        ],
        capture_output=True,
        check=True,
    )
    with make_server("127.0.0.1", 0, app, handler_class=QuietWSGIHandler) as server:
        origin = f"https://localhost:{server.server_port}"
        app._config = PrivateWebConfig(
            frozenset({f"localhost:{server.server_port}"}), frozenset({origin})
        )
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        with running(server):
            yield origin


def login(page, origin):
    page.goto(origin + "/login")
    page.get_by_label("Имя пользователя").fill("alice")
    page.get_by_label("Пароль", exact=True).fill("synthetic-password-a")
    page.get_by_role("button", name="Войти", exact=True).click()
    page.wait_for_url(lambda url: url in {origin + "/privacy", origin + "/chat"})
    if page.url.endswith("/privacy"):
        page.get_by_role("button", name="Понятно, к разговору").click()
    page.wait_for_url(origin + "/chat")
    expect(page.get_by_label("Сообщение", exact=True)).to_be_enabled()


def screenshot(page, name):
    destination = os.environ.get("UI01_SCREENSHOT_DIR")
    if destination:
        Path(destination).mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(Path(destination) / (name + ".png")),
            full_page=page.locator("dialog[open]").count() == 0,
        )


def assert_layout(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for element in page.locator("button:visible").all():
        box = element.bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44
    assert (
        page.locator("meta[name=viewport]")
        .get_attribute("content")
        .endswith("viewport-fit=cover")
    )


def contrast_pairs(page):
    # Compute actual resolved semantic colors, including inherited system theme.
    return page.evaluate("""() => {
      const style = getComputedStyle(document.documentElement);
      const rgb = key => {
        const color = style.getPropertyValue(key).trim();
        const hex = color.slice(1); const full = hex.length === 3 ? [...hex].map(x=>x+x).join('') : hex;
        return [0,2,4].map(i=>parseInt(full.slice(i,i+2),16)/255);
      };
      const lum = key => rgb(key).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4)
        .reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
      return [['--ink','--bg'],['--ink','--surface'],['--ink','--bubble'],['--muted','--bg'],
        ['--muted','--surface'],['--muted','--surface2'],['--link','--bg'],['--danger','--bg'],
        ['--inverse','--primary'],['--inverse','--mark'],['--inverse','--danger-button'],
        ['--danger-ink','--danger-soft'],['--warn-ink','--warn-soft'],['--ok-ink','--ok-soft']]
        .map(([a,b])=>[a,b,(Math.max(lum(a),lum(b))+.05)/(Math.min(lum(a),lum(b))+.05)]);
    }""")


def test_participant_screens_themes_login_and_dialogs(journey, tmp_path):
    with ui_server(journey, tmp_path) as origin, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(ignore_https_errors=True)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "console",
                lambda message: errors.append(message.text)
                if message.type == "error" and "Content Security Policy" in message.text
                else None,
            )
            requests = []
            page.on("request", lambda request: requests.append(request.url))
            page.goto(origin + "/login")
            assert not any(
                url.endswith("/session") or url.endswith("/api/history")
                for url in requests
            )
            page.get_by_label("Имя пользователя").fill("alice")
            page.get_by_label("Пароль", exact=True).fill("wrong-password-canary")
            page.get_by_role("button", name="Показать", exact=True).click()
            expect(page.locator("#password")).to_have_attribute("type", "text")
            with page.expect_response(
                lambda r: r.url == origin + "/login" and r.request.method == "POST"
            ) as reply:
                page.get_by_role("button", name="Войти", exact=True).click()
            assert reply.value.status == 401
            expect(page.get_by_role("alert")).to_have_text("Неверное имя или пароль")
            expect(page.get_by_label("Имя пользователя")).to_have_value("alice")
            expect(page.get_by_label("Пароль", exact=True)).to_have_value("")
            # Safe next POST must work directly from the failure page, without another GET.
            page.get_by_label("Пароль", exact=True).fill("synthetic-password-a")
            page.get_by_role("button", name="Войти", exact=True).click()
            page.wait_for_url(origin + "/privacy")
            for width in (360, 390, 768, 1280):
                page.set_viewport_size({"width": width, "height": 800})
                assert_layout(page)
            screenshot(page, "onboarding-light")
            page.get_by_text("Полный текст уведомления", exact=True).click()
            assert "оператор инфраструктуры" in page.locator("main").inner_text()
            page.get_by_role("button", name="Понятно, к разговору").click()
            page.wait_for_url(origin + "/chat")
            assert page.locator('a[href^="/privacy/source"]').count() == 0
            for mode in ("light", "dark"):
                page.emulate_media(color_scheme=mode)
                assert page.evaluate(
                    "getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()"
                ) == ("#141a17" if mode == "dark" else "#f7f8f5")
                for first, second, ratio in contrast_pairs(page):
                    assert ratio >= 4.5, (mode, first, second, ratio)
            page.get_by_role("button", name="Меню", exact=True).click()
            expect(
                page.get_by_role("link", name="Приватность", exact=True)
            ).to_be_focused()
            page.keyboard.press("Escape")
            expect(page.get_by_role("button", name="Меню", exact=True)).to_be_focused()
            expect(page.locator("#chat-menu")).to_be_hidden()
            page.get_by_role("button", name="Меню", exact=True).click()
            page.get_by_role("button", name="Светлая тема", exact=True).click()
            page.reload()
            assert page.locator("html").get_attribute("data-theme") == "light"
            editor = page.get_by_label("Сообщение", exact=True)
            expect(editor).to_be_enabled()
            editor.fill(
                "Длинное синтетическое сообщение <script>текст</script>\n"
                + "слово " * 240
            )
            page.get_by_role("button", name="Отправить", exact=True).click()
            expect(page.locator("#history .message--participant")).to_have_count(1)
            expect(page.locator("#request-progress")).to_be_hidden()
            screenshot(page, "chat-long-light")
            page.get_by_role("button", name="Меню", exact=True).click()
            page.get_by_role("button", name="Тёмная тема", exact=True).click()
            page.keyboard.press("Escape")
            page.set_viewport_size({"width": 390, "height": 800})
            screenshot(page, "chat-mobile-dark")
            page.get_by_role("button", name="Меню", exact=True).click()
            page.get_by_role("button", name="Светлая тема", exact=True).click()
            page.keyboard.press("Escape")
            page.get_by_role("button", name="Меню", exact=True).click()
            page.get_by_role("link", name="Приватность", exact=True).click()
            for width in (360, 390, 768, 1280):
                page.set_viewport_size({"width": width, "height": 800})
                assert_layout(page)
            expect(page.get_by_text("Мои сообщения", exact=True)).to_be_visible()
            screenshot(page, "privacy-light")
            page.locator(".source-link").first.click()
            for width in (360, 390, 768, 1280):
                page.set_viewport_size({"width": width, "height": 800})
                assert_layout(page)
            screenshot(page, "source-desktop-light")
            page.locator(".theme-toggle").click()
            for width in (360, 390, 768, 1280):
                page.set_viewport_size({"width": width, "height": 800})
                assert_layout(page)
            page.set_viewport_size({"width": 390, "height": 800})
            screenshot(page, "source-mobile-dark")
            page.locator(".theme-toggle").click()
            expect(
                page.get_by_role("radio", name="Дословно", exact=True)
            ).to_be_checked()
            expect(page.get_by_role("radio", name="Сутки", exact=True)).to_be_checked()
            page.get_by_role("radio", name="С указанием автора", exact=True).check()
            expect(page.locator("#purpose-help")).to_contain_text("мысль ваша")
            page.get_by_role("button", name="Разрешить", exact=True).click()
            page.wait_for_url(origin + "/privacy")
            expect(page.locator("#privacy-feedback")).to_contain_text(
                "Публикация разрешена"
            )
            expect(page.get_by_text("Действует", exact=True)).to_be_visible()
            page.get_by_role("button", name="Отозвать", exact=True).click()
            expect(page.locator("#privacy-feedback")).to_have_text(
                "Разрешение отозвано"
            )
            expect(page.get_by_text("Отозвано", exact=True)).to_be_visible()
            page.locator(".source-link").first.click()
            delete = page.get_by_role("button", name="Удалить", exact=True)
            delete.click()
            dialog = page.get_by_role("dialog")
            expect(
                dialog.get_by_role("button", name="Отмена", exact=True)
            ).to_be_focused()
            page.keyboard.press("Shift+Tab")
            assert page.evaluate("document.activeElement.closest('dialog') !== null")
            page.keyboard.press("Escape")
            expect(dialog).to_be_hidden()
            expect(delete).to_be_focused()
            delete.click()
            screenshot(page, "delete-dialog-light")
            dialog.get_by_role("button", name="Удалить", exact=True).click()
            page.wait_for_url(origin + "/privacy")
            expect(page.locator("#privacy-feedback")).to_have_text("Сообщение удалено")
            expect(page.get_by_text("Удалено", exact=True)).to_be_visible()
            assert page.locator(".source-link").count() == 0
            for mode in ("light", "dark"):
                if page.locator("html").get_attribute("data-theme") != mode:
                    page.locator(".theme-toggle").click()
                for width in (360, 390, 768, 1280):
                    page.set_viewport_size({"width": width, "height": 800})
                    for path in ("/privacy", "/missing", "/login"):
                        page.goto(origin + path)
                        assert_layout(page)
                screenshot(page, "login-" + mode)
                page.goto(origin + "/missing")
                screenshot(page, "error-" + mode)
            assert not errors
        finally:
            browser.close()


def test_privacy_operation_and_chat_logout_failures(journey, tmp_path):
    privacy_failures(journey, tmp_path)


def privacy_failures(journey, tmp_path):
    with ui_server(journey, tmp_path) as origin, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(ignore_https_errors=True)
            login(page, origin)
            page.get_by_label("Сообщение", exact=True).fill("Синтетический источник")
            page.get_by_role("button", name="Отправить", exact=True).click()
            expect(page.locator("#history .message--participant")).to_have_count(1)
            page.goto(origin + "/privacy")
            page.locator(".source-link").first.click()
            operations = []
            endpoint = "**/api/privacy/grants"

            def uncertain(route):
                operations.append(route.request.post_data_json["operation_id"])
                route.fetch()
                route.abort()
                page.unroute(endpoint, uncertain)

            page.route(endpoint, uncertain)
            page.get_by_role("button", name="Разрешить", exact=True).click()
            expect(page.locator("#privacy-feedback")).to_have_text(
                "Статус операции неизвестен. Повторите попытку."
            )

            def record(route):
                operations.append(route.request.post_data_json["operation_id"])
                route.continue_()

            page.route(endpoint, record)
            page.get_by_role("button", name="Разрешить", exact=True).click()
            page.wait_for_url(origin + "/privacy")
            assert len(operations) == 2 and operations[0] == operations[1]
            page.route(
                "**/api/privacy/revocations",
                lambda route: route.fulfill(
                    status=503, body="<html>RAW-INTERNAL-ERROR</html>"
                ),
            )
            page.get_by_role("button", name="Отозвать", exact=True).click()
            expect(page.locator("#privacy-feedback")).to_have_text(
                "Не удалось выполнить. Попробуйте ещё раз."
            )
            assert "RAW-INTERNAL-ERROR" not in page.locator("body").inner_text()
            page.locator(".source-link").first.click()
            for disposition, expected in [
                ("pending", "Удаление ожидает выполнения"),
                ("incomplete_blocked", "Удаление не завершено."),
            ]:
                page.route(
                    "**/api/privacy/deletions",
                    lambda route, request, state=disposition: route.fulfill(
                        json={"status": state, "operation_id": "synthetic"}
                    ),
                )
                page.get_by_role("button", name="Удалить", exact=True).click()
                page.get_by_role("dialog").get_by_role(
                    "button", name="Удалить", exact=True
                ).click()
                page.wait_for_url(origin + "/privacy")
                expect(page.locator("#privacy-feedback")).to_contain_text(expected)
                assert (
                    "Сообщение удалено"
                    not in page.locator("#privacy-feedback").inner_text()
                )
                page.locator(".source-link").first.click()
            page.goto(origin + "/chat")
            for failure in ("rejected", "network"):
                page.route(
                    "**/logout",
                    lambda route, request, failure=failure: route.fulfill(status=503)
                    if failure == "rejected"
                    else route.abort(),
                )
                page.get_by_role("button", name="Меню", exact=True).click()
                page.get_by_role("button", name="Выйти", exact=True).click()
                expect(page.locator("#model-status")).to_contain_text(
                    "Не удалось выйти"
                    if failure == "rejected"
                    else "Выход не подтверждён"
                )
                page.keyboard.press("Escape")
            page.unroute("**/logout")
            page.evaluate(
                "() => { Storage.prototype.removeItem = () => {throw Error('unavailable')}; }"
            )
            page.get_by_role("button", name="Меню", exact=True).click()
            page.get_by_role("button", name="Выйти", exact=True).click()
            page.wait_for_url(origin + "/login")
        finally:
            browser.close()


def test_owner_tiles_and_responsive_dialogs(tmp_path):
    with (
        owner_server(tmp_path, worker_result="unavailable") as (origin, store, _),
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(ignore_https_errors=True)
            page.goto(origin)
            page.get_by_label("Имя", exact=True).fill("synthetic-owner")
            page.get_by_label("Пароль", exact=True).fill("synthetic browser password")
            page.get_by_role("button", name="Войти", exact=True).click()
            expect(page.locator("#worker-state")).to_have_text("Недоступен")
            expect(page.locator("#application-state")).to_have_text("Stop включён")
            for theme in ("light", "dark"):
                page.emulate_media(color_scheme=theme)
                for first, second, ratio in contrast_pairs(page):
                    # Owner monogram uses --ink / --bg, tested above; --bubble is still defined.
                    assert ratio >= 4.5, (theme, first, second, ratio)
                for width in (360, 390, 768, 1280):
                    page.set_viewport_size({"width": width, "height": 800})
                    assert_layout(page)
                screenshot(page, "owner-unavailable-" + theme)
            page.get_by_role("button", name="Снять Stop только приложения").click()
            expect(
                page.get_by_role("dialog").get_by_role(
                    "button", name="Отмена", exact=True
                )
            ).to_be_focused()
            page.keyboard.press("Escape")
            assert store.active()
            expect(
                page.get_by_role("button", name="Снять Stop только приложения")
            ).to_be_focused()
            page.locator("#emergency-stop").click()
            page.get_by_role("dialog").get_by_role(
                "button", name="Остановить всё", exact=True
            ).click()
            expect(page.locator("#emergency-result")).to_have_text(
                "Приложение: Подтверждено; воркер: Недоступно."
            )
            expect(page.locator("#emergency-zone")).to_be_visible()

            def fresh_stopped(route):
                data = route.fetch().json()
                data["worker"]["available"] = True
                data["worker"]["collected_at"] = datetime.now(UTC).isoformat()
                data["worker"]["stop"] = {"valid": True, "active": True}
                route.fulfill(json=data)

            page.route("**/status", fresh_stopped)
            page.get_by_role("button", name="Обновить", exact=True).click()
            expect(page.locator("#emergency-zone")).to_be_hidden()
            expect(page.locator("#worker-local-stop")).to_be_visible()
            screenshot(page, "owner-confirmed-stops")
            page.unroute("**/status")

            def stale(route):
                data = route.fetch().json()
                data["worker"]["available"] = True
                data["worker"]["collected_at"] = "2020-01-01T00:00:00+00:00"
                data["worker"]["stop"] = {"valid": True, "active": True}
                route.fulfill(json=data)

            page.route("**/status", stale)
            page.get_by_role("button", name="Обновить", exact=True).click()
            expect(page.locator("#worker-state")).to_have_text("Данные устарели")
            expect(page.locator("#emergency-zone")).to_be_visible()
        finally:
            browser.close()


def test_progress_terminal_outcomes_cancellation_and_storage_failure(journey, tmp_path):
    with ui_server(journey, tmp_path) as origin, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for outcome in (
                "no_action",
                "pending",
                "stopped",
                "rejected",
                "forbidden",
                "expired",
                "offline",
            ):
                context = browser.new_context(ignore_https_errors=True)
                page = context.new_page()
                login(page, origin)
                routes = []
                page.route("**/api/messages", lambda route: routes.append(route))
                editor = page.get_by_label("Сообщение", exact=True)
                editor.fill("Временное сообщение " + outcome)
                page.get_by_role("button", name="Отправить", exact=True).click()
                expect(page.locator(".message--optimistic")).to_have_count(1)
                expect(page.locator("#request-progress")).to_be_visible()
                expect(page.locator(".message__delivery")).to_have_text("Отправляется…")
                assert len(routes) == 1
                route = routes[0]
                if outcome == "offline":
                    route.abort()
                elif outcome in ("rejected", "forbidden", "expired"):
                    route.fulfill(
                        status={"rejected": 422, "forbidden": 403, "expired": 401}[
                            outcome
                        ],
                        body="RAW-ERROR",
                    )
                else:
                    route.fulfill(
                        json={
                            "message_status": "created",
                            "model_state": outcome,
                            "response": None,
                        }
                    )
                expect(page.locator("#request-progress")).to_be_hidden()
                expect(page.locator(".message--optimistic")).to_have_count(0)
                assert "RAW-ERROR" not in page.locator("body").inner_text()
                if outcome == "no_action":
                    expect(page.locator("#model-status")).to_contain_text(
                        "Агент решил не отвечать"
                    )
                if outcome == "offline":
                    editor.fill("Новый черновик")
                    page.get_by_role("button", name="Отменить", exact=True).click()
                    expect(editor).to_have_value("Новый черновик")
                    expect(page.locator("#model-status")).to_contain_text(
                        "операция могла выполниться"
                    )
                    expect(page.locator("#retry-actions")).to_be_hidden()
                assert len(routes) == 1
                context.close()
            context = browser.new_context(ignore_https_errors=True)
            context.add_init_script(
                "Object.defineProperty(Storage.prototype, 'getItem', {value() {throw Error('blocked')}}); Object.defineProperty(Storage.prototype, 'setItem', {value() {throw Error('blocked')}})"
            )
            page = context.new_page()
            requests = []
            page.on("request", lambda request: requests.append(request.url))
            page.goto(origin + "/login")
            page.get_by_role("button", name="Тёмная тема", exact=True).click()
            assert page.locator("html").get_attribute("data-theme") == "dark"
            assert not any(url.endswith("/session") for url in requests)
            context.close()
        finally:
            browser.close()


def test_login_expired_challenge_and_lockout_keep_safe_form(journey, tmp_path):
    journey[1]._max_login_failures = 1
    with ui_server(journey, tmp_path) as origin, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(ignore_https_errors=True)
            page.goto(origin + "/login")
            page.get_by_label("Имя пользователя").fill("alice")
            page.get_by_label("Пароль", exact=True).fill("PASSWORD-CANARY")
            page.locator("input[name=csrf]").evaluate(
                "input => {input.value='expired'}"
            )
            with page.expect_response(
                lambda r: r.url == origin + "/login" and r.request.method == "POST"
            ) as reply:
                page.get_by_role("button", name="Войти", exact=True).click()
            assert reply.value.status == 403
            expect(page.get_by_role("alert")).to_contain_text("Сессия входа устарела")
            expect(page.get_by_label("Пароль", exact=True)).to_have_value("")
            for code in (401, 429):
                page.get_by_label("Пароль", exact=True).fill("WRONG-PASSWORD-CANARY")
                with page.expect_response(
                    lambda r: r.url == origin + "/login" and r.request.method == "POST"
                ) as reply:
                    page.get_by_role("button", name="Войти", exact=True).click()
                assert reply.value.status == code
                expect(page.get_by_label("Пароль", exact=True)).to_have_value("")
                assert "WRONG-PASSWORD-CANARY" not in page.content()
                assert "PASSWORD-CANARY" not in page.url
            assert reply.value.headers["retry-after"] == str(journey[1].lockout_seconds)
            expect(page.get_by_role("alert")).to_contain_text("Слишком много попыток")
        finally:
            browser.close()


def test_owner_network_and_non_json_errors_are_safe_russian(tmp_path):
    with owner_server(tmp_path) as (origin, _store, _), sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(ignore_https_errors=True)
            page.goto(origin)
            page.get_by_label("Имя", exact=True).fill("synthetic-owner")
            page.get_by_label("Пароль", exact=True).fill("synthetic browser password")
            page.get_by_role("button", name="Войти", exact=True).click()
            expect(page.locator("#control-panel")).to_be_visible()
            page.route("**/status", lambda route: route.abort())
            page.get_by_role("button", name="Обновить", exact=True).click()
            expect(page.locator("#message")).to_have_text(
                "Не удалось связаться с панелью. Статус операции неизвестен."
            )
            page.route(
                "**/status",
                lambda route: route.fulfill(status=200, body="RAW-INTERNAL-CANARY"),
            )
            page.get_by_role("button", name="Обновить", exact=True).click()
            expect(page.locator("#message")).to_have_text(
                "Не удалось прочитать ответ. Проверьте состояние отдельно."
            )
            assert "RAW-INTERNAL-CANARY" not in page.locator("body").inner_text()
        finally:
            browser.close()


def test_dates_use_same_local_midnight_boundary_before_and_after_refresh(
    journey, tmp_path
):
    from test_participant_privacy_http import acknowledge, source_for

    clock, *_, a, _b = journey
    acknowledge(a)
    clock.timestamp = int(datetime(2026, 10, 20, 21, 30, tzinfo=UTC).timestamp())
    a.login("alice", "synthetic-password-a")
    source_for(a, "До местной полуночи")
    clock.timestamp += 3600
    a.login("alice", "synthetic-password-a")
    assert a.submit("second-date", "После местной полуночи")[0] == "201 Created"
    with ui_server(journey, tmp_path) as origin, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(ignore_https_errors=True, timezone_id="Europe/Rome")
            page.goto(origin + "/login")
            page.get_by_label("Имя пользователя").fill("alice")
            page.get_by_label("Пароль", exact=True).fill("synthetic-password-a")
            page.get_by_role("button", name="Войти", exact=True).click()
            page.wait_for_url(origin + "/chat")
            expect(page.locator("#message-content")).to_be_enabled()
            expect(page.locator(".date-separator")).to_have_count(2)
            before = page.locator(".date-separator").all_text_contents()
            page.route(
                "**/api/messages",
                lambda route: route.fulfill(
                    json={
                        "message_status": "created",
                        "model_state": "no_action",
                        "response": None,
                    }
                ),
            )
            page.get_by_label("Сообщение", exact=True).fill("Проверить даты")
            page.get_by_role("button", name="Отправить", exact=True).click()
            expect(page.locator("#request-progress")).to_be_hidden()
            assert page.locator(".date-separator").all_text_contents() == before
            assert [
                m.inner_text()
                for m in page.locator(
                    "#history .message--participant .message__text"
                ).all()
            ] == ["До местной полуночи", "После местной полуночи"]
        finally:
            browser.close()


@pytest.mark.parametrize("cause", ["revocation", "deletion"])
def test_blocked_privacy_recovery_and_logout(journey, tmp_path, monkeypatch, cause):
    from app.privacy.models import ErasureReport
    from test_participant_privacy_http import acknowledge, grant_for, post, source_for

    _clock, auth, _store, _ledger, gate, _fake, _config, a, _b = journey
    acknowledge(a)
    source = source_for(a, "BLOCKED-PRIVATE-CANARY")
    reconcile = gate.reconcile
    if cause == "revocation":
        post(a, "/api/privacy/grants", grant_for(source))
        monkeypatch.setattr(gate, "reconcile", lambda: False)
        post(
            a,
            "/api/privacy/revocations",
            {"operation_id": "revoke-blocked", "consent_id": "consent:grant-1"},
        )
        assert post(a, "/api/privacy/reconcile", {})[2] == b'{"status":"blocked"}'
        assert (
            a.app._privacy.status(auth.authenticate(a.cookies["ar_session"]))[
                "deletions"
            ]
            == []
        )
    else:
        monkeypatch.setattr(
            a.app._privacy.deletion.adapter,
            "erase",
            lambda request: ErasureReport(
                False, False, frozenset(), ("synthetic_block",)
            ),
        )
        assert (
            post(
                a,
                "/api/privacy/deletions",
                {
                    "operation_id": "delete-blocked-ui",
                    "source": source,
                    "confirm": "delete",
                },
            )[2]
            == b'{"status":"incomplete_blocked","operation_id":"delete-blocked-ui"}'
        )
    with ui_server(journey, tmp_path) as origin, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(
                ignore_https_errors=True, viewport={"width": 390, "height": 800}
            )
            page.goto(origin + "/login")
            page.get_by_label("Имя пользователя").fill("alice")
            page.get_by_label("Пароль", exact=True).fill("synthetic-password-a")
            page.get_by_role("button", name="Войти", exact=True).click()
            page.wait_for_url(origin + "/chat")
            expect(
                page.get_by_role(
                    "heading", name="Сервис временно недоступен", exact=True
                )
            ).to_be_visible()
            page.get_by_role("link", name="Приватность", exact=True).click()
            expect(page.locator("#privacy-logout")).to_be_visible()
            expect(
                page.get_by_role("button", name="Повторить проверку", exact=True)
            ).to_be_visible()
            assert "BLOCKED-PRIVATE-CANARY" not in page.locator("body").inner_text()
            for width in (360, 390, 768, 1280):
                page.set_viewport_size({"width": width, "height": 800})
                assert_layout(page)
            page.set_viewport_size({"width": 390, "height": 800})
            screenshot(page, "privacy-blocked-" + cause)
            if cause == "revocation":
                page.get_by_role(
                    "button", name="Повторить проверку", exact=True
                ).click()
                expect(page.locator("#privacy-feedback")).to_contain_text(
                    "Проверка приватности пока не завершена"
                )
            monkeypatch.setattr(gate, "reconcile", reconcile)
            page.get_by_role("button", name="Повторить проверку", exact=True).click()
            expect(page.locator("#privacy-feedback")).to_contain_text(
                "Проверка приватности завершена"
            )
            expect(page.locator("#privacy-logout")).to_have_count(0)
            assert page.goto(origin + "/chat").status == 200
            monkeypatch.setattr(gate, "_generation", None)
            page.goto(origin + "/privacy")
            token = next(
                c["value"] for c in page.context.cookies() if c["name"] == "ar_session"
            )
            page.route(
                "**/logout", lambda route: route.fulfill(status=503, body="RAW-ERROR")
            )
            page.locator("#privacy-logout").click()
            expect(page.locator("#privacy-feedback")).to_have_text(
                "Не удалось выйти. Сеанс может быть активен."
            )
            assert auth.authenticate(token) is not None
            page.route("**/logout", lambda route: route.abort())
            page.locator("#privacy-logout").click()
            expect(page.locator("#privacy-feedback")).to_have_text(
                "Выход не подтверждён. Проверьте сеанс перед уходом."
            )
            page.unroute("**/logout")
            page.locator("main").evaluate("main => {main.dataset.csrf='wrong'}")
            page.locator("#privacy-logout").click()
            expect(page.locator("#privacy-feedback")).to_have_text(
                "Не удалось выйти. Сеанс может быть активен."
            )
            assert auth.authenticate(token) is not None
            page.reload()
            page.evaluate(
                "() => {Storage.prototype.removeItem = () => {throw Error('blocked')};}"
            )
            page.locator("#privacy-logout").click()
            page.wait_for_url(origin + "/login")
            assert auth.authenticate(token) is None
            assert all(c["name"] != "ar_session" for c in page.context.cookies())
        finally:
            browser.close()
