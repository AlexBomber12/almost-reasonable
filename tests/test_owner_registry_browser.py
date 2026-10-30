"""Configured worker results and public budgets remain clear in the owner UI."""

from pathlib import Path
from threading import Event, Thread

from playwright.sync_api import sync_playwright, expect

import test_owner_worker_registry as registry_cases
import test_finite_public_grant as grant_cases
from test_owner_web_browser import owner_server
from test_owner_public_runtime import view
from app.owner_control.worker_registry import WorkerRegistry

workers = registry_cases.workers
pending_public = grant_cases.pending_public


def test_registry_owner_ui(workers, pending_public, tmp_path):
    config, _, stops, telemetry, _ = workers
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
