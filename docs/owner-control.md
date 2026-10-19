# INT-07 — Authenticated owner control (Stage 0)

`app.owner_control` provides an independent JSON WSGI interface. It has no
participant routes, inference adapter, provider credential loader, research
transport or publishing transport. Every live-effect capability reported here
is disabled. These are the capabilities of this interface, not an inventory of
other processes running on the host.

## Local provisioning and composition

From an authenticated local operator shell, run:

```sh
python -m app.owner_control --directory /protected/local/owner-control --username operator
```

The directory must belong to the current OS user with mode 0700; every directory
ancestor must pass the existing trusted traversal rules. Passwords are prompted
without echo and never accepted in command arguments. The SQLite file is 0600.
Do not reuse a participant authentication directory or database. Existing files
without the owner schema marker are rejected. Provisioning is an OS-authorized
operation; there is no registration endpoint or remote provisioning authority.
Serialize initial provisioning. An interrupted initialization fails closed and
requires local operator inspection rather than automatic schema repair.

To replace an exposed or obsolete owner password, use the local operator CLI
against the existing protected owner store:

```sh
python -m app.owner_control --directory /protected/local/owner-control --username operator --rotate-password
```

Enter and confirm a new unique password at the hidden prompts. The command
rejects a terminal that cannot provide hidden input. It requires an existing
owner store and account: it never recreates a database or enables a disabled
account. Password replacement and revocation of all previous account sessions
commit in one transaction. The owner identity, current Stop value, pending
operation history and audit remain intact. Old sessions and their pending Stop
operations lose authority; other owner accounts are unchanged. A database error
rolls back both the new password and the session revocations.

An authorized local composition can construct `OwnerStore(directory)` and
`OwnerApplication(store, event_store, PrivateWebConfig(...),
privacy_ledger=ledger)` with existing local stores. Use an exact HTTPS owner
origin and exact Host allowlist. Owner WSGI admission additionally requires an
explicit finite `allowed_clients` frozenset of canonical source addresses, with
a maximum of 16 and a numeric-loopback default. Admission grants no owner
authority: credentials and sessions remain mandatory. Unlisted sources fail
before any authentication or replay-state allocation. Secure cookies are mandatory; there is no
insecure HTTP mode, server-launch command, new listening port or deployment
service in this change. The TLS-facing WSGI host must supply correct request
metadata and avoid logging request bodies or Cookie/CSRF headers. Proxy client
IP handling reuses private-web's explicit trusted-proxy allowlist. A reverse proxy
must overwrite forwarded metadata and be explicitly trusted so admission checks
the actual canonical client address, rather than treating arbitrary remote
callers as the proxy's loopback address. Configure only approved operator source
addresses; broad networks and user-supplied identity headers are not accepted.
No deployment/edge configuration is created by this package.

The store composes the existing scrypt, throttling, hashed session and revocation
primitives. Owner accounts receive random owner actor references. Dedicated
protected storage and the owner schema marker supply independent authority;
passwords retain the existing full 12–1024 UTF-8 byte policy. Owner sessions expire after one
hour, and a new login revokes prior account sessions. `OwnerStore.revoke(token)`
allows trusted local revocation. Participant sessions, participant `is_owner`,
chat claims and model output never supply owner authorization or conversational
privileges.

## HTTP contract

All bodies and responses are JSON. No query strings are accepted. Cookies use
an independent `ar_owner_*` namespace with Secure, HttpOnly and SameSite=Strict;
responses use no-store, no-referrer and restrictive CSP headers. Authentication
precedes protected body parsing and diagnostic reads. Protected requests require
an exact allowed Origin. The existing configured body limit bounds parsing;
malformed JSON, duplicate keys, unknown fields and invalid types fail safely.

| Endpoint | Contract |
| --- | --- |
| `GET /login` | Obtain a signed five-minute login CSRF challenge and its cookie, bound to the validated client address. Issuance is read-only and cannot exhaust or evict another client's challenge. |
| `POST /login` | Exact `username`, `password`, `csrf` fields; matching login cookie and Origin required. Challenge is consumed once, including failed credential attempts. Protected signing key and replay markers survive restart; expired markers are removed during consumption. At most 32 live consumed markers per admitted client; the fixed maximum of 16 source addresses bounds anonymous durable replay state to 512 live markers. No unrelated live replay evidence is evicted. Expired rows are reclaimed before the next admitted consumption, so anonymous callers cannot cause an unbounded SQLite high-water mark through source-identity churn. Returns session cookie and session CSRF token on success; generic 401/429 failures. |
| `GET /status` | Owner session and Origin required. Stage 0 capability status, durable stop, provider suspension/count/evidence, EventStore diagnostics, privacy readiness and at most 50 recent owner audit records. |
| `POST /operation` | Owner session, Origin and `X-CSRF-Token` required. Issue a server-generated stop operation ID bound to this session, expiring after five minutes or session expiry. At most 32 pending IDs per session. |
| `POST /stop` | Owner session, Origin, CSRF and exact `operation_id`, boolean `active` fields required. `true` stops; `false` explicitly clears only this stop state. |
| `POST /logout` | Owner session, Origin and CSRF required. Revoke session and clear cookie. Already revoked sessions cannot authorize further requests. |

