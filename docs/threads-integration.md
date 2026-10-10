# Threads integration domain

Status: deterministic simulation only. Reviewed against Meta's official Threads
documentation on 2026-10-10. This package does not contain an HTTP client, read
credentials, contact Meta, or enable live publication or polling.

## Provider contract review

The provider facts below come from Meta's current [Threads API
overview](https://developers.facebook.com/documentation/threads/overview),
[publishing guide](https://developers.facebook.com/documentation/threads/create-posts),
[reply documentation](https://developers.facebook.com/documentation/threads/retrieve-and-manage-replies),
[post and mention retrieval guide](https://developers.facebook.com/documentation/threads/retrieve-and-discover-posts),
[troubleshooting guide](https://developers.facebook.com/documentation/threads/troubleshooting),
and [delete documentation](https://developers.facebook.com/documentation/threads/posts/delete-posts).
Meta's official [Postman collection](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api)
was used as a secondary official example set. The documentation and App
Dashboard must be checked again before a live transport is reviewed.

Verified provider behavior:

- A Threads user access token represents the configured Threads account.
  `threads_basic` is the base permission. Publishing requires
  `threads_content_publish`; reply reads require `threads_read_replies`;
  mention reads require `threads_manage_mentions`; reply moderation uses
  `threads_manage_replies`; deletion uses `threads_delete`. Permissions for
  people without an app role require the applicable App Review and a published
  app.
- The ordinary publish sequence is provider-specific: `POST
  /{threads-user-id}/threads` creates a text/image/video container, then `POST
  /{threads-user-id}/threads_publish` publishes its `creation_id`. Text
  containers currently also document `auto_publish_text`; this implementation
  deliberately models the explicit two-step sequence because it exposes both
  dispatch boundaries for recovery.
- A public reply is a publication container with `reply_to_id`, followed by the
  same publish step. The application therefore gives each reply its own outbox
  operation; there is no direct model-to-send route.
- Container status can be read with `GET /{container-id}?fields=status,error_message`.
  The documented states are `EXPIRED`, `ERROR`, `FINISHED`, `IN_PROGRESS`, and
  `PUBLISHED`. A known container reporting `PUBLISHED` can be useful exact
  reconciliation evidence; a missing container ID or an inconclusive response
  cannot prove that no post exists.
- Replies, conversations, a user's posts, and mentions have authenticated read
  endpoints. The documented list responses use `data` plus `paging.cursors`
  (`before`/`after`). The implementation begins with bounded polling and stores
  one cursor per account and stream.
- The documented publishing quota is 250 API-published posts per rolling 24
  hours; the reply quota is 1,000 per rolling 24 hours. Current usage and the
  provider-returned quota configuration are available from
  `/{threads-user-id}/threads_publishing_limit`. These are provider ceilings,
  not engagement targets. A future transport must also handle Graph API
  authentication, permission, validation, throttling, and transient errors
  without exposing response bodies or tokens.
- Meta documents deletion of an owned Threads media object with the
  `threads_delete` permission. Deletion is not part of this outbox slice and is
  not simulated.

Not verified or deliberately unsupported:

- The reviewed documentation does not promise an idempotency key for container
  creation or publication. The simulator and outbox do not invent one.
- No documented endpoint was found that accepts the application's local
  operation ID or performs a guaranteed exact lookup by content fingerprint.
  User-post listings may aid an operator, but content/time similarity is not
  deterministic proof.
- A general edit operation was not found in the reviewed contract. The project
  must not claim edit support. Delete capability does not imply safe rollback of
  an outcome whose publication identity is unknown.
- Webhook superiority, delivery guarantees, all error-code retry semantics, and
  exact production reconciliation behavior still require credentialed smoke
  tests. This PR performs none.
- Keyword search requires `threads_keyword_search` and App Review. It is not a
  prerequisite for inbound replies/mentions and remains unavailable here.
- App Review approval, production account permissions, token lifetime/refresh,
  privacy notice, live rate behavior, and provider smoke evidence are absent.

## Architecture and credential boundary

`app/threads/` is an application-owned domain package. `ThreadsStore` owns a
separate version-8 SQLite database in WAL mode, including atomic migrations
through version 8 from every earlier schema version; it does not alter the main
`EventStore` schema. Application integration will eventually supply the database
path, configured account reference, authenticated approval objects, privacy and
Russian-output gate results, emergency-stop state, and a narrow
credential-owning transport.
The service pins the configured account at construction and revalidates both
the mutable policy object and transport/simulator binding at proposal,
authorization, dispatch, recovery, and polling boundaries. A changed binding
fails closed and cannot retarget a durable operation.

Outbound readiness and dispatch also require an explicit
`CurrentAuthorizationProvider`. Its contract resolves the current authenticated
approval by account, operation, and approval reference and obtains a current
per-draft privacy decision. The approval and privacy values stored with a
proposal are historical evidence, not live authorization. A missing provider,
an unavailable provider, an absent or mismatched approval, or any result other
than an exact `True` privacy decision fails closed before the next external
effect. This package intentionally supplies no production implementation of
that trusted boundary.

The current transport is `ThreadsSimulator`. It has no URL, socket, token, or
generic HTTP method. Its `network_calls` counter remains zero. The separate
`SimulationAuthorizationProvider` is an in-memory, mutable fixture used to test
approval revocation, consent changes, and provider unavailability; it also has
no credentials or network behavior. Verified poll pages carry simulator-owned
source evidence that callers cannot create through the public API. JSON fields
inside a public message never grant authority.

Raw private context is not an outbox field. A proposal contains bounded public
content or an approved public reference, a closed provenance value, and an
application-supplied privacy authorization result plus a per-draft Russian
output authorization result. `private_unauthorized` and a missing privacy or
Russian-output result fail before persistence. Identifiable private material
will require exact structured consent, and a factual public claim materially
derived from private discussion will require independent public evidence, once
those external gates are integrated.

## Durable lifecycle

Each item preserves an immutable local operation ID, configured account,
publication type, parent for replies, draft ID, bounded content, fingerprint,
provenance restriction, approval reference/status, state and revision,
correlation ID, attempt and dispatch evidence, provider IDs when known,
reconciliation status, replacement linkage, duplicate-risk acknowledgement, and
timestamps. Every transition is an atomic row update plus a revisioned transition
record.

The closed lifecycle is:

1. `proposed` records intent only.
2. `awaiting_policy_or_operator_approval` makes the lack of authorization
   explicit.
3. `ready` requires an authenticated, unexpired, unrevoked, operation-specific
   operator approval and application-controlled account, scope, stage, privacy,
   Russian-output, publication-capability, and stop checks.
4. `sending` durably records intent before a simulated provider call and
   increments the bounded attempt count.
5. `confirmed` retains verified provider success evidence.
6. `unknown` means an effect may have occurred and requires reconciliation.
7. `unknown_abandoned` means reconciliation ended without resolving the effect;
   uncertainty and identity remain permanent. This transition requires a
   current, purpose-bound `abandon_unknown` operator approval, whose reference
   is durably recorded without replacing publication approval attribution.
8. `cancelled` is allowed only before dispatch.
9. `failed_terminal` records a confirmed rejection or exhausted proven
   pre-dispatch attempts.

The service resolves current approval and privacy state when an item becomes
ready, rechecks both after acquiring SQLite's writer lock for the `ready` and
`sending` transitions, and immediately before both container creation and
publication. The `sending` fence also rechecks the stop and pinned account before
incrementing the bounded attempt count. Each effect check occurs after its final durable
dispatch-marker write, so no intervening store update creates an authorization
gap before the provider call. After all potentially blocking authorization
and unresolved-publication store lookups, the service obtains a fresh atomic
approval/privacy snapshot and validates the accepted approval against a freshly
sampled clock before readiness and before each external effect. Revocation, expiry,
consent withdrawal, or control-plane unavailability after proposal or approval
therefore blocks the next effect. The approval actually accepted for dispatch
replaces the stored
approval reference atomically with the durable sending transition. A stop
activated before either call blocks it. A stop activated after a request is in
flight cannot retract an already accepted effect; the resulting success or
uncertainty is recorded honestly.

A failed initial readiness or dispatch authorization is also durable audit
evidence: the outbox revision advances with a same-state transition that keeps
the operation correlation and records the phase plus either a bounded hash of
the authentic operation-bound approval reference or `unauthenticated`. It does
not overwrite the approval attribution of an accepted publication.

After the last pre-effect authorization check and before the first provider
request, dispatch atomically verifies account uncertainty and acquires one
durable account-wide effect reservation. It holds that reservation across the
container and publication sequence. A second operation for the same account
therefore cannot pass its own atomic uncertainty check while the first can
still become `unknown`. Final classification and ordinary crash recovery remove
the reservation in the same transaction; an unclassified process death leaves
it in place and fails closed until `sending` recovery classifies the operation.
The live dispatcher also holds an account-scoped OS file lock for the complete
dispatch call. `sending` recovery must acquire the same lock without waiting;
it therefore cannot classify or release a reservation owned by a still-running
worker. The OS releases the lock on process death, after which durable evidence
drives recovery. This contract assumes the documented single-host SQLite
deployment and must be replaced by an equally strong coordinator before any
multi-host transport is considered.

## Failure and reconciliation rules

| Evidence | Durable result | Automatic resend |
|---|---|---|
| Request provably not transmitted | `ready` within the two-attempt simulation bound, then `failed_terminal` | Same durable operation only |
| Transmission/acceptance uncertain | `unknown` | Never |
| Container/publish response lost or malformed after possible acceptance | `unknown` | Never |
| Provider explicitly rejects with no publication | `failed_terminal` | No contract-supported retry is assumed |
| Provider returns verified publication identity | `confirmed` | Not applicable |
| Reconciliation verifies `PUBLISHED`/exact success | `confirmed` | Not applicable |
| Reconciliation verifies rejection/no effect | `failed_terminal` | No automatic replacement |
| Reconciliation is unavailable or inconclusive | remain `unknown`, or authenticated operator moves to `unknown_abandoned` | Never |

A crash in `sending` is recovered from durable dispatch evidence. A proven
pre-transmission crash returns the same operation to `ready`; possible
transmission enters `unknown`. The pinned account binding is rechecked inside
the recovery transition before classification or reservation release. Restart
never creates a new identity.

Exact operation IDs and provider IDs are unique across restart. A normalized,
bounded SHA-256 fingerprint produces a likely-duplicate warning; it does not
claim semantic equivalence. Exact content matching an unresolved operation
cannot be recreated under a new ID; normalized matches remain warnings rather
than hard identity decisions. Any unresolved `unknown` or
`unknown_abandoned` pauses ordinary readiness for that account. An exceptional
replacement must link the unresolved operation and carry a separate
authenticated approval that explicitly acknowledges duplicate-publication risk;
it never changes or retries the original item. Proposal insertion revalidates
the pinned policy/transport account binding inside the SQLite transaction.
Replacement proposals also revalidate the authenticated approval and the
target's account and unresolved state there, so account rebinding or a
concurrent reconciliation cannot consume an immutable operation ID with stale
authority.

Submission for approval and cancellation also recheck the pinned account
binding inside their state-transition transaction, so rebinding cannot mutate
work owned by the former account.

Before any replacement provider request, the same transaction also reserves
its unresolved target. Reconciliation takes an exclusive claim on that target.
These durable claims serialize the operations, closing the window where
reconciliation could confirm the original after the last policy check but
before replacement publication. Replacement final-state and crash-recovery
transitions delete both reservations in the same SQLite transaction. A process
death during reconciliation leaves its read claim fail-closed; clearing that
claim requires a fresh, current `recover_reconciliation` operator approval
through the explicit restart-recovery path before reconciliation can resume.
The reconciliation worker holds its own operation-scoped OS liveness lock
through the provider call, and recovery must acquire that lock before clearing
the durable claim, so recovery cannot overlap the original worker. Every
recovery occurrence receives its own durable audit identity, including when the
same still-current approval authorizes more than one recovery.
Each reconciliation call also requires a unique application correlation. Its
configured-account actor and `started` record commit atomically with the durable
claim before the provider read. Normal success, rejection, inconclusive result,
invalid result, and provider failure complete that append-only attempt record;
a process death leaves the `started` evidence intact for authorized recovery.
The pinned account binding is rechecked after the claim, after the provider
read, and inside the final classification transaction; rebinding leaves the
operation unknown and the claim fail-closed.
Both reconciliation-claim recovery and permanent abandonment validate the
provider-returned purpose-bound approval against a freshly sampled clock
immediately before their durable administrative change, then revalidate the
pinned policy/transport account binding after that lookup and before commit.

## Inbound replies, mentions, and conversation outcomes

Polling is split into `reply` and `mention` streams and requires the configured
account and stream-specific scopes. Each verified event records provider event
identity, source reference, account, kind, conversation ID, optional parent,
provider event time normalized to UTC, and bounded public content. Equivalent
aware timestamps replayed with another UTC offset retain the same identity. An
event's receipt time is normalized to UTC, and the pending queue orders both new
and migrated offset-bearing values by their exact receipt instant. An event page
and its cursor commit in one SQLite transaction. Each inbound event
and conversation outcome also persists a bounded application correlation ID so
ingestion, deliberation,
and a later reply operation can share an internal trace without treating a
provider ID as authority. A crash before commit safely replays; a replay after
commit is suppressed by the `(account, stream, event ID)` identity. The same
provider event ID remains valid in another account or stream. Reuse within one
identity with different immutable event data is rejected as a conflict, and
the page cursor is not advanced. Conversation outcomes retain the same scoped
identity in their foreign key.

The loop has hard page, event, and elapsed-time limits. Before an atomic page
commit it passes only the remaining time budget to SQLite's lock wait, then
checks the deadline and live polling policy after acquiring the write
transaction and checks both again after staging events, immediately before the
cursor and audit writes; an expired or newly unauthorized transaction rolls
back every event and the cursor. The same commit records a bounded account
actor, action, and caller correlation even for empty or duplicate-only
successful polls. The audit identity is bound to the starting and committed
cursor range, so reusing it for later progress is rejected and rolled back. It
rejects account/source mismatches, invalid source evidence, stale cursor commits,
repeated cursors, and oversized pages. Public text is inert data even when it
claims to be Meta, the owner, or an administrator. No inbound event calls a
model or creates a reply by itself.

Before the first provider page fetch, polling durably records a unique caller
correlation, bounded account actor, stream, and `started` status. Success is
completed atomically with events and cursor; policy denial, interruption, and
ordinary failure record terminal outcomes, while a real process exit leaves the
`started` evidence for diagnosis. The attempt identity is reserved before the
policy decision, so a denied correlation cannot later be reused after policy
restoration. Reusing any completed correlation is rejected before another
provider read.

Version-6 poll actions lacked cursor-range evidence. Migration rebuilds their
table with the version-7 `NOT NULL` invariant and marks those rows explicitly as
legacy. Because the missing range cannot be reconstructed safely, reuse of a
legacy correlation fails closed with a requirement to supply a new correlation.

Conversation handling records `no_action`, supported `defer` with a resumption
condition, `terminate_conversation`, or `cooldown` separately from delivery
outcomes. `defer` leaves the inbound event unprocessed, and the durable
`deferred_inbound_events` query returns the event, reason, resume condition, and
defer timestamp after restart. Deferred work is excluded from the ordinary
bounded pending window so unresolved deferrals cannot starve newer events.
Deferral timestamps are stored in UTC and latest-outcome selection compares
exact microsecond instants, including migrated values with other valid UTC
offsets. Recording a later non-deferred outcome is the explicit acknowledgement
that removes it from deferred work.
Migration backfills `processed_at` only from non-deferred outcomes, and a stale
worker cannot append a defer outcome after another worker has acknowledged the
event.
A chosen public reply starts as a distinct proposal and traverses the same
approval, unknown, reconciliation, and duplicate controls as a post.

All persisted and authorization timestamps must be genuinely timezone-aware:
a non-null `tzinfo` whose UTC offset is unavailable or raises is rejected.

## Stage 3 readiness and remaining blockers

Simulation is the only executable stage. Selecting Stage 3 always reports
`live Threads transport is not implemented`. Enabling live use additionally
requires all of the following outside this PR:

- a reviewed credential-owning transport with no token exposure to models or
  this public outbox;
- verified account identity, scopes, token refresh, App Review, and restricted
  production configuration;
- integrated privacy/consent, independent-public-evidence, Russian-output, and
  semantic platform-safety gates;
- a reviewed, highly available `CurrentAuthorizationProvider` backed by the
  authenticated control plane, including revocation and consent audit evidence;
- authenticated operator controls, emergency stop, rate telemetry, owner
  alerts, and deployment approval;
- credentialed sandbox smoke tests for current endpoints, errors, quotas,
  container status, pagination, and exact reconciliation evidence;
- reviewed privacy notice, retention/deletion behavior, operational monitoring,
  and incident response.

Until those blockers are closed, real Meta credentials, live publication,
production polling, deletion, and keyword search remain disabled.
