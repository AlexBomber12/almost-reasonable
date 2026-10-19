"""Small JSON owner interface with independent cookies and strict admission."""

from dataclasses import asdict
from http import HTTPStatus
import json
from importlib.resources import files
import secrets

from app.core.provider_spend import ProviderStateStore
from app.worker_telemetry.protocol import unavailable
from app.private_web.auth import LoginStatus
from app.private_web.wsgi import PrivateWebApplication, _RequestError, _canonical_ip


class OwnerApplication:
    """Server-side authentication precedes inspection and administrative input.

    Reuses bounded HTTP parsing and proxy validation, never conversational routes.
    Privacy reconciliation remains disabled: the existing snapshot API is unbounded
    and returns protected references. No privacy mutation authority is invented.
    """

    def __init__(
        self,
        store,
        events,
        config,
        *,
        privacy_ledger=None,
        allowed_clients=frozenset({"127.0.0.1", "::1"}),
        pilot_provider_events=None,
        worker_telemetry=None,
        worker_stop=None,
    ):
        if (
            type(allowed_clients) is not frozenset
            or not 0 < len(allowed_clients) <= 16
            or any(
                type(address) is not str or _canonical_ip(address) != address
                for address in allowed_clients
            )
        ):
            raise ValueError(
                "owner admission requires 1–16 exact canonical client addresses"
            )
        self._allowed_clients = allowed_clients
        if not config.secure_cookies:
            raise ValueError("owner interface requires secure cookies")
        self.store = store
        self.events = events
        self.config = config
        self.privacy = privacy_ledger
        self.pilot_provider_events = pilot_provider_events
        self.worker_telemetry = worker_telemetry
        self.worker_stop = worker_stop
        self._http = PrivateWebApplication(None, None, config)

    def __call__(self, environ, start_response):
        try:
            if self.pilot_provider_events is not None and environ.get("PATH_INFO") in {
                "/",
                "/owner.js",
                "/owner.css",
            }:
                return self._asset(environ, start_response)
            status, payload, cookies = self._dispatch(environ)
        except _RequestError as error:
            status, payload, cookies = error.status.value, {"error": error.message}, []
        except (ValueError, PermissionError):
            status, payload, cookies = 400, {"error": "Invalid owner request."}, []
        except Exception:
            status, payload, cookies = 503, {"error": "Owner control unavailable."}, []
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = (
            self._http._security_headers()
            + [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
            + cookies
        )
        start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [body]

    def _asset(self, env, start_response):
        # Static resources carry no authority and cannot read owner state.
        self._admit(env)
        if env.get("REQUEST_METHOD") != "GET":
            raise _RequestError(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed.")
        path = env["PATH_INFO"]
        name, content_type = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/owner.js": ("owner.js", "text/javascript; charset=utf-8"),
            "/owner.css": ("owner.css", "text/css; charset=utf-8"),
        }[path]
        body = files("app.owner_control.static").joinpath(name).read_bytes()
        start_response(
            "200 OK",
            self._http._security_headers()
            + [("Content-Type", content_type), ("Content-Length", str(len(body)))],
        )
        return [body]

    def _admit(self, env):
        if env.get("HTTP_HOST") not in self.config.allowed_hosts or env.get(
            "QUERY_STRING", ""
        ):
            raise _RequestError(HTTPStatus.BAD_REQUEST, "Invalid request target.")
        if self._http._client_key(env) not in self._allowed_clients:
            raise _RequestError(HTTPStatus.FORBIDDEN, "Client admission denied.")

    def _dispatch(self, env):
        self._admit(env)
        client = self._http._client_key(env)
        path, method = env.get("PATH_INFO"), env.get("REQUEST_METHOD")
        routes = {
            "/login": {"GET", "POST"},
            "/logout": {"POST"},
            "/status": {"GET", "POST"},
            "/operation": {"POST"},
            "/stop": {"POST"},
            "/worker-stop-operation": {"POST"},
            "/emergency-stop": {"POST"},
        }
        if path not in routes:
            raise _RequestError(HTTPStatus.NOT_FOUND, "Not found.")
        if method not in routes[path]:
            raise _RequestError(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed.")
        token = self._http._cookie_value(env, "ar_owner_session")
        if path == "/login":
            if method == "GET":
                csrf = self.store.login_challenge(client)
                return (
                    200,
                    {"csrf": csrf},
                    [self._http._set_cookie("ar_owner_login", csrf, path="/login")],
                )
            self._http._require_origin(env)
            payload = self._body(env, {"username", "password", "csrf"})
            cookie = self._http._cookie_value(env, "ar_owner_login")
            if (
                type(payload["csrf"]) is not str
                or not cookie
                or not secrets.compare_digest(payload["csrf"].encode(), cookie.encode())
                or not self.store.consume_login_challenge(payload["csrf"], client)
            ):
                raise _RequestError(HTTPStatus.FORBIDDEN, "Invalid CSRF.")
            result = self.store.login(payload["username"], payload["password"], client)
            if result.status is not LoginStatus.SUCCESS:
                return (
                    (429 if result.status is LoginStatus.LOCKED else 401),
                    {"error": "Login failed."},
                    [],
                )
            return (
                200,
                {"authenticated": True, "csrf": result.csrf_token},
                [
                    self._http._set_cookie(
                        "ar_owner_session", result.session_token, path="/"
                    ),
                    self._http._clear_cookie("ar_owner_login", path="/login"),
                ],
            )
        session = self.store.authenticate(token)
        if session is None:
            raise _RequestError(
                HTTPStatus.UNAUTHORIZED, "Owner authentication required."
            )
        if path == "/status":
            self._http._require_origin(env)
            provider = ProviderStateStore(self.pilot_provider_events or self.events)
            suspension = provider.suspension()
            usage = None
            if suspension is not None:
                record = provider.usage_attempt(suspension.attempt_id)
                usage = {
                    key: getattr(record, key)
                    for key in (
                        "input_tokens",
                        "cached_input_tokens",
                        "output_tokens",
                        "total_tokens",
                        "cost_estimate_status",
                        "estimated_cost",
                        "currency",
                    )
                }
            status = (
                {
                    "scope": "private_pilot_application",
                    "application_stop": {
                        "active": self.store.active(),
                        "scope": "application_only",
                    },
                    "application_provider": {
                        "spend_suspended": suspension is not None,
                        "suspension_usage": usage,
                        "attempt_count": provider.usage_attempt_count(),
                        "usage_evidence": "unavailable_bounded_listing",
                        "billing_authority": False,
                        "scope": "application_ipc_state",
                    },
                    "worker": (
                        self.worker_telemetry.read()
                        if self.worker_telemetry is not None
                        else unavailable()
                    ),
                    "emergency_operation": self.store.latest_worker_stop(token),
                    "event_store": {
                        "scope": "conversation_event_store",
                        "health": "read_interface_ok",
                        **asdict(self.events.diagnostics()),
                    },
                    "privacy": {
                        "ledger_ready": self.privacy is not None
                        and type(self.privacy.control_generation()) is int,
                        "reconciliation": "unavailable_bounded_public_interface",
                    },
                    "audit": self.store.audit(),
                }
                if self.pilot_provider_events is not None
                else None
            )
            if status is not None:
                worker_provider = status["worker"]["provider"]
                if worker_provider["available"]:
                    worker_provider["recorded_tokens"] = {
                        key: str(value)
                        for key, value in worker_provider["recorded_tokens"].items()
                    }
                return 200, status, []
            return (
                200,
                {
                    "stage": 0,
                    "live_effects": dict.fromkeys(
                        (
                            "inference",
                            "research",
                            "publishing",
                            "enrollment",
                            "privacy_mutations",
                        ),
                        False,
                    ),
                    "stop_active": self.store.active(),
                    "stop_integration": "staged_not_global",
                    "provider": {
                        "spend_suspended": suspension is not None,
                        "suspension_usage": usage,
                        "attempt_count": provider.usage_attempt_count(),
                        "usage_evidence": "unavailable_bounded_listing",
                        "billing_authority": False,
                    },
                    "event_store": {
                        "health": "read_interface_ok",
                        **asdict(self.events.diagnostics()),
                    },
                    "privacy": {
                        "ledger_ready": self.privacy is not None
                        and type(self.privacy.control_generation()) is int,
                        "reconciliation": "unavailable_bounded_public_interface",
                        "application_erasure_integrated": False,
                    },
                    "audit": self.store.audit(),
                },
                [],
            )
        try:
            return self._mutation(env, path, token, session)
        except (_RequestError, ValueError, PermissionError):
            self.store.reject(token, path)
            raise

    def _mutation(self, env, path, token, session):
        self._http._require_origin(env)
        if not session.accepts_csrf(env.get("HTTP_X_CSRF_TOKEN")):
            raise _RequestError(HTTPStatus.FORBIDDEN, "Invalid CSRF.")
        if path == "/operation":
            return 200, {"operation_id": self.store.issue(token)}, []
        if path == "/worker-stop-operation":
            if self.worker_stop is None or self.pilot_provider_events is None:
                raise _RequestError(
                    HTTPStatus.SERVICE_UNAVAILABLE, "Worker control unavailable."
                )
            return 200, {"operation_id": self.store.issue_worker_stop(token)}, []
        if path == "/logout":
            self.store.revoke(token)
            return (
                200,
                {"authenticated": False},
                [self._http._clear_cookie("ar_owner_session")],
            )
        if path == "/emergency-stop":
            payload = self._body(env, {"operation_id"})
            operation = payload["operation_id"]
            outcome = self.store.worker_operation(token, operation)
            if outcome["application"] != "confirmed":
                try:
                    self.store.assert_application_stop(token, operation)
                    application = "confirmed"
                except (RuntimeError, OSError, PermissionError):
                    application = "unavailable"
                try:
                    self.store.record_worker_outcome(
                        token, operation, "application_result", application
                    )
                except (RuntimeError, OSError, PermissionError):
                    application = (
                        "uncertain" if application == "confirmed" else "unavailable"
                    )
            else:
                application = "confirmed"
            if outcome["worker"] != "confirmed":
                worker = (
                    self.worker_stop.assert_stop()
                    if self.worker_stop is not None
                    else "unavailable"
                )
                try:
                    self.store.record_worker_outcome(
                        token, operation, "worker_result", worker
                    )
                except (RuntimeError, OSError, PermissionError):
                    worker = "uncertain"
            else:
                worker = "confirmed"
            return 200, {"application": application, "worker": worker}, []
        payload = self._body(env, {"operation_id", "active"})
        result = self.store.change(token, payload["operation_id"], payload["active"])
        return (200 if result == "applied" else 409), {"result": result}, []

    def _body(self, env, fields):
        if self._http._media_type(env) != "application/json":
            raise _RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON required.")
        raw = self._http._read_body(env)
        payload = json.loads(raw, object_pairs_hook=self._unique)
        if type(payload) is not dict or set(payload) != fields:
            raise ValueError("invalid fields")
        return payload

    @staticmethod
    def _unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate fields")
            result[key] = value
        return result
