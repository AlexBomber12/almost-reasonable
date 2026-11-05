"""Real Chromium ordering against held/offline history responses, no model eval."""

import json
from pathlib import Path
import pytest

from playwright.sync_api import expect, sync_playwright

from test_agent_intellectual_state import private_setup


@pytest.mark.parametrize("account_switch", [False, True, "during_decode"])
def test_old_history_cannot_overwrite_a_later_completed_post(tmp_path, account_switch):
    _app, a, _b, _events, _ledger, _gate, sdk, server = private_setup(tmp_path)
    try:
        html = a.request("GET", "/chat")[2]
        held, turns, delayed = [], [], []
        current = {"username": "alice"}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()

                def route_request(route):
                    path = route.request.url.split("chat.test", 1)[-1]
                    if path == "/chat":
                        route.fulfill(
                            content_type="text/html; charset=utf-8", body=html
                        )
                    elif path.startswith("/assets/"):
                        name = path.rsplit("/", 1)[-1]
                        route.fulfill(
                            content_type="text/javascript"
                            if name.endswith(".js")
                            else "text/css",
                            body=Path("app/private_web", name).read_bytes(),
                        )
                    elif path == "/session":
                        route.fulfill(
                            json={
                                "authenticated": True,
                                "username": current["username"],
                            }
                        )
                    elif path == "/api/messages":
                        assert route.request.headers.get("x-chat-account") == "alice"
                        index = len(turns)
                        value = route.request.post_data_json
                        turn = [
                            {
                                "event_id": f"input-{index}",
                                "occurred_at": f"2026-11-05T12:00:0{index}+00:00",
                                "role": "participant",
                                "content": value["content"],
                            },
                            {
                                "event_id": f"reply-{index}",
                                "occurred_at": f"2026-11-05T12:00:0{index}+00:00",
                                "role": "agent",
                                "content": f"Проверенный ответ {index}",
                            },
                        ]
                        turns.append(turn)
                        if account_switch is True and index == 2:
                            delayed.append(route)
                            return
                        route.fulfill(
                            json={
                                "message_status": "created",
                                "model_state": "completed",
                                "response": turn[1]["content"],
                                "turn": turn,
                            }
                        )
                    elif path == "/api/history":
                        assert route.request.headers.get("x-chat-account") == "alice"
                        held.append(route)
                    else:
                        route.abort()

                page.route("https://chat.test/**", route_request)
                page.goto("https://chat.test/chat")
                editor = page.get_by_label("Сообщение", exact=True)
                expect(editor).to_be_enabled()
                for index in range(2):
                    editor.fill(f"Вопрос {index}")
                    with page.expect_request("**/api/history"):
                        page.get_by_role("button", name="Отправить", exact=True).click()
                    expect(
                        page.get_by_text(f"Проверенный ответ {index}", exact=True)
                    ).to_be_visible()
                    expect(editor).to_be_editable()
                assert len(held) == 2
                if account_switch:
                    if account_switch == "during_decode":
                        page.evaluate("""() => {
                          const original = window.fetch;
                          window.fetch = async (...args) => {
                            const response = await original(...args);
                            if (args[0] === '/api/messages') {
                              const decode = response.json.bind(response);
                              response.json = async () => {
                                const value = await decode();
                                window.decodeWaiting = true;
                                await new Promise(resolve => window.releaseDecoded = resolve);
                                return value;
                              };
                            }
                            return response;
                          };
                        }""")
                    editor.fill("Черновик старой страницы")
                    with page.expect_request("**/api/messages"):
                        page.get_by_role("button", name="Отправить", exact=True).click()
                    if account_switch == "during_decode":
                        page.wait_for_function("window.decodeWaiting === true")
                    current["username"] = "bob"
                    page.evaluate("window.dispatchEvent(new Event('focus'))")
                    expect(
                        page.get_by_text("Аккаунт изменился", exact=False)
                    ).to_be_visible()
                    if account_switch == "during_decode":
                        page.evaluate("window.releaseDecoded()")
                        page.wait_for_function(
                            "!document.querySelector('textarea').readOnly"
                        )
                    else:
                        assert len(delayed) == 1
                        with page.expect_response("**/api/messages"):
                            delayed[0].fulfill(
                                json={
                                    "message_status": "created",
                                    "model_state": "completed",
                                    "response": turns[2][1]["content"],
                                    "turn": turns[2],
                                }
                            )
                    for old in held:
                        old.fulfill(json={"messages": turns[0]})
                    expect(page.locator("#history .message")).to_have_count(0)
                    expect(editor).to_be_disabled()
                    assert (
                        page.evaluate("sessionStorage.getItem('ar-chat-recovery-v1')")
                        is None
                    )
                    return
                held[0].fulfill(json={"messages": turns[0]})
                expect(
                    page.get_by_text("Проверенный ответ 1", exact=True)
                ).to_be_visible()
                held[1].fulfill(
                    status=503,
                    content_type="application/json",
                    body=json.dumps({"error": "unavailable"}),
                )
                expect(
                    page.get_by_text("Не удалось обновить историю", exact=False)
                ).to_be_visible()
                expect(page.locator("#history .message")).to_have_count(4)
                assert sdk.calls == []
            finally:
                browser.close()
    finally:
        server.close()
