"""Small standard-library WSGI application for authenticated private chat."""

from dataclasses import dataclass
from html import escape
from http import HTTPStatus
import json
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .auth import AuthenticatedSession, AuthStore, LoginStatus
from .conversation import ConversationService


SESSION_COOKIE = "ar_session"
CSRF_COOKIE = "ar_csrf"
LOGIN_CSRF_COOKIE = "ar_login_csrf"
_HOST = re.compile(r"[a-z0-9.-]+(?::[0-9]{1,5})?\Z")


@dataclass(frozen=True, slots=True)
class PrivateWebConfig:
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    secure_cookies: bool = True
    max_body_bytes: int = 16 * 1024

    def __post_init__(self) -> None:
        if not self.allowed_hosts or any(
            type(host) is not str or _HOST.fullmatch(host) is None
            for host in self.allowed_hosts
        ):
            raise ValueError("allowed_hosts must contain valid exact host values")
        if not self.allowed_origins or any(
            not _valid_origin(origin) for origin in self.allowed_origins
        ):
            raise ValueError("allowed_origins must contain absolute HTTP(S) origins")
        if type(self.max_body_bytes) is not int or self.max_body_bytes < 1:
            raise ValueError("max_body_bytes must be a positive integer")


def _valid_origin(value: object) -> bool:
    if type(value) is not str:
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )


class _RequestError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        self.status = status
        self.message = message


