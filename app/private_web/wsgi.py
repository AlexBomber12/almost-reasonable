"""Small standard-library WSGI application for authenticated private chat."""

from dataclasses import dataclass
from contextlib import nullcontext
from html import escape
from http import HTTPStatus
from ipaddress import ip_address
import json
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from .auth import AuthenticatedSession, AuthStore, LoginStatus
from .conversation import ConversationService
from .privacy import ParticipantPrivacyControls, PrivacyControlError
from app.privacy.application import PrivacyReadBlocked


SESSION_COOKIE = "ar_session"
CSRF_COOKIE = "ar_csrf"
LOGIN_CSRF_COOKIE = "ar_login_csrf"
_DNS_HOST = re.compile(r"[a-z0-9.-]+\Z")


@dataclass(frozen=True, slots=True)
class PrivateWebConfig:
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    secure_cookies: bool = True
    max_body_bytes: int = 16 * 1024
    trusted_proxy_addresses: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.allowed_hosts or any(
            not _valid_host(host) for host in self.allowed_hosts
        ):
            raise ValueError("allowed_hosts must contain valid exact host values")
        if not self.allowed_origins or any(
            not _valid_origin(origin) for origin in self.allowed_origins
        ):
            raise ValueError("allowed_origins must contain absolute HTTP(S) origins")
        if type(self.secure_cookies) is not bool:
            raise ValueError("secure_cookies must be a boolean")
        if self.secure_cookies:
            if any(
                urlsplit(origin).scheme != "https" for origin in self.allowed_origins
            ):
                raise ValueError("secure cookie mode requires HTTPS origins")
        elif any(not _loopback_http_origin(origin) for origin in self.allowed_origins):
            raise ValueError("insecure cookie mode requires loopback HTTP origins")
        if type(self.max_body_bytes) is not int or self.max_body_bytes < 1:
            raise ValueError("max_body_bytes must be a positive integer")
        if any(
            type(address) is not str or _canonical_ip(address) != address
            for address in self.trusted_proxy_addresses
        ):
            raise ValueError("trusted_proxy_addresses must contain canonical IP values")


def _valid_host(value: object) -> bool:
    if type(value) is not str or value != value.lower():
        return False
    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port
    except ValueError:
        return False
    if (
        not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or (port is not None and port == 0)
    ):
        return False
    hostname = parsed.hostname or ""
    if ":" in hostname:
        address = ip_address(hostname)
        normalized = f"[{address}]"
    else:
        if _DNS_HOST.fullmatch(hostname) is None:
            return False
        normalized = hostname
    if port is not None:
        normalized += f":{port}"
    return value == normalized