Stop, operation-consumption and audit writes share one SQLite transaction.
Concurrent retries with identical operation/session/action return the previous
result without another mutation or audit entry. Conflicting action reuse returns
409 and records a sanitized rejection. Expired, unknown or other-session
operation IDs also return 409. Replaying an earlier completed operation after a
later change never reapplies its old state. Logout is idempotent at storage level;
a repeated HTTP request with the revoked session returns 401.

Successful stop/reset records include the server operation ID, random owner
actor reference, action, timestamp, result and rejection category. Rejected
admission and conflicting replay records use fresh server correlation IDs, never
client-supplied text. Authenticated malformed/CSRF/Origin mutation requests are
recorded as fixed-category rejections. Anonymous requests cannot create owner
audit records. No audit includes password, session token, private chat, content
reference or provider credential. Audit history and completed operation identity
are durable; reads are bounded to 50 records. Storage retention/compaction is a
future local operator policy and must preserve replay guarantees.

## Stop authority and inspection limitations

New stores start stopped. `OwnerStore.active` is the trusted read callback and
returns stopped on store failures. A synthetic execution boundary can check it
before execution; tests demonstrate suppression and restart durability. Clearing
stop changes no capability, enrollment, privacy, research or publishing flag.

**This is staged, not global shutdown enforcement.** Stage 1 still uses its own
protected `WorkerEmergencyStop` JSON file and request gate. This PR does not write
that file or wire real execution boundaries. Follow-up application composition
must choose one authoritative source, wire fresh reads before every provider
call/retry and external effect, define failure/recovery policy and test process
restart. An in-flight external call cannot be retroactively revoked.

The later INT-26/B09B composition adds a separately configured, worker owned
Stop assertion service for its private pilot. See `docs/b09b-worker-emergency-stop.md`.

The INT-26 composition can optionally show independent worker observations
through the separate read-only status process in `docs/worker-telemetry.md`.
Its Stop observation is distinct from this application Stop. Its recorded
provider totals are not billing or model-readiness evidence. Unresolved worker
reservations appear separately from finalized attempts and incomplete usage.
They do not prove provider contact; actual usage may remain uncertain when
recorded totals are zero. Missing or stale
status remains unavailable, and the console never receives worker state files.

Provider inspection uses `ProviderStateStore.suspension`, `usage_attempt_count`
and, when suspended, `usage_attempt` for the recorded suspension attempt. Only
token counters, cost availability, estimate and currency are projected; request,
response, operation, correlation and configuration identifiers are excluded.
These are recorded estimates, never billing authority. General recent usage
listing remains disabled: the existing public API has no bounded listing. A
follow-up provider-owned API should return a bounded payload-free aggregate or
allowlisted numeric projection without arbitrary database introspection.

EventStore health means its public diagnostics call succeeded; it is not an
integrity scan or proof of complete application health. Errors return generic
503 responses without exception text.

Privacy readiness uses only `PrivacyLedger.control_generation` when a ledger is
supplied. Reconciliation is explicitly unavailable: existing
`reconciliation_snapshot` and `deletions_for_reconciliation` return unbounded
protected references. Follow-up privacy ownership must provide a bounded
payload-free count/status read API. No consent, deletion, revocation, restore or
participant impersonation mutation is exposed. INT-03 application erasure and
appropriate participant authorization UX remain prerequisites; this interface
does not claim complete Stage 2 privacy or widen ordinary chat permissions.

## Validation

`tests/test_owner_control.py` uses synthetic accounts, temporary SQLite stores,
injected clocks and concurrent local requests. It covers authentication,
isolation, cookie/Origin/Host/CSRF admission, login and stop replay, malformed and
oversized bodies, durable audit and stop, fail-closed files, numeric provider
inspection, privacy readiness and guarded synthetic execution. Run
`scripts/ci.sh` for the full 100% line/branch coverage gate. No paid API, external
network, Threads operation, enrollment, publishing or deployment is required.
