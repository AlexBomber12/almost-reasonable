# One project and one monthly budget

Public Sol, private Sol, and Luna hosted search can share one billing project
and one $40 local conservative wallet. There are no per-lane dollar allocations.
Every SDK request must obtain an atomic reservation from the same local broker.
The independent search count remains 100 attempts per UTC calendar month.

The broker is credential-free, has no network, and owns only its ledger and Unix
socket. Workers retain separate UIDs, provider credentials, grants, Stops, and
private/public histories. OS peer credentials bind each worker to its lane;
neither a request field nor a model can choose the lane or a price. Workers and
telemetry mount the socket directory read-only and never mount the shared ledger.
The ledger directory is 0700 and its SQLite file 0600, owned by a dedicated broker
UID. Its socket directory is 0710, broker-owned and in a dedicated IPC group.

## What the budget measures

The local limit is conservative admission, **not an OpenAI invoice**. Unknown
SDK outcomes retain the full reservation across restarts and month rollover.
Settled charges use pinned upper prices including cache writes and a possible
regional premium. Provider-attested cached input gets the lower cached ceiling;
missing cache details use the full uncached ceiling, and invalid details retain
the reservation. Sol reserves $0.40 for its bounded short input and at most
2,048 output tokens; Luna reserves $0.32 for its full context and one search.
These are ceilings, not charges per request. Rejected output can still cost money
when the provider has already generated it. A rejection before SDK admission
does not create a new financial reservation.

Pricing checked 2026-11-08:

- [Sol pricing](https://developers.openai.com/api/docs/models/gpt-6.1-sol):
  short input $2/M, cache writes $2.50/M, output $10/M; regional processing +10%.
- [Luna pricing](https://developers.openai.com/api/docs/models/gpt-6-luna):
  input $0.10/M, cache writes $0.125/M, output $0.50/M; long context and regional
  premiums are included in the conservative research policy.
- [Spend limits](https://developers.openai.com/api/docs/guides/spend-limits):
  configure an enforced $40 limit on the same OpenAI project as an independent
  backstop. Provider enforcement is delayed and can slightly exceed its limit.

Local settled charges and unknown holds must be displayed separately from
provider-billed usage. All lanes show the **same shared balance**; adding those
observations together would triple-count it. Exhausted search admission does not
exhaust dollars or block an otherwise authorized Sol call. Fresh months replenish
the monthly allowance, but never erase unknown holds or duplicate-operation fences.

## Reviewed cutover

1. Confirm all three SDK clients use the same intended project and existing keys.
   Compare key equality only inside trusted memory; never print keys or digests.
   Set that project's enforced monthly limit to $40. Archive an unused project
   only with the operator's explicit confirmation of irreversible archival.
2. Assert all native Stops, wait for in-flight SDK requests and outbox effects to
   finish, and stop the three credential workers and public coordinator. Back up
   all histories, grants, provider journals, wallets, and protected configuration.
   Preserve the original Stops for guarded restoration; do not clear an owner's
   independently asserted Stop.
3. Take quiescent SQLite backups of **all three** legacy wallets. Put protected
   copies under the broker UID, then call
   `SharedWallet.initialize(path, project_id=..., sources={"sol":..., "research":...,
   "private_sol":...})` once. It requires all lanes in the same project and imports
   every operation, month, settled charge, unknown hold, search count, and clock
   watermark. Existing targets are rejected. Do not reprice or refund history.
4. Before traffic resumes, verify exact imported records and sums against the
   snapshots. Retire the **original** wallets by replacing each exact pinned policy
   with a retirement marker under its worker UID; keep all attempt rows. This
   prevents an old or accidentally restarted worker from admitting requests out of
   an independent legacy balance. Missing or changed original policy aborts the
   cutover. New workers must point only to the protected route below.
5. Start `ops/compose.shared-budget.yaml` using a reviewed immutable image, dedicated
   UID/GID, no network, no keys, and no mounts of participant state. Mount its IPC
   directory read-only into the existing workers and telemetry, adding the IPC GID
   **alongside** their existing groups. Each worker gets a 0400 route JSON in its
   own 0700 config directory:

   ```json
   {"version":"shared-budget-route-v1","project_id":"proj_SYNTHETIC",
    "lane":"sol","socket_path":"/run/project-budget/budget.sock","server_uid":11041}
   ```

   Use `private_sol` and `research` for the other routes. The existing monthly
   budget CLI/config option now points to that `.json` route. Neither the API key
   nor the shared SQLite file belongs in this document. Read-only observations
   must report scope `shared_conservative_project_wallet` and the same balance.
6. Update the public coordinator and credential workers together. The budget-v2
   provider configuration stops treating a legacy per-lane denial as the new
   policy's denial. Only an attested pre-SDK refusal with no response, tokens,
   outbox, research, or intellectual effects can get one fresh operation per new
   policy/month. Unknown provider outcomes are never automatically replayed.
7. Verify actual image/source identity, all original history/account/grant rows,
   imported financial rows, shared admission, 100-search count, owner diagnostics,
   and original Stops before restoring authorized operation. Do not use paid
   inference or a public post as an installation health check.

If the broker is unavailable, admission fails closed. It cannot silently fall
back to a legacy wallet or recreate a missing ledger. An exclusive kernel lease
on the pinned wallet inode prevents concurrent brokers and releases on process
exit. On restart, only an owned, unchanged socket with no listener can be
reclaimed, under that lease; live or foreign paths fail closed. A rollback must keep the shared
ledger and its new charges, or perform a reviewed reverse migration under Stops.
Restoring a pre-cutover wallet after new spending would manufacture credit.
