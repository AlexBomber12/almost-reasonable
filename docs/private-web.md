# Private web interface

This Stage 0 slice provides a local, participant-private WSGI interface backed
by the existing `EventStore` and `PrivateContextAssembler`. It is intended only
for synthetic accounts and data. It does not enroll real participants, call a
real model, expose an owner control plane, or deploy through Cloudflare.

## Local setup

Use a private directory because both SQLite stores reject a group- or
world-writable parent:

```bash
install -d -m 700 data/private-web
python -m app.private_web.cli provision \
  --auth-db data/private-web/auth.sqlite3 \
  --username synthetic-a \
  --participant-id participant-synthetic-a
python -m app.private_web.cli provision \
  --auth-db data/private-web/auth.sqlite3 \
  --username synthetic-b \
  --participant-id participant-synthetic-b
```

The command prompts twice with terminal echo disabled. It deliberately has no
password command-line option, default password, built-in account, environment
variable fallback, or web-based provisioning route. Use unique synthetic
passwords of 12–1024 UTF-8 bytes. Do not use real participant credentials.

For loopback-only development over plain HTTP:

```bash
python -m app.private_web.cli serve \
  --auth-db data/private-web/auth.sqlite3 \
  --event-db data/private-web/events.sqlite3 \
  --listen-host 127.0.0.1 \
  --port 8000 \
  --allowed-host 127.0.0.1:8000 \
  --allowed-origin http://127.0.0.1:8000 \
  --insecure-http
```

Open `http://127.0.0.1:8000/login`. The `--insecure-http` switch is explicitly
for loopback development; the CLI rejects it unless both the bind address and
the HTTP browser origin use a numeric loopback IP. Omit it behind an HTTPS
endpoint so every session and CSRF cookie has the `Secure` attribute; secure
mode rejects a non-HTTPS browser origin. Listener addresses are explicit
numeric IPv4 or IPv6 values; bracket IPv6 literals in `--allowed-host` and the
browser origin (for example, `[::1]:8000`).

The same transport rule is enforced by `PrivateWebConfig`, including for an
embedded WSGI application: secure cookies require HTTPS origins, while cookies
without `Secure` require numeric loopback HTTP origins. Configure browser
origins in their exact lowercase canonical form, without an explicit default
`:443` or `:80` port or empty delimiters, matching the value browsers send in
the `Origin` header.

When an HTTPS terminator is required, explicitly list each numeric address from
which the WSGI process may accept a forwarded client address:

```bash
python -m app.private_web.cli serve \
  --auth-db data/private-web/auth.sqlite3 \
  --event-db data/private-web/events.sqlite3 \
  --allowed-host chat.example \
  --allowed-origin https://chat.example \
  --trusted-proxy 127.0.0.1
```

The named proxy boundary must remove any inbound `X-Forwarded-For` value and
write exactly one validated client IP. The application accepts that header only
when `REMOTE_ADDR` exactly matches the explicit numeric allowlist. It rejects
missing, malformed, or chained values from a trusted proxy and ignores all
forwarding headers from other peers. `X-Forwarded-Host` and
`X-Forwarded-Proto` are never trusted. This PR does not deploy or configure the
HTTPS proxy itself.

Run the focused tests with:

```bash
python -m pytest tests/test_private_web_*.py
```

Run the complete repository gate with `scripts/ci.sh`.

## Account and session storage

`AuthStore` owns a dedicated, versioned SQLite file. It is intentionally
separate from the application `EventStore` and from the provider worker's
database. The file is created owner-only (`0600`), symbolic links and unsafe
parent directories are rejected, and credentials are never written to the
event database.

Passwords use per-account 128-bit random salts and Python's standard-library
`hashlib.scrypt` with the versioned `scrypt-v1` parameters N=32768, r=8, p=1,
and a 256-bit result. Neither passwords nor recoverable password equivalents
are stored. Login comparisons use `hmac.compare_digest`, unknown accounts run a
dummy scrypt path, and failures are bounded by both a durable account-wide
window and a separate aggregate per-client window. Expired throttle rows are
removed during login, and overlong login values are rejected after the bounded
dummy-work path rather than truncated into an accepted credential.

