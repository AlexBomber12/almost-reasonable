# Coordinated goal-completion release

This is a preparation checklist, not deployment authorization or evidence of an
installed release. Use a merged release commit whose exact source has passed the
connected gate, current-HEAD CI, trusted secret scan and Codex Review. Record the
commit and immutable image digests in protected operator evidence before changing
services. Do not reuse a previous activation or wallet-initialization script.

## Before changing services

1. Obtain authorization for this specific coordinated rollout. Capture the actual
   running image digests, service arguments, mounts, ownership, health and Stop
   state. Preserve a human Stop throughout; a release must not silently resume it.
2. Hold new public and private work through the existing controls, then drain
   active work. An already submitted request can still finish. Preserve received
   receipts, uncertain provider/publication outcomes and their reservations; do
   not clear a hold to make the release proceed.
3. Take consistent protected backups of the event, privacy, provider, public
   journal, Threads, account and wallet stores and the associated configuration.
   Keep credentials and participant content out of build logs and the repository.
   Check restoration on isolated copies, including SQLite sidecar consistency.
4. Exercise migration and readers against copies with the candidate images and
   network disabled. The connected candidate expects event schema 14 and Threads
   schema 11. Confirm actual schema versions, integrity and retained record counts;
   never edit a version number to bypass a migration or reader check.

## Release as one compatible set

The private application, private model worker, public application/model worker,
public research worker and owner readers must use compatible schemas and
intellectual projections. In particular, the optional `recorded_reason` field and
intellectual instruction v5 require the strict model-worker reader to be upgraded
together with its callers. Do not point an old worker at a newly migrated live
store. Keep new work held while replacing this set and checking local IPC.

The public writer also backfills and maintains the additive
`public_state_export_aliases_v1` lookup index. Preserve the public journal and
private protected receipts as a consistent set. Read-only consumers never migrate
the producer. Verify old and new revision identities, retained private history,
derived memory and source withdrawal on copies; the index itself grants no
permission. See [historical public context](historical-public-context.md).

Keep the configured Sol primary and Luna research models and existing reasoning
settings. Preserve accounts and histories. Public operation retains its existing
$20 monthly partition ($15 primary, $5 research) and 100-search UTC-month bound;
the combined private-chat wallet retains $10 monthly. Use the existing API project.
No grant, wallet, replay identity or unresolved reservation is reinitialized.

Enable `--defer-memory-maintenance` explicitly on the private application only
after its worker and account authorization callback are ready. This moves optional
maintenance after reply delivery; it does not make explicit narrative episodes
independent of their reply. Existing inline turns are not backfilled. Keep private
hosted research disabled unless it receives its own explicit authorization.

Configure each supported telemetry process with `--monthly-budget` pointing to its
existing absolute worker-owned wallet. The new owner client accepts legacy
telemetry during replacement. An unavailable observation remains unknown and must
not be replaced with a new empty wallet. Keep public activity retrieval enabled
and preserve the existing source/ledger/account bindings.

## Verify, resume and observe

Before resuming, check service identity, schema compatibility, local authenticated
IPC, HTTP assets, separate authenticated account binding, source configuration
and owner controls without sending a paid inference or public test post. Compare
the protected before/after snapshots: balances, holds, grants, history and account
identity must survive. Normalize only documented inspection clock watermarks,
not spending or authority records.

Resume only the work covered by the operator's authorization and previous Stop
state. Then use the [user trials](goal-completion-user-trials.md) and the existing
[real-acceptance protocol](real-acceptance.md) for authorized behavioral observation.
Record POST delivery and subsequent history synchronization separately; a fast
offline test does not establish production latency. Check eventual maintenance
progress and receipt recovery without deliberately replaying a paid operation.

For rollback, hold work again and preserve all post-release receipts and uncertain
effects. An older image alone is not a safe rollback after a schema change.
Restore a compatible coordinated set and the corresponding consistent stores
only after accounting for any effects since the snapshot. If that cannot be
established, leave work held and report the specific blocker.
