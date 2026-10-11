# INT-09 — Threads Stage 0 application integration

`app/integration/threads_stage0.py` is an explicitly constructed, default-off
application coordinator. It connects actual first-party components with exact
`RecordedBehaviorModel` and `ThreadsSimulator` implementations. There is no
production entrypoint, daemon, Meta transport, credential lookup, enrollment or
paid provider execution. Simulator confirmation means only simulated success.

## Component flow and authority

Autonomous input goes through `SQLiteSchedulerRepository` and `DurableScheduler`
to a bounded `DeliberationEngine`, the application public-route policy,
`RussianOutputPolicy`, the durable application journal and `ThreadsService`.
An accepted scheduler decision is separate from an outbox proposal, and both are
separate from a confirmed publication. `no_action` is a durable successful
behavior outcome with no outbox item and no publication target or quota.

Inbound replies and mentions enter only through verified bounded
`ThreadsService.poll`. The exact account, simulator source, stream, provider
event ID, conversation, parent, occurrence time and correlation remain durable.
`process_inbound` uses the existing pending/deferred queries and scoped
conversation controls. It supplies only that event's bounded public text to
behavior. It never retrieves EventStore private data or shared-memory content.
Incoming owner/Meta/tool/approval claims remain inert untrusted text.

The application owns the exact configured account and determines POST versus
REPLY. A reply targets the verified incoming provider event ID, not a model
field or the incoming event's own parent. The existing private `ActionPolicy`
requires an authenticated private principal in `TrustedActionContext`; it has
no applicable public principal here. The coordinator therefore enforces its own
account/route binding rather than inventing a private participant or issuing a
private reply grant. Ordinary behavior outputs remain closed typed proposals.
Research, private-source drafts, arbitrary shared state, non-conversation
termination scopes and other unsupported decisions are rejected without saving
rejected text. Operational rejection is never rewritten as `no_action`.

## Synthetic public-content and privacy boundary

Only the two literal Russian drafts in `PUBLIC_DRAFTS` are reviewed for this
slice. They express general intellectual interest or request clarification;
they contain no quotations, personal facts, source-specific assertions,
credentials, prompts or operational metadata. Exact catalog membership is
required at decision, recovery and authorization. It does not claim a general
semantic publication screen. A failed Russian gate holds/rejects the draft;
its static private fallback is never turned into a public post. Threads' existing
500-character and UTF-8 limits still apply.

`Stage0Authorization` implements `CurrentAuthorizationProvider`. Publication
snapshots bind exact immutable account/operation/type/parent/draft/content,
public provenance, correlation, source eligibility and the configured synthetic
operator actor. The current approval's purpose, expiry and revocation are
validated by the existing service. Source-catalog revocation/deletion is durable
and is not restored on restart. Inbound source lookup must retain the exact
reviewed metadata; missing or changed sources block later effects. Reply
provenance also rechecks the current scoped conversation control before handoff,
READY and each provider effect; an active termination or cooldown holds the reply.

The real `PrivacyLedger.control_generation()` is checked, not a model Boolean
or `SimulationAuthorizationProvider.set_privacy(True)`. Only synthetic public
sources qualify: this adapter issues no private consent and cannot authorize
identifiable disclosure or protected shared provenance. Any ledger generation
change conservatively invalidates previously generated publication intents;
this may hold unrelated synthetic drafts and deliberately avoids claiming a
fine-grained private-public provenance implementation. Missing ledger/control
state fails closed. An AbstractToShared screen or INT-05B approval grants no
Threads permission. A future private-derived publication must represent its
complete protected dependencies through a separately reviewed interface or
remain rejected.

`SyntheticOperator` is a constructor-only fixture, disabled by default. The
application accepts only the exact enabled fixture object installed at
construction. `approve` records exact immutable draft binding, purpose, expiry
and actor in the journal before constructing `issue_approval` evidence. No HTTP
JSON, public text or model output is parsed into operator authority. Revocation
is permanent for a reference; repeating approval cannot resurrect it. A changed
expiry yields a separate approval identity. The fixture also supplies explicit
purpose-bound abandonment/reconciliation-recovery approvals through the existing
service; it does not grant replacement or autonomous resend authority. Current
administrative lookup also requires the configured operator to remain enabled
and match the original approval actor after restart.

Owner stop and provider suspension are sampled through `OwnerStore.active()`
and `ProviderStateStore.is_spend_suspended()` before model work and in every
publication authorization snapshot. A narrow `ThreadsService.poll_guard`
rechecks those live controls at its existing polling boundaries, including the
page/cursor commit. Resetting stop preserves existing approvals and unknown
states; it does not issue approval or release account uncertainty. Policy
capabilities and scopes remain independent checks. Inactivity requires no
publication capability.

## Identity, durable handoff and recovery

The dedicated application journal is version 1, SQLite WAL with FULL synchronous
commits. It rejects foreign databases, unsupported versions and changed account
binding. It retains at most 4,096 inference identities and 4,096 approvals; hitting
a bound fails closed rather than forgetting effects. Application IDs are bounded
SHA-256 identities over exact account plus wake-up ID, or account/stream/inbound
ID plus initial/resume attempt. Model text never participates in operation IDs.
Wake-up correlation is the opaque operation identity; inbound correlation is the
original verified event's correlation. The journal stores only synthetic-safe
public drafts and payload-free decision/origin evidence, never private text or
public attack payloads.

