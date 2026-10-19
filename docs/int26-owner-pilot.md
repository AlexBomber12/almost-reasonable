# INT-26 owner console for the bounded private pilot

The owner console is an explicit companion to the INT-22 application. It uses
the existing `OwnerStore`, the conversation EventStore for bounded diagnostics,
the application IPC EventStore for application-observed spend state, and the
existing privacy ledger. It has no participant routes, worker credentials,
worker databases, provider client, account registration or inference authority.
Ordinary `app.private_web.cli serve` and the Stage 0 owner composition remain
unchanged. Starting the console does not provision an owner, create a missing
store, clear Stop, clear spending suspension or start either pilot process.

## Prepare locally

Use an already reviewed INT-22 application state directory and the **same
application OS UID** that owns its stores. Follow `docs/int22-private-pilot.md`
for the one-time creation of empty stores and the separate owner provisioning
command. Preserve all existing databases and worker control files. The owner
console needs only these existing application-owned paths:

```text
/var/lib/pilot-app/events.db
/var/lib/pilot-app/ipc-state.db
/var/lib/pilot-app/ledger.db
/var/lib/pilot-app/owner/owner.sqlite
```

Each database must be a regular, application-owned `0600` file. The owner
directory must be application-owned `0700`. The launcher rejects absent,
unsafe, uninitialized, or aliased stores before constructing application objects.
Schema validation is read-only, so an empty existing file is not initialized
by console startup. Run the
following as the application UID with an approved checkout and with those
paths available. This example assumes a local HTTPS reverse proxy serving a
**separate** owner origin. The launcher compares normalized browser origins,
including default HTTPS port 443, and requires a canonical participant URL so
case, escaped hosts and port spelling cannot make one origin appear separate.
Replace the reserved example hosts and source IP
with exact approved values; `--allowed-client` is repeatable, with at most 16
canonical numeric addresses.

```sh
python -B -m app.ops.int26_owner_pilot \
  --event-db /var/lib/pilot-app/events.db \
  --ipc-state-db /var/lib/pilot-app/ipc-state.db \
  --privacy-ledger /var/lib/pilot-app/ledger.db \
  --owner-directory /var/lib/pilot-app/owner \
  --allowed-host owner.example.invalid \
  --allowed-origin https://owner.example.invalid \
  --participant-origin https://chat.example.invalid \
  --allowed-client 192.0.2.25 \
  --trusted-proxy 127.0.0.1
```

The launcher binds `127.0.0.1:18026` by default. The HTTPS reverse proxy must
use the exact owner Host and Origin, restrict access to the approved operator
source addresses, overwrite any incoming forwarded headers, avoid logging
cookies, CSRF tokens or bodies, and forward to this loopback listener. If the
proxy sets `X-Forwarded-For`, supply its canonical numeric source address with
`--trusted-proxy` so the existing single-address proxy validation checks the
operator address against `--allowed-client`. A trusted proxy request without
one valid forwarded client address is rejected. Keep the listener on numeric
loopback; public binds are rejected. The proxy must provide TLS. There is no
insecure cookie mode and no external asset, CDN or analytics dependency.
Inspect the target host's proxy, UID, ownership and network isolation before
any separately approved live run. This document does not authorize one.

## Use and scope

Open the owner HTTPS origin, enter the separately provisioned owner credentials,
and wait for the login script to enable the login button. The form declares
POST explicitly and remains disabled when JavaScript is disabled, blocked or
fails to initialize. It must never put credentials into a URL. Native POST
fallback remains subject to the existing Origin, JSON and CSRF checks.
The participant message and privacy forms also declare POST and their exact
endpoints. If their scripts are unavailable, native submissions keep private
text and form inputs out of URLs and remain unauthorized without the existing
CSRF/Origin and JSON protections.

The owner and participant HTTP launchers use `PrivateWSGIRequestHandler`.
Raw request/access/error lines and WSGI exception details are discarded so
query strings, headers, cookies and bodies cannot enter container logs through
the reference server. Use the sanitized status/audit for operational evidence.
Configure reverse proxies, tunnels and external log collectors separately;
this application change cannot sanitize logs already held by those systems.

If credentials were exposed in a URL, stop the owner console and rotate the
password using `app.owner_control --rotate-password` from a trusted local shell
with the owner store mounted. That revokes old owner sessions atomically while
preserving Stop and the audit. Replace the affected browser-history entry and
handle existing logs/screenshots as sensitive evidence. Do not restart the
console until the fixed image is deployed and credential rotation succeeds.

After normal authentication, use the status and Stop controls. Login obtains a signed, single-use CSRF
challenge. The browser keeps the session CSRF token in memory; after a page
reload, sign in again to mutate state. Owner sessions are independent of
participant sessions. All protected reads require the exact Origin and
authenticated owner session. The browser uses `POST /status` so the browser
sends its Origin on same-origin requests; the existing `GET /status` remains
available for authenticated non-browser clients. Stop changes require CSRF and a single-use,
session-bound operation ID. Logout revokes that session. The last 50 sanitized
owner audit records appear below status.

The panel reports **application Stop** from `OwnerStore`. Turning it on blocks
subsequent private inference before IPC/SDK contact. A response already in
flight is checked again before release, so a Stop activated during that flight
prevents its release. This does not cancel a provider request already running,
change the separate worker Stop, or undo incurred cost. Removing application
Stop affects only this application control; it does not clear application or
worker spend suspension or open the worker's independent Stop.

Application-observed spend suspension and attempt count come from
`ipc-state.db`, the store used by the private runtime's `ProviderStateStore`.
They are not a worker usage total or billing authority. The optional,
independent read-only worker telemetry socket in `docs/worker-telemetry.md`
supplies worker Stop and provider aggregates.
For the separately configured B09B worker Stop assertion socket, operation
outcomes and local recovery procedure, see `docs/b09b-worker-emergency-stop.md`.
The display separates finalized usage, finalized attempts with incomplete
token data, and reservations without a final result. A reservation does not
confirm a provider call; actual total usage may be unknown even when recorded
token totals are zero. Use
`--worker-telemetry-socket /run/pilot-worker-status/status.sock` and
`--worker-telemetry-uid WORKER_UID` together to enable it. Without those flags,
or when a source cannot be validated, worker observations appear
**unavailable**, never zero. The owner process has no worker database or
control-file access. Conversation EventStore diagnostics only show a
bounded read-interface check. The privacy ledger indicator only shows that
its generation can be read. General usage listing and privacy reconciliation
remain unavailable through the bounded public APIs. Errors are returned as
generic responses; the page does not render private messages, provider bodies,
secrets or raw database errors.

## Shutdown and validation

For an authorized live pilot, use the B09B emergency control when configured,
or first activate the independent worker Stop with the protected worker-side
procedure in `docs/int22-private-pilot.md`, then activate application Stop
through this console. Stop the owner console with
the local process supervisor or `Ctrl-C`, and stop the worker/application by
the INT-22 procedure. Preserve all databases and audit evidence. Stopping the
console process does not change either Stop value.

`tests/test_int26_owner_console.py` uses synthetic stores and the INT-22 fake
SDK worker to verify browser admission, status scope, and Stop before contact
and after an in-flight control change. `scripts/ci.sh` runs the full local gate.
No real credentials, paid calls, services or participants are required.
