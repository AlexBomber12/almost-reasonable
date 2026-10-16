"""Explicit, credential-bound Threads Graph transport. No ambient token lookup."""

from dataclasses import dataclass
from datetime import datetime
import base64
import json
import re
from typing import Protocol
from urllib import error, parse, request

from .domain import (
    InboundEvent,
    InboundKind,
    OutboxItem,
    PublicationType,
    require_reference,
    validate_content,
)
from .simulator import (
    EffectClass,
    ProviderFailure,
    PublishEvidence,
    ReconciliationResult,
    VerifiedPollPage,
    _VERIFIED_SOURCE_AUTHORITY,
)

_HOST = "https://graph.threads.net"
_VERSION = "v1.0"
_SOURCE = "meta-threads-v1"
_MAX_RESPONSE = 64_000
_CURSOR = re.compile(r"c-([A-Za-z0-9_-]{1,124})\Z")


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status: int
    body: bytes


@dataclass(frozen=True, slots=True)
class ConfirmedPublication:
    publication_id: str
    evidence: str


class HTTPClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HTTPResponse: ...


class ThreadsTransport(Protocol):
    account_ref: str
    source_ref: str

    def create_container(self, item: OutboxItem) -> str: ...
    def publish(self, item: OutboxItem, container_id: str) -> PublishEvidence: ...
    def reconcile(
        self, item: OutboxItem
    ) -> ReconciliationResult | ConfirmedPublication: ...
    def poll(self, stream: InboundKind, cursor: str | None) -> VerifiedPollPage: ...


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPSClient:
    """TLS verifying stdlib client with redirects disabled and bounded reads."""

    def __init__(self):
        self._opener = request.build_opener(_NoRedirect)

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HTTPResponse:
        req = request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as response:
                return HTTPResponse(response.status, response.read(_MAX_RESPONSE + 1))
        except error.HTTPError as response:
            try:
                return HTTPResponse(response.code, response.read(_MAX_RESPONSE + 1))
            finally:
                response.close()


