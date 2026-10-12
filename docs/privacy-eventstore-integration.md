# INT-03 EventStore privacy integration

Current synthetic integration and exact scenario status are documented in
[integration-contracts.md](integration-contracts.md) and the validated
[acceptance manifest](../acceptance/scenarios.json). Historical v4/v5 inventories
below describe those schema versions; current live synthetic storage is EventStore
v6, with private/shared reply edges and mission erasure. Participant privacy-control
web ingress and production restore activation remain absent.

This is Stage 0 synthetic integration. `app/privacy/application.py` implements
real application erasure and quarantined restore for the exact EventStore v4
schema. No participant enrollment, inference/provider call, public action,
route change, deployment change or live database switch is enabled.

## Supported erasure

Use `DeletionCoordinator(ledger, EventStoreErasureAdapter(store))` only behind an
authenticated control boundary. `TrustedPrivacyContext` construction itself does
not authenticate an operator. `DeletionRequest.content_reference` is an exact
event ID and the supported source category is `PRIVATE_PAYLOAD`. Other source
categories, unknown/missing source identities and ownership mismatches are
blocked. Public actions retain the coordinator's `NOT_ELIGIBLE` behavior.

The coordinator commits `PENDING` intent in the independent Privacy Ledger
before application mutation. The adapter takes `BEGIN IMMEDIATE`, checks exact
schema objects, SQLite integrity/foreign keys and the declared protected graph,
then removes the source payload, retaining the content-free envelope and source
identity. It removes **every payload in every revision** of each affected state
item, including later revisions without a direct edge to the deleted event.
Rationales, alternatives and constraints disappear through the same transaction.
Every affected state's protected edges become invalidated. All affected held
reply payloads are removed and their operation/source edges invalidated. An
invalid graph rolls back; no payload-free audit receives exception text or
protected provenance. `APPLIED` is recorded only after durable application
commit; an incomplete report or exception remains `INCOMPLETE_BLOCKED`.

Retained identity rows prevent recreation: input deduplication still recognizes
the authenticated source/message tuple, state retries see invalidated historical
provenance, and reply reservation/persistence rechecks unavailable sources and
invalidated operation state. Nothing stores a content digest. New deletion
operation IDs for an already tombstoned event remain idempotent. Other
participants and unrelated events, state and replies are preserved.

Erasure explicitly sets SQLite `secure_delete=ON` and `synchronous=FULL`. The
supported guarantee is logical application removal and non-reappearance through
retrieval, retry and eligible quarantine. It is not forensic erasure of earlier
WAL snapshots, backups, filesystem/storage-device copies or external processors.
Old backups remain private assets subject to retention and quarantine replay.

## Reviewed v4 table and index inventory

| Tables | Disposition and validation |
| --- | --- |
| `schema_metadata` | Retained; exact metadata, user version and schema objects must match v4. |
| `private_payloads` | Exact owned source payload removed; orphaned, mismatched and inactive residual payloads block. |
| `event_envelopes` | Content-free tombstone retains opaque identity, principal audience, timestamp and deduplication fields; deleting payload sets `payload_id` to NULL. |
| `protected_event_taints` | Retained protected source identity; each event must have its own correctly owned assertion taint. Missing or inconsistent taints block. |
| `state_items` | Retained identity/current-revision pointer; complete numbered chain required. Any shared state blocks, because shared commits/provenance are unsupported. |
| `state_operations` | Retained retry identities; revision/item/principal bindings checked. |
| `state_revisions` | Retained control/version/status metadata; protected initiating and supporting/counter evidence required. Entire affected item history cascades. |
| `state_revision_payloads` | All affected item payloads physically deleted, including revision rationale and every state text field. Active payload/state-type and invalidation consistency checked. |
| `state_payload_alternatives` | Deleted with affected revision payloads by enforced foreign key cascade. |
| `state_payload_constraints` | Deleted with affected revision payloads by enforced foreign key cascade. |
| `protected_state_provenance` | All affected item edges invalidated; ownership, audience, type, verification, required roles and active-source availability checked. |
| `provider_spend_suspensions` | Retained provider control metadata; no participant payload, prompt or result text exists in this table. |
| `provider_attempt_reservations` | Retained attempt/correlation/provider/time identities; no prompt/result cache exists here. |
| `provider_usage_attempts` | Retained bounded technical usage/error/cost/version/response-ID metadata. No model response text, prompt or participant payload is stored. |
| `agent_reply_operations` | Retained bound identities; affected operations invalidated; declared source count, initiating edge, correlation and payload/lifecycle consistency checked. |
| `agent_reply_payloads` | All affected held reply text removed. |
| `agent_reply_sources` | All affected reply edges invalidated; exact source count and source ownership/availability checked. |

The six explicit indexes are `event_envelopes_private_scope_time`,
`state_items_private_scope_updated`, `state_revisions_item_revision`,
`protected_state_provenance_event`, `provider_usage_correlation` and
`agent_reply_sources_event`. SQLite updates these and its automatic uniqueness
indexes atomically with the mutation. No content-bearing search index exists.

