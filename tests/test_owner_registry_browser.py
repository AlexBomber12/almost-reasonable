"""Configured worker results and public budgets remain clear in the owner UI."""

from pathlib import Path
from threading import Event, Thread
import json

import pytest

from playwright.sync_api import sync_playwright, expect

import test_owner_worker_registry as registry_cases
import test_finite_public_grant as grant_cases
from test_owner_web_browser import owner_server
from test_owner_public_runtime import view
from app.owner_control.worker_registry import WorkerRegistry

workers = registry_cases.workers
pending_public = grant_cases.pending_public


@pytest.mark.parametrize("secondary", ["active", "unavailable", "stale", "invalid"])
def test_partial_worker_stop_keeps_emergency_control_available(
    workers, tmp_path, monkeypatch, secondary
):
    config, _, stops, telemetry, controls = workers
    value = json.loads(controls[0].read_text()) | {"stop": True}
    controls[0].chmod(0o600)
    controls[0].write_text(json.dumps(value))
    controls[0].chmod(0o400)
    registry = WorkerRegistry.from_config(config)
    read, incomplete = registry.read, [True]

    def observe():
        rows = read()
        target = next(row["status"] for row in rows if row["name"] == "public-model")
        if incomplete[0] and secondary == "stale":
            target["collected_at"] = "2000-01-01T00:00:00+00:00"
        if incomplete[0] and secondary == "invalid":
            target["stop"] = {"valid": False, "active": None}
        return rows

    monkeypatch.setattr(registry, "read", observe)
    stop_event = Event()
    threads = []

    def start(server):
        thread = Thread(target=server.serve, kwargs={"should_stop": stop_event.is_set})
        thread.start()
        threads.append(thread)

    for index, server in enumerate(telemetry):
        if index != 1 or secondary != "unavailable":
            start(server)
    for server in stops:
        start(server)
    try:
        with (
            owner_server(tmp_path, workers=registry) as (origin, _, _),
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
                expect(page.locator("#worker-state")).to_have_text("Stop включён")
                expect(page.locator("#emergency-zone")).to_be_visible()
                expect(page.locator("#worker-local-stop")).to_be_hidden()
                page.locator("#emergency-stop").click()
                page.locator("#dialog-confirm").click()
                expect(page.locator("#emergency-result")).to_contain_text(
                    "public-model"
                )
                assert all(json.loads(path.read_text())["stop"] for path in controls), (
                    page.locator("#emergency-result").inner_text()
                )
                if secondary != "active":
                    expect(page.locator("#emergency-zone")).to_be_visible()
                    if secondary == "unavailable":
                        start(telemetry[1])
                    incomplete[0] = False
                    page.locator("#refresh").click()
                try:
                    expect(page.locator("#emergency-zone")).to_be_hidden(timeout=10000)
                except AssertionError:
                    raise AssertionError(
                        page.evaluate(
                            "snapshot.workers.map(entry => ({name:entry.name,status:entry.status}))"
                        )
                    )
                expect(page.locator("#worker-local-stop")).to_be_visible()
            finally:
                browser.close()
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=3)
            assert not thread.is_alive()


