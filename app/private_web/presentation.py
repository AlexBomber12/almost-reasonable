"""Small escaped HTML primitives shared by participant pages."""

from datetime import datetime
from html import escape


MONTHS_RU = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def date_label(value: datetime) -> str:
    return f"{value.day} {MONTHS_RU[value.month - 1]} {value.year}"


def document(
    title: str, content: str, *, body_class: str = "", script: str = ""
) -> bytes:
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        f"<title>{escape(title)} · Almost Reasonable</title>"
        '<link rel="stylesheet" href="/assets/chat.css">'
        '<script src="/assets/chat.js" defer></script>'
        f'{script}</head><body class="{escape(body_class)}">{content}</body></html>'
    ).encode()


def back_header(title: str, destination: str) -> str:
    return (
        '<header class="page-header">'
        f'<a class="icon-button" href="{destination}" aria-label="Назад">←</a>'
        f'<h1>{escape(title)}</h1><button class="theme-toggle" type="button">Тема</button></header>'
    )
