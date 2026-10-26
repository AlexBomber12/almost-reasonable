# B11C: supervised public operating windows

The existing INT24 operator can now run a reviewed, finite operating window.
It offers due wakeups to `DurableScheduler`, alternates bounded reply/mention
polls, and processes verified stored inbound events through `ThreadsRuntime`.
Decisions can be no action, approved-catalog research, or a publication/reply
draft. No action completes that deliberation; it does not trigger another
attempt. Every draft remains in the existing outbox awaiting independent
approval. The runner never approves, dispatches, reconciles, resets UNKNOWN,
or clears an account pause.

## Prerequisites and bounds

Use the ordinary deployment's existing protected INT24 operator configuration,
operator UID, account, journals, public worker and approved fixed public mission.
Existing account grants, INT27 read-only verification, token validity, disclosure,
Stop, spend, privacy, mission and worker controls remain prerequisites. Public
research is optional and needs the existing B05B public-only application/worker
profiles, catalog, finite aggregate/per-topic quotas and research worker. Private
conversation profiles, memory, narrative and participant text do not supply
public research authority. Mission evolution is outside this window's three
outcomes; a configuration enabling it is rejected before activation.

Default `readiness` remains inert. The new `window-plan` command reads the
protected configuration and, if enabled, the public research profile; it reads
no provider token, starts no runtime, creates no journal and makes no request.
First have an operator review an `operating_window` object in that existing
configuration, plus `operating_window_reviewed: true`. The existing
`activation_reviewed: true` requirement also remains in force. This change does
not supply or modify any production configuration.

| Field | Meaning |
| --- | --- |
| `window_id` | Stable, unique reviewed identity; reuse it unchanged on resume. |
| `expires_at` | Timezone-aware ISO timestamp. At most 24 hours after first activation. Expiry is never renewed by restart. |
| `max_model_calls` | 1–4096 total admitted deliberations, shared by wakeups and inbound work. Started, lost, rejected and held work remains consumed, even when no provider request can be proven. |
| `max_poll_pages` | 0–4096 total admitted polls, each at most one page and one GET. Zero disables polling. |
| `max_inbound_events` | 0–4096 total admitted inbound deliberations, also charged to the model limit. Zero disables inbound processing. |
| `poll_interval_seconds` | Integer 1–3600 between polls, alternating replies and mentions across restart. |
| `page_size` | Optional integer 1–10, default 10. Each page fetches at most this many verified events. |

For example, limits of 6 model calls, 4 poll pages, 3 inbound events and page
size 5 admit at most 6 model calls in total, 4 polling GETs and 20 fetched events.
At most 3 of the model calls process inbound events (including already stored
backlog). If research is enabled, each deliberation can choose at most one
research operation, with no immediate research-to-model loop. Research remains
separately charged to the existing finite authority: its upper bound is the
smaller of the model limit and the public profile aggregate limit, further
reduced by already consumed aggregate/per-topic slots and worker controls. Each
research operation retains the existing one-tool-call provider contract. A
later ordinary deliberation can reuse the canonical report as untrusted evidence
with its uncertainty and mission/source bindings intact. The runner issues zero
publication POSTs. Planning reports counts, not a currency estimate or a claim
that existing quota is still available.

Each budget bounds its own lane: polling exhaustion stops polling; inbound
exhaustion stops inbound deliberation; model exhaustion ends the run. Disabled
or exhausted polling does not prevent an otherwise authorized scheduled wakeup.
Expiry or denied controls stop all new work. Existing per-call worker deadlines
remain finite; model deadlines and polling deadlines are additionally capped by
the remaining window time. An already started research request retains its
existing finite worker deadline, and its result is retained only if controls
still permit it. Idle control checks run at most 250 ms apart; interruption can
arrive while idle or in a bounded external call.

## Later operator procedure

1. Review the fixed mission, maximum work, public-only authority and backlog.
   Set the stable identity, absolute expiry and explicit limits in the protected
   configuration. Keep credentials in their existing protected files. Record the
   reviewed Git revision and configuration privately. Do not put credentials in
   arguments, environment, logs or the checkout.
