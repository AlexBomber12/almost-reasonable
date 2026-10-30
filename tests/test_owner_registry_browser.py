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