A complete `ErasureReport` covers private payload and state/reply derivatives;
summary, standalone generic memory, keyed content fingerprints and populated
shared abstractions are **provably absent from this exact schema**. This is not
support for those future categories. Unknown objects/triggers/indexes, populated
shared state and missing required provenance block rather than receiving a
fabricated completeness report. V4 edges are trusted application-written
provenance, not cryptographic tamper evidence: structurally valid rewrites by a
privileged raw-SQL writer cannot reconstruct lost optional edges. Storage must
remain restricted to the reviewed application writers; suspected provenance loss
requires operator investigation, not bypassing a blocked reconciliation.
Outputs derived from memory/revisions, standalone caches and other components'
separate databases are outside this v4 contract and must acquire explicit edges
and erasure support before they can be enabled.

## Quarantine and revocation

Construct `QuarantinedRestore(ledger, 4, EventStoreRestoreAdapter)`. The adapter
uses only the callback-lifetime descriptor path supplied by recovery, opens it
with `mode=rw`, closes every connection and never initializes an EventStore or
resolves the descriptor into a replaceable pathname. Every retained deletion,
including live `APPLIED` records, is replayed against quarantine without changing
the authoritative live deletion disposition. Missing exact identities in backups
that predate source creation block; no fictional tombstone or ownership is added.

V4 contains no consent cache, grant or disclosure authorization. Revocation
callbacks validate the current ledger record and exact application schema; the
external Privacy Ledger remains the sole authorization authority. There is no
application consent state to replay or permit use from a stale backup. Dependency
rebuild validates the resulting graph and SQLite indexes; invalidated private
state stays invalidated instead of being regenerated from a stored model result.
Writer release permanently closes the adapter capability. Existing recovery
continues to enforce descriptor ownership, sidecars, WAL-consistent copying,
generation checks, fresh-inode sealing and the read-only readiness marker.

## Trusted read and activation boundaries

`PrivacyReadGate` starts closed. Trusted startup calls `reconcile()` and exposes
only its scoped event, candidate, revision-history, evidence and reply methods.
It reconciles pending/blocked entries and replays even applied entries against
the configured database, validates dependencies, and binds readiness to the
current privacy-control generation and configured database inode. An inode
change during reconciliation or before/after a read blocks the gate. Any failure
keeps it closed. This identity check is not a production writer/switch fence. Each read
holds a short Privacy Ledger write fence while checking that generation and the
absence of pending/blocked entries. Thus a preceding read finishes before new
durable intent commits; reads after that intent remain blocked until successful
reconciliation. The local lock serializes reconciliation with reads on the gate.
The fence is not held across coordinator ledger updates.

Raw storage remains a trusted internal capability; passing it directly to a
participant-facing component bypasses this service boundary. This PR does not
alter participant WSGI or Owner Control routes. Production wiring, authentication
and control ingress must adopt the gate before enrollment. A readiness marker is
quarantine eligibility evidence only. There is deliberately **no activation API**.
Activation needs all of the following in a separate independently validated PR:

1. An exclusive fence for every live application writer and reader/snapshot.
2. A ledger-generation fence binding the current controls to the exact sealed
   quarantine inode, digest, mode, size, sidecar absence and readiness outcome.
3. Durable database-switch intent and crash reconciliation across the application
   database, privacy ledger dispositions and configured database pointer.
4. Startup/service admission that cannot expose the old database, an unfinished
   switch or a quarantine through a raw storage path.

## Deterministic evidence

`tests/test_privacy_application.py` uses only synthetic principals and temporary
SQLite files. It covers A/B and same-principal unrelated-data preservation;
interest, belief and goal history; multi-source and counterevidence taint; held
reply removal and replay; missing/corrupt/unsupported graphs; duplicate and
conflicting operations; rollback before commit, interruption after application
commit and before ledger update; concurrent retrieval versus durable intent;
old WAL-consistent backup replay; revocation, incomplete reports and failed
rebuild; stale configured-store startup replay; all gated read methods; and
absence of any database activation capability. Private canaries are placed in
source/reply text, state text, rationale, alternatives and constraints and are
absent from application logical dumps and eligible quarantine logical dumps.
Existing `test_privacy_deletion.py` and `test_privacy_recovery.py` retain their
ledger monotonicity, generation race, filesystem identity and sealing coverage.

## INT-04 additive v5 release compatibility

`agent_reply_resolutions` retains only an immutable outcome and release identity,
never content. Its result/release consistency joins are included in graph
validation. Existing reply payload deletion and provenance invalidation also hide
released replies; retained release metadata cannot grant retrieval after deletion.
The explicit synthetic runtime now installs `PrivacyReadGate` at admission, model
context/reservation, result commit, release and history boundaries. Normal disabled
configuration and live restore activation remain unchanged. See
[Private conversation runtime](private-conversation-runtime.md) and
`tests/test_private_conversation_runtime.py` for actual connected erasure evidence.

`event_input_sequences` is retained event identity/order metadata. Its required
one-to-one event binding is checked by the v5 graph validator. Payload erasure
retains this sequence with the tombstone; v4 quarantine validation omits both
v5-only metadata checks while still verifying the exact v4 schema and graph.

INT-05B adds the explicit offline [shared-intelligence path](shared-intelligence.md)
with EventStore v6 protected cross-participant edges and mission evidence. The
older component description above does not authorize production sharing.