@pytest.mark.parametrize(
    "research_codes",
    [
        ("research_citation_budget", "research_annotation_budget"),
        ("research_citation_conflict", "research_assessment_json"),
        ("research_assessment_fields", "research_assessment_source"),
    ],
)
def test_registry_owner_ui(workers, pending_public, tmp_path, research_codes):
    from dataclasses import replace
    from app.core.provider_spend import ProviderStateStore
    from app.core.store import EventStore
    from test_worker_telemetry import attempt

    config, _, stops, telemetry, _ = workers
    from app.trusted.public_monthly_budget import MonthlyBudget

    budget_path = telemetry[2].provider_db.with_name("observed-budget.sqlite")
    MonthlyBudget.initialize(
        budget_path, lane="research", project_id="proj_SYNTHETIC_CANARY"
    )
    budget = MonthlyBudget(
        budget_path, lane="research", project_id="proj_SYNTHETIC_CANARY"
    )
    assert budget.reserve("synthetic-unknown-search")
    telemetry[2].monthly_budget_path = budget_path
    store = ProviderStateStore(EventStore(telemetry[2].provider_db))
    store.record_attempt(
        replace(
            attempt("diagnostic"),
            outcome="invalid_response",
            provider_error_code="research_output_truncated",
        )
    )
    research_labels = {
        "research_citation_budget": "Превышен лимит разных источников поиска",
        "research_annotation_budget": "Превышен лимит цитат в ответе поиска",
        "research_citation_conflict": "Повторные цитаты содержат разные сведения об источнике",
        "research_assessment_json": "Исследовательский вывод содержит некорректный JSON",
        "research_assessment_fields": "Исследовательский вывод содержит некорректные поля",
        "research_assessment_source": "Вывод ссылается на неизвестный или неоднозначный источник",
    }
    for index, code in enumerate(research_codes):
        store.record_attempt(
            replace(
                attempt("research-diagnostic-" + str(index)),
                outcome="invalid_response",
                provider_error_code=code,
            )
        )
    public_store = ProviderStateStore(EventStore(telemetry[1].provider_db))
    public_labels = {
        "public_json_invalid": "Публичная модель вернула некорректный JSON",
        "public_proposal_invalid": "Некорректные поля публичного действия",
    }
    for index, code in enumerate((*public_labels, "PRIVATE-CANARY-UNKNOWN-CODE")):
        public_store.record_attempt(
            replace(
                attempt("public-diagnostic-" + str(index)),
                outcome="provider_error",
                provider_error_code=code,
            )
        )
    registry = WorkerRegistry.from_config(config)
    stop_event = Event()
    threads = [
        Thread(target=server.serve, kwargs={"should_stop": stop_event.is_set})
        for server in telemetry
    ]
    for thread in threads:
        thread.start()
    try:
        with (
            owner_server(
                tmp_path, workers=registry, public_runtime=view(pending_public[0])
            ) as (origin, _, _),
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    ignore_https_errors=True, viewport={"width": 1280, "height": 900}
                )
                page.goto(origin)
                page.get_by_label("Имя", exact=True).fill("synthetic-owner")
                page.get_by_label("Пароль", exact=True).fill(
                    "synthetic browser password"
                )
                page.get_by_role("button", name="Войти", exact=True).click()
                page.locator("#diagnostics summary").click()
                expect(page.locator("#status")).to_contain_text(
                    "public-model: записанные токены"
                )
                expect(page.locator("#status")).to_contain_text("Публичное разрешение")
                expect(page.locator("#status")).to_contain_text(
                    "Вывод поиска обрезан по лимиту токенов"
                )
                expect(page.locator("#status")).to_contain_text(
                    "Публичные исследования"
                )
                expect(page.locator("#status")).to_contain_text(
                    "Публичное развитие: обработка"
                )
                expect(page.locator("#status")).to_contain_text(
                    "Это журнал, а не число действующих убеждений"
                )
                expect(page.locator("#status")).to_contain_text(
                    "Публичный опрос: Комментарии к постам"
                )
                expect(page.locator("#status")).to_contain_text(
                    "Попытки опроса не наблюдались"
                )
                expect(page.locator("#status")).to_contain_text(
                    "Ограничения и сбои поиска"
                )
                expect(page.locator("#status")).to_contain_text(
                    "удержано $0.70 (1 незавершённых попыток)"
                )
                expect(page.locator("#status")).to_contain_text("1/100 попыток")
                expect(page.locator("#status")).to_contain_text(
                    "Нет наблюдения кошелька"
                )
                for label in public_labels.values():
                    expect(page.locator("#status")).to_contain_text(label)
                for code in research_codes:
                    expect(page.locator("#status")).to_contain_text(
                        research_labels[code]
                    )
                expect(page.locator("#status")).to_contain_text("Ошибка провайдера")
                assert (
                    "PRIVATE-CANARY-UNKNOWN-CODE"
                    not in page.locator("body").inner_text()
                )
                serving = registry_cases.serving(stops)
                page.locator("#emergency-stop").click()
                page.locator("#dialog-confirm").click()
                expect(page.locator("#emergency-result")).to_contain_text(
                    "hosted-research"
                )
                registry_cases.finished(serving)
                assert "synthetic/" not in page.locator("body").inner_text()
                page.set_viewport_size({"width": 420, "height": 900})
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                destination = (
                    Path(__file__).resolve().parents[1] / ".artifacts" / "pr8-owner.png"
                )
                destination.parent.mkdir(exist_ok=True)
                page.screenshot(path=str(destination), full_page=True)
            finally:
                browser.close()
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=3)
            assert not thread.is_alive()
