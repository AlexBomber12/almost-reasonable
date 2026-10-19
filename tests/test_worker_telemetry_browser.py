"""Browser labels keep worker evidence separate from application control."""

from playwright.sync_api import expect, sync_playwright

from test_owner_web_browser import owner_server


def test_worker_telemetry_available_and_unavailable_rendering(tmp_path):
    with (
        owner_server(tmp_path) as (origin, _store, _observed),
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(ignore_https_errors=True)
            page.goto(origin)
            page.get_by_label("Имя", exact=True).fill("synthetic-owner")
            page.get_by_label("Пароль", exact=True).fill("synthetic browser password")
            page.get_by_role("button", name="Войти", exact=True).click()
            expect(page.locator("#status")).to_contain_text("Stop воркераНедоступен")
            expect(page.locator("#status")).to_contain_text(
                "Попытки провайдера, записанные воркеромНедоступно"
            )

            def status_with_worker(route):
                response = route.fetch()
                data = response.json()
                data["worker"] = {
                    "available": True,
                    "collected_at": "2026-10-19T12:00:00+00:00",
                    "stop": {"valid": True, "active": False},
                    "provider": {
                        "available": True,
                        "attempt_count": 0,
                        "recorded_tokens": {
                            "input_tokens": 0,
                            "cached_input_tokens": 0,
                            "output_tokens": 0,
                            "total_tokens": 0,
                        },
                        "unknown_usage_attempts": 0,
                        "unresolved_reservations": 1,
                        "spend_suspended": True,
                    },
                }
                route.fulfill(response=response, json=data)

            page.route("**/status", status_with_worker)
            page.get_by_role("button", name="Обновить").click()
            expect(page.locator("#status")).to_contain_text("Stop воркераВыключен")
            expect(page.locator("#status")).to_contain_text(
                "Попытки провайдера, записанные воркером0"
            )
            expect(page.locator("#status")).to_contain_text(
                "Попытки воркера с неизвестным или неполным учётом токенов0"
            )
            expect(page.locator("#status")).to_contain_text(
                "Резервирования воркера без окончательного результата1"
            )
            expect(page.locator("#status")).to_contain_text(
                "Фактическое использование провайдераНеизвестно: итоговые данные неполны"
            )
            expect(page.locator("#status")).to_contain_text(
                "Приостановка расходов воркераПриостановлены"
            )
        finally:
            browser.close()
