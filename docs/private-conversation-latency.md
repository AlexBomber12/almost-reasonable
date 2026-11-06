# Delivering a private reply before optional maintenance

The private messages endpoint returns the current authorized turn alongside its
existing response fields. These are persisted event identities and the released
reply, not a second model response or streamed unvalidated tokens. The browser
renders that turn immediately and synchronizes history in the background. A late
history request cannot replace a newer turn. History failure leaves the delivered
turn visible with a synchronization warning.

The page binds messages and history to its authenticated account using
`X-Chat-Account`. If another tab changes the session account, these endpoints
reject the stale page before reading private data or admitting an inference.
Focus/visibility checks clear the old transcript, pending draft and recovery
storage when the account changes. Generation checks discard responses that were
already in flight. This binding supplements authentication; it grants no access.

## Optional deferred memory

`--defer-memory-maintenance` is an explicit private worker-runtime option. It
requires an enabled auxiliary memory feature and an account-authorization
callback. Generic evolution, typed memory and shared suggestions then run after
reply delivery. Explicit narrative requests remain on the reply path because the
reply depends on the validated episode. The default remains inline operation.

A marked reply operation is the durable maintenance intent. An opaque completion
binding uses the existing extraction audit; no second private transcript or raw
payload queue is created. Each stage keeps its existing source lineage, grant,
receipt and one-use worker operation. After restart, received receipts reconcile
without another provider call. An interrupted call is not blindly repeated.
Old inline turns are not retroactively scheduled when this option is enabled.

Maintenance rechecks the authenticated account, source availability, privacy
release and provider controls. Stop or spend suspension preserves pending work.
Deleted inputs cannot be processed; unrelated erased context cannot leak through
a later maintenance request. A process lock permits one drainer per provider
database. Foreground turns take priority before and between auxiliary stages.
An already submitted provider request may still finish and consume its existing
reservation. A foreground retry cannot race a delayed receipt commit.

The optional application thread retries durable work after internal failures and
logs only a fixed failure notice, without private input, proposal or exception
details. Its bounded shutdown does not claim to cancel an in-flight paid call.
This change adds no grant, budget, retry allowance or publication authority.

## Concurrency and evidence

The private HTTP server permits eight concurrent requests and preserves the
existing quiet request handler. A slow conversation no longer serializes every
HTTP request. The provider worker can still serialize inference: concurrency does
not promise two simultaneous model completions or preempt an in-flight call.

Foreground turns and deferred maintenance share one application inference lane.
A new question waits for an in-flight auxiliary call, then takes priority before
the next auxiliary stage. A completed deferred turn is revalidated and replayed
without waiting for maintenance; a still-pending duplicate waits for its original
turn without another inference. Active inline narrative duplicates retain their
nonblocking pending response and reconciliation guard. Two participants' turns
use that same lane, so ordinary
overlap does not reach the worker's single-active rejection path. Queued request
authority is checked again before input persistence; existing Stop, source and
spending checks run before the eventual inference. Read-only HTTP requests and
privacy controls remain independent of the inference lane.

Offline regressions exercise real local IPC/store authorization, receipt recovery,
source erasure and browser request races with synthetic model responses. Loopback
HTTP tests check bounded concurrency. These establish ordering and isolation,
not a production latency percentile or model quality. Measure admission, provider
completion, response delivery and history synchronization separately after an
authorized deployment; do not use private text in timing logs.
