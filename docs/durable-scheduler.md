# Durable autonomous wake-ups

`DurableScheduler` is an effect-free, one-run application boundary. It persists
wake-up identity, UTC scheduling metadata, coalesced/processed identities, last
deliberation time, action-rate history, selected cadence, execution claims and
terminal scheduler decisions. It creates no background process and has no
Threads, research, mission-write or publication adapter.

Use a dedicated SQLite path in a private application-owned directory, shared by
all instances of the same scheduler lane. Do not use an EventStore, Privacy or
owner database. The repository rejects a foreign component database and unknown
schema versions. Filesystem permissions, lifecycle and backups belong to the
application; WAL backups must use SQLite's backup API rather than copying the
main file during writes. This PR changes none of those other storage schemas.

## Claims and recovery

SQLite WAL, FULL synchronous commits and short `BEGIN IMMEDIATE` transactions
serialize admission, claims, start fences and completion. Each call opens and
closes its own connection; no SQLite transaction spans the injected deliberation
callback. One claim covers all currently due events, selecting the newest once.
Future events remain durable. Submissions made during a live claim are retained
within the total 1,024 pending/claimed-event bound. Oversized admission rolls back.

A claim has an opaque owner token and a lease covering the policy's full maximum
execution time plus five seconds for bookkeeping. Construction saturates to
`timedelta.max` only if the duration exceeds Python's representable range; the
absolute deadline clamps to the latest representable UTC instant. There is no
additional daily cap or shorter model budget. The callback still receives the
policy's original timeout. The application adapter must enforce
the supplied hard model-call timeout. Completion requires the matching token,
matching claimed event identities and an unexpired lease. A successful decision
also requires a committed start fence. Repeated starts, stale owners and
conflicting completions cannot overwrite the winner.

| Durable state | Recovery behavior |
| --- | --- |
| Pending | Eligible when due, subject to current guards and policy. |
| Claimed, start fence absent | Wait until lease expiry, then return events to pending and coalesce once. |
| Started, result not committed | Wait until lease expiry, record `failed` with no decision and mark identities uncertain. Never automatically repeat that work. |
| Completed proposal | Preserve decision vocabulary and processed identities; no authorization or external effect. |
| Completed `no_action` | Successful agent choice, retained across restart. |
| Operational failure/skip | Preserve its distinct outcome, consume the batch, and admit later new events under current policy. |

The start fence is intentionally conservative: a crash immediately after the
fence but before the provider call is also uncertain. A crash after a model
return but before completion never manufactures `no_action` or a proposal.
Uncertain identities remain durable beyond the duplicate-retention window and
are not automatically retried or pruned. Resolving them requires later explicit
application reconciliation, outside this PR. Terminal outcome inspection is
bounded to the latest 4,096 runs; uncertain identity fences survive this pruning.
Ordinary completed identities expire under `duplicate_window` and have the
existing 4,096-entry bound. Reuse after normal expiry follows existing scheduler
semantics; a gateway must still enforce its own durable operation identity.

Wall-clock deadlines use UTC instants. A durable clock watermark includes the
completed deliberation timestamp and rejects execution during clock rollback.
Ordinary completed identities expire before rollback admission; uncertain
identities retain their fence. Arrivals are admitted transactionally before
deferring work, so both new and legitimately reusable one-time events survive
restart and become eligible when the clock catches up. Local active-hour policy still uses the injected
clock's timezone, including overnight windows and DST folds. The application
must provide its chosen timezone consistently across restarts.

## Application controls and autonomy

Compose `owner=OwnerStore` and `provider=ProviderStateStore`, using only their
`active()` and `is_spend_suspended()` accessors. These narrow protocols add no
authentication or reset authority. Non-boolean control values and accessor
failures abort work. OwnerStore retains its existing fail-closed stop behavior.
The scheduler reads current guards before admission/recovery and again directly
before the start fence. It rechecks after deliberation and discards a successful
result if stop or spend suspension became active. Such a discarded result adds
no admitted action-rate entry. A stop cannot undo a provider call already in
flight; the provider adapter remains responsible for guarding its call boundary
and bounded retries.

Stops and spend suspension consume due batches as operational skips. Clearing
stop creates no catch-up loop: at most one coalesced deliberation occurs per
run, and uncertain work is never replayed. Persistent provider suspension is
owned solely by the existing provider-state store. Clearing stop changes no
capabilities and enables no public effect.

`repository.select_cadence(timedelta(...))` persists an application-accepted,
agent-selected positive interval exactly, including microseconds, across restart.
It preserves the existing
cadence semantics (including the technical action-rate window), active hours,
cooldown and one-iteration bound. Cadence updates during deliberation are not
overwritten by completion. Wake-up event creation remains the application's
responsibility; there is no mandatory periodic activity, quota, popularity
objective or engagement metric. `no_action` is success, while rate, budget,
stop, timeout and failures retain their existing meanings. Scheduler retries
are zero after a committed start; model retries must remain bounded by the
existing application provider adapter.

The injected callback returns `AgentDecision`, not an executable grant.
`REVISE_MISSION` remains a typed proposal boundary; only the separate mission
state machine can authorize a future mission transition. This scheduler neither
calls that state machine nor persists a mission change. Publication proposals
likewise create no dispatch, content, approval or external operation.

## Reproducible evidence

Run `python -m pytest tests/test_durable_scheduler.py` and `scripts/ci.sh`.
All fixtures use synthetic identities and temporary SQLite databases. Network
and credential entry points are forbidden in the tests; no real OpenAI or Meta
call is needed.

- `test_process_crash_recovery` exits a separate Python process with `os._exit`
  after claim, after start, and after a returned decision. Reopening preserves
  only safe retry or explicit uncertainty, with no fabricated success.
- `test_two_processes_race_for_one_durable_wakeup` releases two OS processes
  against one SQLite file and proves exactly one callback runs. The other
  process returns `execution_limit` while the winner is in flight.
- `test_concurrent_instances_do_not_call_competing_models` provides the same
  assertion with independent in-process instances and a controlled callback.
- `test_actual_owner_and_persistent_provider_suspension_composition` exercises
  actual owner provisioning/authenticated stop clearing and provider suspension
  persistence through their existing accessors.
- Remaining tests cover bounded coalescing, stale/expired/conflicting claims,
  callbacks interrupted before completion, guard changes, cadence and rate
  persistence, rollback, DST/overnight hours, later valuable events, failure
  categories, and hostile proposals without effects.
