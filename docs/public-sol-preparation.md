# Public Sol preparation without activation

The public INT24 worker selects its model independently of the private operating
grant. Preparing `gpt-6.1-sol` does not start Threads, issue authority, read
credentials or make a provider call. Private dialogues and their active model,
allowance, accounts and sessions are unaffected.

For Sol, `PublicSDKAdapter` sends Responses with explicit `reasoning.effort: low`
and a finite 2048-token output ceiling, including reasoning tokens. Existing
model selections retain their previous settings. All public proposal formats,
including intellectual state and mission evolution, retain strict JSON, no tools,
`store: false`, the request's bounded deadline and one SDK call. The existing
worker creates the SDK with zero retries. Sol's higher output ceiling can change
cost and latency; this is not an increase of any operation or publication budget.
Incomplete responses still fail closed and are not automatically retried.

The compatibility baseline follows the [Sol model documentation](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
and [migration guidance](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6.1-sol).
Offline fake-SDK tests do not prove account access, real model quality or latency.

## Deliberately disabled profiles

Repository files `ops/public-sol-worker.disabled.json` and
`ops/public-sol-operator.disabled.json` are inert preparation profiles. The worker
file fixes the requested model but has `worker_enabled: false`; the operator file
has `activation_reviewed: false` and autonomous publication disabled. The real
entrypoints reject them before SDK/HTTP/runtime construction or credential reads,
even if the operator supplies `--activate-live`. Changing only the worker's
enabled flag also fails before credential access because billing scope is absent.

These are intentionally incomplete profiles, not a claim of launch readiness.
They contain no credentials, participant data, invented billing/account IDs,
authority, socket or mutable-state bindings. If installed for later review, keep
each candidate as an owner-only 0400 regular file under a 0700 protected directory,
separate from all existing active configurations. Building images or copying
these disabled files requires no service creation, restart, scheduler, `up`,
`wakeup`, `run-window`, public grant issuance or live API probe.

## Hold until private dialogue testing is reviewed

First evaluate actual private dialogues under the two independently authenticated
accounts. Preserve failures and uncertainty; a short conversation does not satisfy
formal M1–M6/SCN acceptance. Do not enable public behavior merely because CI passed.

Only after a separate public activation decision, use the existing
[INT24 operator procedure](int24-threads-runtime.md) and
[launch runbook](launch-readiness.md). Review the protected model/project/org and
actual model access; existing account, scopes and credential references; separate
operator/worker UIDs, restricted sockets, complete current journals and provider
stores; owner/native Stop; mission, privacy/provenance composition; finite model,
research, publication and time budgets; backup and recovery. Bind an enabled
configuration to the exact reviewed image revision. Preserve existing ledgers,
operation identities, expiry and spent capacity. Do not replace them with empty
state or silently extend prior authority. No reviewed activation or public grant
is supplied by this preparation.