Successful login generates independent 256-bit session and CSRF tokens. Only
SHA-256 token digests are stored. Sessions have an absolute expiry, can be
revoked, are invalid after account disablement, survive a process restart, and
are rotated rather than accepting a pre-login session identifier. A successful
login removes expired/revoked records and invalidates any earlier session for
that account, bounding storage and leaving one active session per account. The
trusted event-source reference is a domain-separated hash of the account's
unique participant identity, so an uncertain message retry remains idempotent
after session expiry or rotation without persisting a session bearer token.
Logout revokes the server-side record before clearing cookies, and the browser
only navigates to the login page after that response succeeds. Session bearer
tokens must therefore be protected like credentials.

Local account provisioning is an operator filesystem/terminal operation, not
an administrative web interface. Any future owner or administrative operation
requires a separately authenticated, anti-replay-protected control plane; an
ordinary participant session and chat text claiming owner status have no such
authority.

## HTTP and privacy boundary

The standard-library WSGI application exposes:

| Route | Purpose |
|---|---|
| `GET /login`, `POST /login` | CSRF-protected sign-in |
| `POST /logout` | authenticated revocation |
| `GET /session` | minimal authentication status |
| `GET /chat` | escaped private conversation page |
| `GET /api/history` | authenticated participant-private history |
| `POST /api/messages` | authenticated participant input |

The application checks the exact `Host` on every request. State changes also
require an exact configured `Origin` and a session-bound CSRF token. Cookies are
`HttpOnly` and `SameSite=Strict`, and are `Secure` unless loopback HTTP is
explicitly selected. Private responses use `Cache-Control: no-store`, a
restrictive Content Security Policy, explicit content types, bounded request
bodies, and sanitized errors without tracebacks. No credentialed CORS policy
is emitted.

Authentication occurs before reading a private-message body, persisting its
payload, retrieving participant context, or reaching the inference seam. The
authenticated session alone constructs `TrustedPrincipalContext`. JSON fields,
query parameters, conversation text, model output, and owner/admin claims
cannot select a participant or widen scope. Message JSON accepts exactly
`message_id` and `content`; extra identity or privilege fields fail closed.

Permitted input is stored as `PrivateInputEvent`. History is retrieved through
the existing `EventStore` with the same trusted context, and model context is
assembled by `PrivateContextAssembler` only after a newly created input is
durable. Repeated source message IDs are idempotent and do not repeat context
assembly or inference. The browser retains one pending ID across network and
server-error retries, then creates a new ID only after an acknowledged success.
HTML renders participant content with escaping; JSON is served only as
`application/json`.

## Disabled default and explicit worker mode

`ConversationService` accepts the narrow `InferenceAdapter` protocol. Its safe
default is `DisabledInferenceAdapter`, which performs no network or provider
operation and returns `unavailable`. `SyntheticInferenceAdapter` records the
already privacy-scoped context in memory for deterministic tests and returns
`pending`.

The disabled adapter result cannot contain deliverable response text. INT-19
adds a separate `--worker-socket` CLI mode that composes matching authentication,
privacy, owner-stop, provider-spend and worker-client controls. This path uses
`WorkerConversationRuntime` for Russian-screened, policy-controlled durable
replies and history; it does not widen the ordinary adapter. See
[private-worker-conversation.md](private-worker-conversation.md) for required
configuration and current limits.

## Before Stage 2 enrollment

This slice is not sufficient for real participants. Privacy consent and
notices, retention and deletion, encrypted storage/key handling,
backup/quarantined recovery with deletion-ledger replay, processor terms,
incident/crisis procedures, and a separately authenticated owner control plane
are being developed independently. They must be integrated and validated
before any real participant enrollment.

In particular, this PR does not claim database encryption, operator blindness,
external-provider deletion, safe private-to-shared abstraction, production
recovery, or permission for real OpenAI inference. The infrastructure operator
may still have operational database access; that does not create an
application-level conversational route to another participant's history.