2. Inspect the maximum without activation:

   ```sh
   python -m app.ops.int24_threads_runtime readiness
   python -m app.ops.int24_threads_runtime window-plan --config /absolute/existing/operator.json
   ```

3. After explicit authorization for the bounded deliberation and polling costs,
   start one foreground window:

   ```sh
   python -m app.ops.int24_threads_runtime run-window --activate-live --config /absolute/existing/operator.json
   ```

   The operator need not issue a command per wakeup or inbound event. A competing
   runner using these account journals returns `busy` without admitting work.
4. Use Ctrl-C or normal SIGTERM to stop the foreground command. The JSON summary
   classifies the exit, consumed limits, next scheduled work, scheduler outcomes, decisions, polling
   and research outcomes, and pending draft/operation IDs. It contains neither
   draft content nor raw inbound/provider data. Owner Stop also stops admission;
   this command does not clear it. A failed or interrupted command never proves
   that a started paid operation had no effect.
5. Resume by running the **same command with the same configuration and stores**
   while the original expiry and controls still permit it. Reusing the window
   ID with changed limits or expiry is rejected. Privacy generation, mission and
   public research authority remain bound to the first activation. A changed
   binding is denied, rather than renewed on restart. Do not reset journals,
   invent replacement IDs or create another window to recover uncertain work.
   A fresh operating window needs its own independent review and authority.
6. Inspect a returned pending operation with the existing command:

   ```sh
   python -m app.ops.int24_threads_runtime draft OPERATION --activate-live --config /absolute/existing/operator.json
   ```

   Separate `approve`, `dispatch` and `reconcile` commands remain the publication
   boundary. Follow [B11B's publication/readback procedure](b11b-threads-publication.md)
   and the existing independent approval rules. Running a window does not grant
   unattended publication authority.

## Persistence and evidence limits

Window configuration, control bindings, monotonic watermark, due times,
sequences and work admissions are stored in the existing runtime journal. A
model slot commits in the same transaction as its existing `started` intent.
The journal's existing single-host OS lock serializes window runners across
processes and is released on process exit; no SQLite transaction spans HTTP or
worker calls. Recovery is limited to the current window's admitted operations.
The application requires a verified public mission before window activation.
An absolute model deadline commits with each admitted intent and travels as an
optional timestamp in the existing secretless public-worker request. The client
and worker recompute the remaining timeout after control/journal and IPC delays,
before provider dispatch. Expiry during admission remains consumed without a
paid request or an automatic replacement. Ordinary non-window requests retain
their existing exchange. Use the same reviewed revision for app and public worker.
Polling retains the absolute expiry through durable admission and policy checks,
then caps each transport timeout and page commit against the remaining time.
An observed expiry remains closed across restart and clock rollback through
the existing durable watermark.
Existing scheduler claims, coalescing, selected cadence, duplicate suppression,
active hours, action limits and cooldown still decide wakeup eligibility.
Downtime offers at most one current tick and schedules the next a full cadence
later; it does not replay missed intervals. An admitted poll is never resent
under its original identity. Subsequent scheduled polling may see the same
public event, which the existing verified-event and intent stores suppress.
Started inference/research without a durable response remains consumed and is
never automatically retried. Existing finite journal capacity remains a hard
stop, not permission to discard consumed identities.

The connected tests use controllable clocks, real SQLite journals and worker
IPC, fake inference/research SDKs and fake HTTP. They verify request counts,
public canonical research, later unverified evidence, exact durable admissions,
competing runners, restart, changed controls, interrupted/lost responses and
pending drafts with zero publication POSTs. These tests establish supervised
autonomous deliberation contracts. They do not establish actual Meta access,
live model quality, publication/readback, unattended publication authorization,
ordinary-host deployment readiness or complete MVP acceptance. No deployment,
production configuration, credential or live OpenAI/Meta call is part of B11C.
