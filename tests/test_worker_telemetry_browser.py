"""Browser labels keep worker evidence separate from application control."""

from playwright.sync_api import expect, sync_playwright
import os
from threading import Event, Thread

from app.owner_control.worker_telemetry import WorkerTelemetryClient
from app.trusted.openai_gateway import GatewayOutcome
from app.trusted.openai_transport import OpenAITransportError
from app.trusted.worker_telemetry import WorkerTelemetryServer
from test_openai_gateway import config, gateway, request
from test_worker_telemetry import sources

from test_owner_web_browser import owner_server


def test_gateway_rate_limit_renders_specific_safe_owner_diagnosis(tmp_path):
    _, stop, path = sources(tmp_path)
    instance, transport, provider = gateway(
        tmp_path,
        [
            OpenAITransportError(
                status_code=429,
                error_type="rate_limit_error",
                error_code="rate_limit_exceeded",
            )
        ],
        provider_config=config(max_attempts=1),
    )
    assert (
        instance.submit(
            request(),
            correlation_id="browser-correlation",
            operation_id="browser-operation",
        ).outcome
        is GatewayOutcome.RATE_LIMIT
    )
    assert len(transport.calls) == 1
    assert provider.suspension() is None

    worker = WorkerTelemetryServer(
        path, tmp_path / "events.sqlite3", stop, frozenset({os.geteuid()})
    )
    worker.start()
    stopped = Event()
    thread = Thread(target=worker.serve, kwargs={"should_stop": stopped.is_set})
    thread.start()
    try:
        with (
            owner_server(
                tmp_path, worker_telemetry=WorkerTelemetryClient(path, os.geteuid())
            ) as (origin, _store, _observed),
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(ignore_https_errors=True)
                page.goto(origin)
                page.get_by_label("Имя", exact=True).fill("synthetic-owner")
                page.get_by_label("Пароль", exact=True).fill(
                    "synthetic browser password"
                )
                page.get_by_role("button", name="Войти", exact=True).click()
                page.locator("#recent-activity summary").click()
                expect(page.locator("#recent-worker")).to_contain_text(
                    "Ограничение частоты запросов провайдера"
                )
                expect(page.locator("#recent-worker")).not_to_contain_text(
                    "Лимит расходов"
                )
                assert "rate_limit_exceeded" not in page.locator("body").inner_text()
                assert len(transport.calls) == 1
                assert provider.suspension() is None
            finally:
                browser.close()
    finally:
        stopped.set()
        thread.join(timeout=2)
        worker.close()


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

            large_total = [False]

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
                            "input_tokens": "9007199254740993"
                            if large_total[0]
                            else "0",
                            "cached_input_tokens": "0",
                            "output_tokens": "0",
                            "total_tokens": "9007199254740993"
                            if large_total[0]
                            else "0",
                        },
                        "unknown_usage_attempts": 0,
                        "unresolved_reservations": 1,
                        "spend_suspended": True,
                    },
                }
                data["recent"] = {
                    "application": {
                        "available": True,
                        "rows": [
                            {
                                "time": "2026-10-20T00:00:00.000000Z",
                                "family": "private_reply",
                                "outcome": "rejected",
                                "reason": "<script>PRIVATE-CANARY</script>",
                                "tokens": None,
                                "reference": "abcdef0123456789abcdef01",
                            }
                        ],
                    },
                    "worker": {
                        "available": True,
                        "rows": [
                            {
                                "time": "2026-10-20T00:00:00.000000Z",
                                "family": "provider_attempt",
                                "outcome": "reservation",
                                "reason": None,
                                "tokens": None,
                                "reference": "abcdef0123456789abcdef01",
                            }
                        ],
                    },
                }
                route.fulfill(response=response, json=data)

            page.route("**/status", status_with_worker)
            page.get_by_role("button", name="Обновить").click()
            expect(page.locator("#status")).to_contain_text("Stop воркераВыключен")
            expect(page.locator("#latest-outcome")).to_contain_text(
                "Предложение отклонено"
            )
            page.locator("#recent-activity summary").click()
            expect(page.locator("#recent-application")).to_contain_text(
                "Предложение отклонено"
            )
            expect(page.locator("#recent-worker")).to_contain_text(
                "Резервирование без результата"
            )
            assert "PRIVATE-CANARY" not in page.locator("#recent-activity").inner_text()
            assert page.locator("#recent-activity script").count() == 0
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
            large_total[0] = True
            page.get_by_role("button", name="Обновить").click()
            expect(page.locator("#status")).to_contain_text(
                "Всего записанных токенов воркера9007199254740993"
            )
        finally:
            browser.close()