class PrivateWebApplication:
    """Authenticate before any participant-private persistence or retrieval."""

    def __init__(
        self,
        auth_store: AuthStore,
        conversations: ConversationService,
        config: PrivateWebConfig,
    ):
        self._auth_store = auth_store
        self._conversations = conversations
        self._config = config

    def __call__(self, environ: dict[str, Any], start_response: Any) -> list[bytes]:
        try:
            response = self._dispatch(environ)
        except _RequestError as error:
            response = self._error(error.status, error.message)
        except Exception:
            response = self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR, "The request could not be completed."
            )
        status, headers, body = response
        headers = [*self._security_headers(), *headers]
        start_response(f"{status.value} {status.phrase}", headers)
        return [body]

    def _dispatch(
        self, environ: dict[str, Any]
    ) -> tuple[HTTPStatus, list[tuple[str, str]], bytes]:
        host = environ.get("HTTP_HOST", "").lower()
        if host not in self._config.allowed_hosts:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "Invalid request host.")
        method = environ.get("REQUEST_METHOD", "")
        path = environ.get("PATH_INFO", "")
        known_paths = {
            "/login",
            "/logout",
            "/session",
            "/chat",
            "/api/history",
            "/api/messages",
            "/assets/chat.js",
        }
        if path not in known_paths:
            raise _RequestError(HTTPStatus.NOT_FOUND, "Not found.")
        allowed_methods = {
            "/login": {"GET", "POST"},
            "/logout": {"POST"},
            "/session": {"GET"},
            "/chat": {"GET"},
            "/api/history": {"GET"},
            "/api/messages": {"POST"},
            "/assets/chat.js": {"GET"},
        }
        if method not in allowed_methods[path]:
            raise _RequestError(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed.")

        if method == "GET" and path == "/login":
            return self._login_page()
        if method == "POST" and path == "/login":
            return self._login(environ)
        if method == "GET" and path == "/assets/chat.js":
            return self._javascript()

        session_token = self._cookie_value(environ, SESSION_COOKIE)
        session = self._auth_store.authenticate(session_token)
        if method == "GET" and path == "/session":
            return self._session_status(session)
        if session is None:
            raise _RequestError(
                HTTPStatus.UNAUTHORIZED,
                "Your session is invalid or expired. Please sign in again.",
            )
        if method == "GET" and path == "/chat":
            return self._chat_page(session, environ)
        if method == "GET" and path == "/api/history":
            return self._history(session)
        if method == "POST" and path == "/logout":
            self._require_state_change(environ, session)
            self._auth_store.revoke(session_token)
            return self._redirect(
                "/login",
                [
                    self._clear_cookie(SESSION_COOKIE),
                    self._clear_cookie(CSRF_COOKIE),
                ],
            )
        self._require_state_change(environ, session)
        return self._submit_message(environ, session)

    def _login_page(
        self,
    ) -> tuple[HTTPStatus, list[tuple[str, str]], bytes]:
        csrf = secrets.token_urlsafe(32)
        body = (
            "<!doctype html><html lang=en><meta charset=utf-8>"
            "<title>Private chat login</title><h1>Private chat login</h1>"
            '<form method="post" action="/login">'
            '<label>Username <input name="username" autocomplete="username"></label>'
            '<label>Password <input type="password" name="password" '
            'autocomplete="current-password"></label>'
            f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
            "<button type=submit>Sign in</button></form></html>"
        ).encode()
        return (
            HTTPStatus.OK,
            [
                ("Content-Type", "text/html; charset=utf-8"),
                self._set_cookie(LOGIN_CSRF_COOKIE, csrf, path="/login"),
            ],
            body,
        )

    def _login(
        self, environ: dict[str, Any]
    ) -> tuple[HTTPStatus, list[tuple[str, str]], bytes]:
        self._require_origin(environ)
        if self._media_type(environ) != "application/x-www-form-urlencoded":
            raise _RequestError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Unsupported content type."
            )
        body = self._read_body(environ)
        try:
            fields = parse_qs(
                body.decode("utf-8"), keep_blank_values=True, strict_parsing=True
            )
        except (UnicodeDecodeError, ValueError):
            raise _RequestError(
                HTTPStatus.BAD_REQUEST, "Malformed login request."
            ) from None
        if set(fields) != {"username", "password", "csrf"} or any(
            len(values) != 1 for values in fields.values()
        ):
            raise _RequestError(HTTPStatus.BAD_REQUEST, "Malformed login request.")
        cookie_csrf = self._cookie_value(environ, LOGIN_CSRF_COOKIE)
        if not secrets.compare_digest(fields["csrf"][0], cookie_csrf or ""):
            raise _RequestError(HTTPStatus.FORBIDDEN, "CSRF validation failed.")
        result = self._auth_store.login(
            fields["username"][0],
            fields["password"][0],
            environ.get("REMOTE_ADDR", "unknown"),
            existing_session_token=self._cookie_value(environ, SESSION_COOKIE),
        )
        if result.status is LoginStatus.LOCKED:
            return self._error(
                HTTPStatus.TOO_MANY_REQUESTS,
                "Too many login attempts. Try again later.",
                [("Retry-After", str(self._auth_store.lockout_seconds))],
            )
        if result.status is LoginStatus.INVALID:
            return self._error(HTTPStatus.UNAUTHORIZED, "Invalid username or password.")
        return self._redirect(
            "/chat",
            [
                self._set_cookie(SESSION_COOKIE, result.session_token, path="/"),
                self._set_cookie(CSRF_COOKIE, result.csrf_token, path="/"),
                self._clear_cookie(LOGIN_CSRF_COOKIE, path="/login"),
            ],
        )

    def _session_status(
        self, session: AuthenticatedSession | None
    ) -> tuple[HTTPStatus, list[tuple[str, str]], bytes]:
        payload = (
            {"authenticated": False}
            if session is None
            else {"authenticated": True, "username": session.username}
        )
        return self._json(HTTPStatus.OK, payload)

    def _chat_page(
        self, session: AuthenticatedSession, environ: dict[str, Any]
    ) -> tuple[HTTPStatus, list[tuple[str, str]], bytes]:
        csrf = self._cookie_value(environ, CSRF_COOKIE)
        if not session.accepts_csrf(csrf):
            raise _RequestError(
                HTTPStatus.UNAUTHORIZED,
                "Your session protection is invalid. Please sign in again.",
            )
        items = self._conversations.history(session.trusted_context)
        history = "".join(
            f"<li><time>{escape(item.occurred_at.isoformat())}</time> "
            f"<span>{escape(item.content)}</span></li>"
            for item in items
        )
        body = (
            "<!doctype html><html lang=en><meta charset=utf-8>"
            "<title>Private chat</title><h1>Private chat</h1>"
            f"<p>Authenticated as {escape(session.username)}</p>"
            '<p role="status">Model responses are currently unavailable or pending.</p>'
            f"<ol id=history>{history}</ol>"
            f'<form id=message-form data-csrf="{escape(csrf)}">'
            '<label>Message <textarea name="content" required maxlength="8192"></textarea></label>'
            "<button type=submit>Send</button></form>"
            '<form method="post" action="/logout" id=logout-form>'
            "<button type=submit>Log out</button></form>"
            '<script src="/assets/chat.js"></script></html>'
        ).encode()
        return HTTPStatus.OK, [("Content-Type", "text/html; charset=utf-8")], body

    def _history(
        self, session: AuthenticatedSession
    ) -> tuple[HTTPStatus, list[tuple[str, str]], bytes]:
        messages = [
            {
                "event_id": item.event_id,
                "occurred_at": item.occurred_at.isoformat(),
                "content": item.content,
            }
            for item in self._conversations.history(session.trusted_context)
        ]
        return self._json(HTTPStatus.OK, {"messages": messages})

    def _submit_message(
        self, environ: dict[str, Any], session: AuthenticatedSession
    ) -> tuple[HTTPStatus, list[tuple[str, str]], bytes]:
        if self._media_type(environ) != "application/json":
            raise _RequestError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Unsupported content type."
            )
        raw = self._read_body(environ)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _RequestError(
                HTTPStatus.BAD_REQUEST, "Malformed JSON request."
            ) from None
        if type(payload) is not dict or set(payload) != {"message_id", "content"}:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "Malformed message request.")
        try:
            result = self._conversations.submit(
                session.trusted_context, payload["message_id"], payload["content"]
            )
        except ValueError:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "Invalid message.") from None
        status = (
            HTTPStatus.CREATED
            if result.message_status.value == "created"
            else HTTPStatus.OK
        )
        return self._json(
            status,
            {
                "message_status": result.message_status.value,
                "model_state": result.model_state.value,
                "response": result.response_content,
            },
        )

    def _require_state_change(
        self, environ: dict[str, Any], session: AuthenticatedSession
    ) -> None:
        self._require_origin(environ)
        if not session.accepts_csrf(environ.get("HTTP_X_CSRF_TOKEN")):
            raise _RequestError(HTTPStatus.FORBIDDEN, "CSRF validation failed.")

    def _require_origin(self, environ: dict[str, Any]) -> None:
        if environ.get("HTTP_ORIGIN") not in self._config.allowed_origins:
            raise _RequestError(HTTPStatus.FORBIDDEN, "Origin validation failed.")

    def _read_body(self, environ: dict[str, Any]) -> bytes:
        value = environ.get("CONTENT_LENGTH")
        if value in {None, ""}:
            raise _RequestError(
                HTTPStatus.LENGTH_REQUIRED, "Content-Length is required."
            )
        try:
            length = int(value)
        except (TypeError, ValueError):
            raise _RequestError(
                HTTPStatus.BAD_REQUEST, "Invalid Content-Length."
            ) from None
        if length < 0:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length.")
        if length > self._config.max_body_bytes:
            raise _RequestError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body is too large."
            )
        body = environ["wsgi.input"].read(length)
        if len(body) != length:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "Incomplete request body.")
        return body

    @staticmethod
    def _media_type(environ: dict[str, Any]) -> str:
        return environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()

    @staticmethod
    def _cookie_value(environ: dict[str, Any], name: str) -> str | None:
        values = []
        for part in environ.get("HTTP_COOKIE", "").split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key == name:
                values.append(value)
        return values[0] if len(values) == 1 else None

    def _set_cookie(
        self, name: str, value: str | None, *, path: str
    ) -> tuple[str, str]:
        cookie = f"{name}={value}; Path={path}; HttpOnly; SameSite=Strict"
        if self._config.secure_cookies:
            cookie += "; Secure"
        return "Set-Cookie", cookie

    def _clear_cookie(self, name: str, *, path: str = "/") -> tuple[str, str]:
        cookie = f"{name}=; Path={path}; Max-Age=0; HttpOnly; SameSite=Strict"
        if self._config.secure_cookies:
            cookie += "; Secure"
        return "Set-Cookie", cookie

    @staticmethod
    def _security_headers() -> list[tuple[str, str]]:
        return [
            ("Cache-Control", "no-store"),
            ("Pragma", "no-cache"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            (
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; form-action 'self'; frame-ancestors 'none'",
            ),
        ]

    def _javascript(
        self,
    ) -> tuple[HTTPStatus, list[tuple[str, str]], bytes]:
        body = b"""'use strict';
const form = document.getElementById('message-form');
if (form) form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const content = new FormData(form).get('content');
  const response = await fetch('/api/messages', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': form.dataset.csrf},
    body: JSON.stringify({message_id: crypto.randomUUID(), content})
  });
  if (response.ok) window.location.reload();
});
const logout = document.getElementById('logout-form');
if (logout) logout.addEventListener('submit', async (event) => {
  event.preventDefault();
  await fetch('/logout', {method: 'POST', credentials: 'same-origin',
    headers: {'X-CSRF-Token': form.dataset.csrf}});
  window.location.assign('/login');
});
"""
        return HTTPStatus.OK, [("Content-Type", "text/javascript; charset=utf-8")], body

    @staticmethod
    def _json(
        status: HTTPStatus, payload: object
    ) -> tuple[HTTPStatus, list[tuple[str, str]], bytes]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        return status, [("Content-Type", "application/json; charset=utf-8")], body

    @staticmethod
    def _error(
        status: HTTPStatus,
        message: str,
        headers: list[tuple[str, str]] | None = None,
    ) -> tuple[HTTPStatus, list[tuple[str, str]], bytes]:
        body = (
            "<!doctype html><html lang=en><meta charset=utf-8>"
            f"<title>{status.value} {escape(status.phrase)}</title>"
            f"<h1>{escape(message)}</h1></html>"
        ).encode()
        return (
            status,
            [("Content-Type", "text/html; charset=utf-8"), *(headers or [])],
            body,
        )

    @staticmethod
    def _redirect(
        location: str, cookie_headers: list[tuple[str, str]]
    ) -> tuple[HTTPStatus, list[tuple[str, str]], bytes]:
        return (
            HTTPStatus.SEE_OTHER,
            [
                ("Location", location),
                *cookie_headers,
                ("Content-Type", "text/plain; charset=utf-8"),
            ],
            b"See other.",
        )