def _valid_origin(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    default_port = {"http": 80, "https": 443}.get(parsed.scheme)
    return (
        parsed.scheme in {"http", "https"}
        and port != default_port
        and _valid_host(parsed.netloc)
        and value == f"{parsed.scheme}://{parsed.netloc}"
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )


def _canonical_ip(value: object) -> str | None:
    if type(value) is not str:
        return None
    try:
        return str(ip_address(value))
    except ValueError:
        return None


def _loopback_http_origin(value: str) -> bool:
    parsed = urlsplit(value)
    address = _canonical_ip(parsed.hostname)
    return (
        parsed.scheme == "http"
        and address is not None
        and ip_address(address).is_loopback
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
        *,
        privacy: ParticipantPrivacyControls | None = None,
    ):
        if privacy is not None and (
            privacy.auth is not auth_store
            or privacy.store is not conversations._event_store
            or getattr(conversations, "_gate", None) is not privacy.gate
        ):
            raise ValueError("privacy composition must bind the conversation")
        self._auth_store = auth_store
        self._conversations = conversations
        self._config = config
        self._privacy = privacy

    def __call__(self, environ: dict[str, Any], start_response: Any) -> list[bytes]:
        try:
            response = self._dispatch(environ)
        except _RequestError as error:
            response = self._error(error.status, error.message)
        except PrivacyReadBlocked:
            response = self._error(
                HTTPStatus.SERVICE_UNAVAILABLE
                if self._privacy is not None
                else HTTPStatus.INTERNAL_SERVER_ERROR,
                "Privacy reconciliation required.",
            )
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
        if self._privacy is not None:
            known_paths.update(
                {
                    "/privacy",
                    "/privacy/source",
                    "/api/privacy/status",
                    "/api/privacy/notice",
                    "/api/privacy/grants",
                    "/api/privacy/revocations",
                    "/api/privacy/deletions",
                    "/api/privacy/reconcile",
                    "/assets/privacy.js",
                }
            )
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
        if self._privacy is not None:
            allowed_methods.update(
                {
                    "/privacy": {"GET"},
                    "/privacy/source": {"GET"},
                    "/api/privacy/status": {"GET"},
                    "/api/privacy/notice": {"POST"},
                    "/api/privacy/grants": {"POST"},
                    "/api/privacy/revocations": {"POST"},
                    "/api/privacy/deletions": {"POST"},
                    "/api/privacy/reconcile": {"POST"},
                    "/assets/privacy.js": {"GET"},
                }
            )
        if method not in allowed_methods[path]:
            raise _RequestError(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed.")

        if method == "GET" and path == "/login":
            return self._login_page()
        if method == "POST" and path == "/login":
            return self._login(environ)
        if method == "GET" and path == "/assets/chat.js":
            return self._javascript()
        if method == "GET" and path == "/assets/privacy.js":
            return self._privacy_javascript()

        session_token = self._cookie_value(environ, SESSION_COOKIE)
        session = self._auth_store.authenticate(session_token)
        if method == "GET" and path == "/session":
            return self._session_status(session)
        if session is None:
            raise _RequestError(
                HTTPStatus.UNAUTHORIZED,
                "Your session is invalid or expired. Please sign in again.",
            )
        if self._privacy is not None and path.startswith(("/privacy", "/api/privacy")):
            return self._privacy_request(environ, session, method, path)
        if self._privacy is not None and path in {
            "/chat",
            "/api/history",
            "/api/messages",
        }:
            if not self._privacy.acknowledged(session):
                if path == "/chat":
                    return self._redirect("/privacy", [])
                raise _RequestError(
                    HTTPStatus.FORBIDDEN, "Privacy notice acknowledgement required."
                )
        if method == "GET" and path == "/chat":
            return self._chat_page(session, environ)
        if method == "GET" and path == "/api/history":
            return self._history(session, environ)
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
        client_key = self._client_key(environ)
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
            client_key,
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
        items = self._checked_history(session, environ)
        history = "".join(
            f"<li><time>{escape(item.occurred_at.isoformat())}</time> "
            f"<span>{escape(item.content)}</span>"
            + (
                f' <a href="/privacy/source?ref={quote(item.event_id, safe="")}">'
                "Управление источником</a>"
                if self._privacy is not None and item.role == "participant"
                else ""
            )
            + "</li>"
            for item in items
        )
        body = (
            "<!doctype html><html lang=en><meta charset=utf-8>"
            "<title>Private chat</title><h1>Private chat</h1>"
            f"<p>Authenticated as {escape(session.username)}</p>"
            + (
                '<nav><a href="/privacy">Приватность</a></nav>'
                if self._privacy is not None
                else ""
            )
            + '<p role="status" id="model-status">'
            "Model responses are currently unavailable or pending.</p>"
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
        self, session: AuthenticatedSession, environ: dict[str, Any]
    ) -> tuple[HTTPStatus, list[tuple[str, str]], bytes]:
        messages = [
            {
                "event_id": item.event_id,
                "occurred_at": item.occurred_at.isoformat(),
                "content": item.content,
                "role": item.role,
            }
            for item in self._checked_history(session, environ)
        ]
        return self._json(HTTPStatus.OK, {"messages": messages})

    def _session_current(self, session, environ):
        current = self._auth_store.authenticate(
            self._cookie_value(environ, SESSION_COOKIE)
        )
        return (
            current is not None and current.trusted_context == session.trusted_context
        )

    def _privacy_request(self, environ, session, method, path):
        controls = self._privacy
        if not self._session_current(session, environ):
            raise _RequestError(HTTPStatus.UNAUTHORIZED, "Session expired.")
        try:
            if method == "GET":
                if path == "/api/privacy/status":
                    result = self._json(HTTPStatus.OK, controls.status(session))
                else:
                    csrf = self._cookie_value(environ, CSRF_COOKIE)
                    if not session.accepts_csrf(csrf):
                        raise _RequestError(
                            HTTPStatus.UNAUTHORIZED, "Session protection invalid."
                        )
                    if path == "/privacy":
                        raw_query = environ.get("QUERY_STRING", "")
                        if len(raw_query) > 512:
                            raise PrivacyControlError(400)
                        try:
                            query = parse_qs(raw_query, strict_parsing=True)
                        except ValueError:
                            raise PrivacyControlError(400) from None
                        if raw_query and (
                            len(query) != 1
                            or not set(query) <= {"before", "source_before"}
                            or len(next(iter(query.values()))) != 1
                        ):
                            raise PrivacyControlError(400)
                        body = controls.page(
                            session,
                            csrf,
                            query.get("before", [None])[0],
                            query.get("source_before", [None])[0],
                        )
                    else:
                        try:
                            query = parse_qs(
                                environ.get("QUERY_STRING", ""), strict_parsing=True
                            )
                        except ValueError:
                            raise PrivacyControlError(400) from None
                        if set(query) != {"ref"} or len(query["ref"]) != 1:
                            raise PrivacyControlError(400)
                        body = controls.source_page(session, csrf, query["ref"][0])
                    result = (
                        HTTPStatus.OK,
                        [("Content-Type", "text/html; charset=utf-8")],
                        body,
                    )
            else:
                self._require_state_change(environ, session)
                fields = {
                    "/api/privacy/notice": {"version"},
                    "/api/privacy/grants": {
                        "operation_id",
                        "source",
                        "audience",
                        "purpose",
                        "validity_hours",
                    },
                    "/api/privacy/revocations": {"operation_id", "consent_id"},
                    "/api/privacy/deletions": {"operation_id", "source", "confirm"},
                    "/api/privacy/reconcile": set(),
                }[path]
                if self._media_type(environ) != "application/json":
                    raise _RequestError(
                        HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Unsupported content type."
                    )

                def unique(pairs):
                    value = {}
                    for key, item in pairs:
                        if key in value:
                            raise PrivacyControlError(400)
                        value[key] = item
                    return value

                try:
                    data = json.loads(
                        self._read_body(environ), object_pairs_hook=unique
                    )
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise PrivacyControlError(400) from None
                if type(data) is not dict or set(data) != fields:
                    raise PrivacyControlError(400)

                def current_authorized():
                    return self._session_current(session, environ)

                if path == "/api/privacy/notice":
                    payload = controls.acknowledge(
                        session, data["version"], current_authorized
                    )
                    status = HTTPStatus.OK
                elif path == "/api/privacy/grants":
                    code, payload = controls.grant(session, data, current_authorized)
                    status = HTTPStatus(code)
                elif path == "/api/privacy/revocations":
                    code, payload = controls.revoke(session, data, current_authorized)
                    status = HTTPStatus(code)
                elif path == "/api/privacy/deletions":
                    payload = controls.delete(session, data, current_authorized)
                    status = HTTPStatus.OK
                else:
                    payload = {
                        "status": "reconciled"
                        if controls.gate.reconcile()
                        else "blocked"
                    }
                    status = HTTPStatus.OK
                result = self._json(status, payload)
        except PrivacyControlError as error:
            raise _RequestError(
                HTTPStatus(error.status), "Privacy request rejected."
            ) from None
        except PrivacyReadBlocked:
            raise _RequestError(
                HTTPStatus.SERVICE_UNAVAILABLE, "Privacy reconciliation required."
            ) from None
        if not self._session_current(session, environ):
            raise _RequestError(HTTPStatus.UNAUTHORIZED, "Session expired.")
        return result

    @staticmethod
    def _privacy_javascript():
        body = """'use strict';
document.querySelectorAll('form[data-endpoint]').forEach((form) => {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    if (form.dataset.endpoint !== '/api/privacy/notice' &&
        form.dataset.endpoint !== '/api/privacy/reconcile') {
      data.operation_id = form.dataset.operationId ||= crypto.randomUUID();
    }
    if (data.validity_hours) data.validity_hours = Number(data.validity_hours);
    const response = await fetch(form.dataset.endpoint, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json',
                'X-CSRF-Token': document.querySelector('main').dataset.csrf},
      body: JSON.stringify(data)
    });
    if (!response.ok) {
      alert('Операция отклонена');
      return;
    }
    const result = await response.json();
    alert(JSON.stringify(result));
    if (form.dataset.endpoint === '/api/privacy/deletions') location.href = '/privacy';
    else location.reload();
  });
});
const logout = document.getElementById('privacy-logout');
if (logout) logout.addEventListener('click', async () => {
  const response = await fetch('/logout', {method: 'POST', credentials: 'same-origin',
    headers: {'X-CSRF-Token': document.querySelector('main').dataset.csrf}});
  if (response.ok) location.href = '/login';
  else alert('Не удалось выйти. Сессия может быть активна.');
});
""".encode()
        return HTTPStatus.OK, [("Content-Type", "text/javascript; charset=utf-8")], body

    def _checked_history(self, session, environ):
        from .runtime import Stage0ConversationRuntime

        def current_authorized():
            return self._session_current(session, environ)

        if not current_authorized():
            raise _RequestError(
                HTTPStatus.UNAUTHORIZED,
                "Your session is invalid or expired. Please sign in again.",
            )
        scope = (
            self._conversations.authenticated_request(current_authorized)
            if isinstance(self._conversations, Stage0ConversationRuntime)
            else nullcontext()
        )
        with scope:
            items = self._conversations.history(session.trusted_context)
        if not current_authorized():
            raise _RequestError(
                HTTPStatus.UNAUTHORIZED,
                "Your session is invalid or expired. Please sign in again.",
            )
        return items

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
        from .runtime import Stage0ConversationRuntime

        def current_authorized():
            return self._session_current(session, environ)

        scope = (
            self._conversations.authenticated_request(current_authorized)
            if isinstance(self._conversations, Stage0ConversationRuntime)
            else nullcontext()
        )
        try:
            with scope:
                result = self._conversations.submit(
                    session.trusted_context, payload["message_id"], payload["content"]
                )
        except ValueError:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "Invalid message.") from None
        current = self._auth_store.authenticate(
            self._cookie_value(environ, SESSION_COOKIE)
        )
        if current is None or current.trusted_context != session.trusted_context:
            raise _RequestError(
                HTTPStatus.UNAUTHORIZED,
                "Your session is invalid or expired. Please sign in again.",
            )
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

    def _client_key(self, environ: dict[str, Any]) -> str:
        remote = _canonical_ip(environ.get("REMOTE_ADDR"))
        if remote is None:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "Invalid client address.")
        if remote not in self._config.trusted_proxy_addresses:
            return remote
        forwarded = environ.get("HTTP_X_FORWARDED_FOR")
        if type(forwarded) is not str or "," in forwarded:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "Invalid forwarded client.")
        client = _canonical_ip(forwarded)
        if client is None:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "Invalid forwarded client.")
        return client

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
const modelStatus = document.getElementById('model-status');
let pendingMessage = null;
let requestInFlight = false;
if (form) form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (requestInFlight) return;
  const content = new FormData(form).get('content');
  if (!pendingMessage || pendingMessage.content !== content) {
    pendingMessage = {id: crypto.randomUUID(), content};
  }
  requestInFlight = true;
  try {
    const response = await fetch('/api/messages', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': form.dataset.csrf},
      body: JSON.stringify({message_id: pendingMessage.id, content: pendingMessage.content})
    });
    if (response.ok) {
      pendingMessage = null;
      window.location.reload();
      return;
    }
    modelStatus.textContent = 'Message was not accepted. Retry or sign in again.';
  } catch (error) {
    modelStatus.textContent = 'Delivery is uncertain. Retrying will reuse this message.';
  } finally {
    requestInFlight = false;
  }
});
const logout = document.getElementById('logout-form');
if (logout) logout.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const response = await fetch('/logout', {method: 'POST', credentials: 'same-origin',
      headers: {'X-CSRF-Token': form.dataset.csrf}});
    if (response.ok) {
      window.location.assign('/login');
      return;
    }
    modelStatus.textContent = 'Logout failed; your session is still active.';
  } catch (error) {
    modelStatus.textContent = 'Logout is uncertain; verify before leaving this device.';
  }
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