| Boundary | Durable evidence and restart behavior |
|---|---|
| Scheduler reserved but not started | Existing scheduler claim/lease handles safe admission recovery |
| Before deliberation after scheduler started | Scheduler uncertainty; no model call is inferred or retried |
| Journal `started`, including interrupted model or lost result | At-most-once inference fence; never automatically repeat |
| Complete decision before journal intent | Remains started/uncertain if interrupted; no invented draft |
| Journal `intent` before scheduler completion | Actual draft survives; held pending exact durable scheduler acceptance |
| Scheduler accepted, before outbox handoff | `recover` matches accepted decision and wake-up; creates original operation |
| Outbox created, before approval submission/link | Same immutable operation reused; submit only if still proposed |
| Reply outbox durable, before inbound acknowledgement | Exact outbox/source/correlation checked and acknowledged idempotently |
| Non-public outcome committed, before journal link | Outcome identity lookup avoids duplicate outcome; preserve control/defer |
| READY, before dispatch | Existing service rechecks current approval/privacy/controls |
| SENDING before/after possible transmission | Existing durable dispatch evidence and liveness lock govern recovery |
| Container response lost, publish uncertain, success response lost | UNKNOWN and account pause; no automatic resend or alternate identity |
| Reconciliation interrupted | Existing attempt/claim evidence; explicit purpose-bound recovery approval |
| Inbound page fetched before cursor commit | Existing atomic event/page/cursor transaction safely replays |

The journal uses an operation-scoped OS lock to serialize handoffs, consistent
with the existing single-host SQLite outbox assumptions. Atomic journal claims
serialize two inbound processors. Scheduler inference claims remain owned by the
existing durable scheduler. No SQLite write transaction spans model execution
or provider effects. No atomicity across independent databases is asserted.
A scheduler outcome pruned before handoff, or a crash leaving intent without
accepted scheduler evidence, remains held rather than guessing policy acceptance.
`recover()` returns blocked intent identities; failed handoffs do not stop
independent intent recovery.

A public reply has a separate journal-to-outbox link and native `processed_at`
acknowledgement validated against that durable outbox item. This is **proposal
processing**, not successful reply publication. It is never represented with a
fake NO_ACTION outcome. Already claimed/held/rejected event IDs are excluded from
bounded pending queries so one interrupted event cannot starve unrelated work.

`no_action` uses the existing durable reason/category outcome. Defer accepts only
`new_public_evidence`; an explicit application `resume=True` represents the
trusted synthetic fixture's decision that this condition is met, never a message
instruction. At most one resumed inference is allowed per event; repeated defer
on that resume is rejected. Termination is restricted to the exact conversation
and stream; positive cooldown seconds produce an explicit durable expiry.
Independent accounts/streams/conversations remain separate. A later event after
cooldown can become eligible.

## Outbox and unknown effects

The coordinator calls `propose` and `submit_for_approval`; an explicit operator
then uses `authorize` and `dispatch`. The lifecycle remains
`proposed → awaiting_policy_or_operator_approval → ready → sending → confirmed`.
Container creation and publication occur only inside `ThreadsService` through
the exact simulator. Existing current snapshot checks run before READY, inside
its transition, inside SENDING, and immediately before each provider effect.

`recover_sending`, `reconcile`, `abandon_unknown` and
`recover_reconciliation_claim` remain existing service operations with their
existing account reservations, attempt bounds, verified evidence and attribution.
There is no duplicate transition implementation. Uncertain effects remain
unknown/unknown_abandoned; no automatic retry or new identity is permitted.
Likely-duplicate fingerprints concern only catalog public text and are warnings,
not Meta idempotency evidence. Replacement remains a separately approved
component capability and is not exposed by this coordinator.

## Connected evidence and remaining blockers

`tests/test_threads_stage0_integration.py` connects actual scheduler, behavior,
owner/provider stores, Privacy Ledger, outbox, polling and simulator. It covers
autonomous post/inactivity, reply and mention, defer/resume, scoped controls,
closed-schema/privacy/Russian rejection, durable approval revocation/expiry,
source removal, stop/spend timing, crash recovery, reconciliation and concurrent
processors. Its live-dependency guards fail on sockets, DNS, HTTP connections or
credential transport construction; simulator `network_calls` remains zero.
Preserved component tests in `test_threads_outbox.py`, `test_threads_polling.py`,
`test_behavior_deliberation.py` and `test_durable_scheduler.py` separately cover
the full component fault/transition contracts. Component coverage and connected
application evidence are distinct; neither is complete-MVP acceptance.

Stage 3 remains blocked on reviewed real Meta transport/scopes, App Review,
credential lifecycle, verified provider smoke evidence, live reconciliation,
restricted production approvals, privacy/retention/enrollment decisions and
platform/Russian behavioral evaluation. General public drafts, private-derived
publication authorization, safe production restore, multi-host coordination,
indefinite journal retention and general defer scheduling require separate
reviewed work. Live deletion, keyword search, production deployment, participant
enrollment and actual publishing remain disabled. No scenario is promoted to
complete solely because this synthetic path passes.