class ThreadsHTTPTransport:
    """Narrow Graph API client; construction requires an explicit trusted token."""

    source_ref = _SOURCE

    def __init__(
        self,
        *,
        account_ref: str,
        provider_user_id: str,
        token: str,
        reply_thread_id: str,
        client: HTTPClient,
        timeout: float = 5.0,
    ):
        self.account_ref = require_reference("account reference", account_ref)
        self.provider_user_id = require_reference("provider user ID", provider_user_id)
        self.reply_thread_id = require_reference("reply thread ID", reply_thread_id)
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ValueError("invalid transport credential")
        if type(timeout) not in (int, float) or not 0 < timeout <= 10:
            raise ValueError("invalid transport timeout")
        self._token = token
        self._client = client
        self._timeout = timeout

    def _call(
        self,
        method: str,
        path: str,
        parameters: dict[str, str],
        *,
        publication_id: str | None = None,
        container_id: str | None = None,
    ) -> dict:
        query = parse.urlencode(parameters)
        url = f"{_HOST}/{_VERSION}/{path}" + (f"?{query}" if query else "")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        body = b"" if method == "POST" else None
        try:
            response = self._client.request(method, url, headers, body, self._timeout)
        except Exception:
            response = None
        if response is None:
            raise ProviderFailure(
                EffectClass.UNCERTAIN_DISPATCH
                if method == "POST" or publication_id is not None
                else EffectClass.PROVEN_PRE_DISPATCH,
                "provider_transport_interrupted",
                container_id=container_id,
                publication_id=publication_id,
            )
        if (
            type(response) is not HTTPResponse
            or type(response.status) is not int
            or type(response.body) is not bytes
            or len(response.body) > _MAX_RESPONSE
        ):
            raise ProviderFailure(
                EffectClass.UNCERTAIN_DISPATCH,
                "provider_response_invalid",
                container_id=container_id,
                publication_id=publication_id,
            )
        if response.status in (301, 302, 303, 307, 308):
            raise ProviderFailure(
                EffectClass.UNCERTAIN_DISPATCH,
                "provider_redirect_rejected",
                container_id=container_id,
                publication_id=publication_id,
            )
        try:
            data = json.loads(response.body)
        except (ValueError, UnicodeDecodeError):
            raise ProviderFailure(
                EffectClass.UNCERTAIN_DISPATCH,
                "provider_response_malformed",
                container_id=container_id,
                publication_id=publication_id,
            ) from None
        if not isinstance(data, dict):
            raise ProviderFailure(
                EffectClass.UNCERTAIN_DISPATCH,
                "provider_response_malformed",
                container_id=container_id,
                publication_id=publication_id,
            )
        if response.status != 200:
            known_rejection = response.status in (400, 401, 403) and isinstance(
                data.get("error"), dict
            )
            effect = (
                EffectClass.UNCERTAIN_DISPATCH
                if publication_id is not None
                else EffectClass.CONFIRMED_REJECTION
                if known_rejection and method == "POST"
                else EffectClass.PROVEN_PRE_DISPATCH
                if known_rejection
                else EffectClass.UNCERTAIN_DISPATCH
            )
            raise ProviderFailure(
                effect,
                "provider_rejected" if known_rejection else "provider_error_uncertain",
                container_id=container_id,
                publication_id=publication_id,
            )
        return data

    def create_container(self, item: OutboxItem) -> str:
        if item.account_ref != self.account_ref:
            raise ValueError("outbound account mismatch")
        self._verify_account()
        params = {"media_type": "TEXT", "text": validate_content(item.content)}
        if item.publication_type is PublicationType.REPLY:
            params["reply_to_id"] = require_reference(
                "reply parent", item.parent_message_id
            )
        data = self._call("POST", f"{self.provider_user_id}/threads", params)
        try:
            return require_reference("container ID", data.get("id"))
        except ValueError:
            raise ProviderFailure(
                EffectClass.UNCERTAIN_DISPATCH, "container_id_missing"
            ) from None

    def publish(self, item: OutboxItem, container_id: str) -> PublishEvidence:
        if item.account_ref != self.account_ref:
            raise ValueError("outbound account mismatch")
        container_id = require_reference("container ID", container_id)
        self._verify_account()
        data = self._call(
            "POST",
            f"{self.provider_user_id}/threads_publish",
            {"creation_id": container_id},
            container_id=container_id,
        )
        try:
            publication_id = require_reference("publication ID", data.get("id"))
        except ValueError:
            raise ProviderFailure(
                EffectClass.UNCERTAIN_DISPATCH,
                "publication_id_missing",
                container_id=container_id,
            ) from None
        details = self._call(
            "GET",
            publication_id,
            {"fields": "id,owner,replied_to"},
            container_id=container_id,
            publication_id=publication_id,
        )
        if not self._publication_matches(item, publication_id, details):
            raise ProviderFailure(
                EffectClass.UNCERTAIN_DISPATCH,
                "publication_binding_unverified",
                container_id=container_id,
                publication_id=publication_id,
            )
        return PublishEvidence(publication_id, "provider_publication_details_verified")

    def reconcile(
        self, item: OutboxItem
    ) -> ReconciliationResult | ConfirmedPublication:
        # Only an ID returned for this exact publish attempt is sufficient evidence.
        if item.account_ref != self.account_ref or item.provider_publication_id is None:
            return ReconciliationResult.UNRESOLVED
        publication_id = require_reference(
            "publication ID", item.provider_publication_id
        )
        try:
            details = self._call(
                "GET",
                publication_id,
                {"fields": "id,owner,replied_to"},
                publication_id=publication_id,
            )
        except ProviderFailure:
            return ReconciliationResult.UNRESOLVED
        if self._publication_matches(item, publication_id, details):
            return ConfirmedPublication(
                publication_id, "provider_publication_details_verified"
            )
        return ReconciliationResult.UNRESOLVED

    def _publication_matches(
        self, item: OutboxItem, publication_id: str, details: dict
    ) -> bool:
        owner = details.get("owner")
        replied = details.get("replied_to")
        expected_parent = (
            item.parent_message_id
            if item.publication_type is PublicationType.REPLY
            else None
        )
        return (
            details.get("id") == publication_id
            and isinstance(owner, dict)
            and owner.get("id") == self.provider_user_id
            and (replied.get("id") if isinstance(replied, dict) else None)
            == expected_parent
        )

    def _verify_account(self) -> None:
        profile = self._call("GET", "me", {"fields": "id"})
        if profile.get("id") != self.provider_user_id:
            raise ProviderFailure(
                EffectClass.PROVEN_PRE_DISPATCH, "provider_account_mismatch"
            )

    def poll(self, stream: InboundKind, cursor: str | None) -> VerifiedPollPage:
        if type(stream) is not InboundKind:
            raise ValueError("invalid inbound stream")
        raw_cursor = None
        if cursor is not None:
            match = _CURSOR.fullmatch(cursor)
            if match is None:
                raise ValueError("invalid stored cursor")
            try:
                raw_cursor = base64.urlsafe_b64decode(
                    match[1] + "=" * (-len(match[1]) % 4)
                ).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                raise ValueError("invalid stored cursor") from None
        path = (
            f"{self.reply_thread_id}/replies"
            if stream is InboundKind.REPLY
            else f"{self.provider_user_id}/mentions"
        )
        params = {
            "fields": "id,text,timestamp,root_post,replied_to,is_reply_owned_by_me",
            "limit": "50",
        }
        if raw_cursor is not None:
            params["after"] = raw_cursor
        data = self._call("GET", path, params)
        rows = data.get("data")
        paging = data.get("paging", {})
        if not isinstance(rows, list) or len(rows) > 50 or not isinstance(paging, dict):
            raise ProviderFailure(EffectClass.PROVEN_PRE_DISPATCH, "poll_page_invalid")
        cursors = paging.get("cursors", {})
        if not isinstance(cursors, dict):
            raise ProviderFailure(EffectClass.PROVEN_PRE_DISPATCH, "poll_page_invalid")
        raw_next = cursors.get("after")
        if raw_next is not None and (
            not isinstance(raw_next, str) or not raw_next or len(raw_next.encode()) > 90
        ):
            raise ProviderFailure(
                EffectClass.PROVEN_PRE_DISPATCH, "poll_cursor_invalid"
            )
        next_cursor = (
            "c-" + base64.urlsafe_b64encode(raw_next.encode()).decode().rstrip("=")
            if raw_next is not None
            else None
        )
        events = []
        try:
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("invalid row")
                event_id = require_reference("event ID", row.get("id"))
                text = validate_content(row.get("text"), inbound=True)
                occurred = datetime.fromisoformat(row.get("timestamp"))
                if occurred.tzinfo is None:
                    raise ValueError("unaware event time")
                root = row.get("root_post")
                replied = row.get("replied_to")
                root_id = root.get("id") if isinstance(root, dict) else None
                parent_id = replied.get("id") if isinstance(replied, dict) else None
                if stream is InboundKind.REPLY:
                    if (
                        root_id != self.reply_thread_id
                        or row.get("is_reply_owned_by_me") is not False
                    ):
                        raise ValueError("reply binding mismatch")
                    conversation_id = self.reply_thread_id
                    parent_id = require_reference("reply parent", parent_id)
                else:
                    conversation_id = require_reference(
                        "mention conversation", root_id or event_id
                    )
                    if parent_id is not None:
                        parent_id = require_reference("mention parent", parent_id)
                events.append(
                    InboundEvent(
                        event_id,
                        event_id,
                        self.account_ref,
                        self.source_ref,
                        stream,
                        conversation_id,
                        parent_id,
                        occurred,
                        text,
                    )
                )
        except (TypeError, ValueError):
            raise ProviderFailure(
                EffectClass.PROVEN_PRE_DISPATCH, "poll_page_invalid"
            ) from None
        return VerifiedPollPage(
            self.account_ref,
            self.source_ref,
            cursor,
            next_cursor,
            tuple(events),
            _VERIFIED_SOURCE_AUTHORITY,
        )
