# Integration contracts and Stage 0 evidence

This inventory follows the executable INT-01 through INT-25 synthetic slices,
including EventStore v8, ThreadsStore v10 and the independent Stage 0 journal v2.
Ordinary runtime defaults remain disabled. Explicit offline compositions use
synthetic identities, temporary SQLite/WAL stores, fake responses and ThreadsSimulator.
No fixture, H screen or recorded answer grants production authority.

## Current connected interfaces

| Owner | Actual connected boundary | Remaining boundary |
| --- | --- | --- |
| Private web | AuthStore -> Host/Origin/CSRF WSGI -> INT-19 WorkerConversationRuntime -> privacy-fenced context/reservation -> INT-22 bounded AF_UNIX worker -> fake SDK adapter -> Russian gate -> durable reply release -> session/route revalidation | Ordinary ConversationService has disabled inference. INT-22 adds a separate protected activation and durable five-operation allowance; no paid pilot, real enrollment or live-model validation occurred. |
| Private cognition | Runtime synthetic extractor -> CognitionEngine/SelfStateStore -> memory selector -> bounded private-memory v2 context and complete reply dependencies | Synthetic generic consolidation exists; live-model memory extraction and behavioral evaluation remain absent. |
| Shared intelligence | Closed constructor-bound approval -> screened/quarantined EventStore v6 revision -> authorized v3 context -> protected cross-participant reply edges | Four reviewed literal abstractions only; arbitrary prose and production approval are absent. |
| Mission | SharedMissionAdapter -> independent later exact confirmation -> durable current mission; INT-18 explicitly configured Stage 0 coordinator reads it under the privacy gate for scheduler and inbound Threads deliberation and binds delayed intents | No deployed daemon, live model judgment or generic reassessment policy. |
| Research | Authenticated private input -> INT-23 exact-input application approval -> fixed public question -> authenticated AF_UNIX public-research worker -> fake SDK search -> bounded unverified Russian reply and durable authorized replay. INT-11's earlier fixed-fixture bridge remains separate. | Default-disabled; no production grant issuer, arbitrary private-topic abstraction, semantic source-support verification or belief bridge. |
| Behavior/scheduler | SQLiteSchedulerRepository/DurableScheduler -> bounded DeliberationEngine -> closed intent/outcome -> ThreadsStage0 | A wakeup authorizes deliberation only. Uncertain started inference is held, never blindly replayed. |
| Threads | INT-24 explicitly enabled runtime -> separate bounded public worker -> durable intent/outbox -> exact local operator approval -> current control snapshot -> fake Threads HTTP dispatch/reconciliation. Verified fake inbound replies bind the provider parent. | No production approval, verified Meta account/scopes, live transport validation or semantic public-behavior evaluation. |
| Privacy | Authenticated WSGI notice/grant/revoke/delete/status -> separate ledger -> v8 erasure adapter -> gated startup replay; consistent backup -> current-ledger replay -> sealed read-only quarantine | Synthetic ingress only; no public participant dispatch, real enrollment or production restore activation. Eligibility is not activation. |
| Owner | Independently provisioned OwnerStore/OwnerApplication -> hashed sessions/CSRF/replay -> atomic stop/operation/audit. INT-25 composes the same application Stop into private and public runtimes. | INT-22 worker Stop/spend remains separately protected. No deployed global control, complete usage inspection or participant authority. |

## Authority, context and effects

Only server authentication constructs participant identity. The opt-in INT-19 path verifies the worker UID before private transmission and the worker verifies the application peer UID. INT-22 binds that application UID, a pilot identity, exact worker configuration and a window of at most ten minutes in a protected worker artifact. Its worker-owned SQLite ledger reserves at most five distinct reply operations before provider contact and does not rearm on restart or uncertain outcomes. The single provider attempt remains independently reserved by the existing gateway. Extra JSON identities,
owner claims, stored instructions and public/tool text remain untrusted. Context
filters before ranking and truncation; protected source IDs, principals and owner
markers have no memory/shared model-facing fields. Literal input remains untrusted
content, not semantic redaction. All material revision dependencies bind replies;
missing edges fail closed. Participant assertions remain unverified despite repetition.

Private reply reservation links the exact original authenticated input to bounded
inference and durable released-for-response state. Duplicate retries preserve
identity across renewed sessions. Pending uncertain inference does not repeat.
Completed means server release, not proven browser receipt. Russian gating is H;
route, capability, current privacy, stop/spend and dependency checks are independent G.

Shared approvals authorize only their closed transition and purpose. They grant
neither research nor Threads publication. Mission proposals stay inactive until a
later independent exact confirmation; changed capabilities remain forbidden.
Participant consent binds exact content, audience, purpose and validity in the
current ledger, but its CONTROL fixture construction is not authenticated UI proof.
Owner control, participant control and SyntheticOperator are distinct authorities.

Threads preserves proven no-effect, confirmed rejection/success and uncertain
transmission/response-loss evidence. UNKNOWN pauses the account; reconciliation
must establish the outcome or retain uncertainty. UNKNOWN_ABANDONED retains the
operation identity and never permits an automatic resend/replacement. All simulated
operator approvals bind exact content/account/route/purpose/current expiry/revocation.

## Independent stores and recovery

AuthStore, EventStore/provider state, PrivacyLedger, OwnerStore, scheduler,
ThreadsStore and the application journal have independent commit boundaries.
No cross-database atomicity is claimed. Pending deletion precedes application
mutation; reconciliation blocks participant reads until graph/control validation.
Restore replays even applied current ledger entries against an older snapshot.
Production switching needs an exclusive writer/read fence, current generation/inode
binding and recoverable activation intent; none is supplied here.

The owner optional-sidecar race has a controlled failing-before-fix regression.
One lstat treats only FileNotFoundError as optional absence; unsafe entries and
other errors still stop. An actual writer-lock wait followed by committed session
revocation denies the attempted mutation. Historical generic CI errors do not prove
that every prior transient failure had this cause.

## Executable traceability

[acceptance/scenarios.json](../acceptance/scenarios.json) is the sole scenario
status and exact pytest mapping inventory. CI validates it against actual full
collection, including parameter IDs. Entries distinguish component, connected
integration and process-recovery evidence, with supported scope and precise gaps.
[Stage 0 acceptance procedure](stage0-acceptance.md) explains the runner and results.
The normative [scenario contract](behavior-scenarios.md) remains the full requirement;
a PASS for a named synthetic connection is not complete scenario/MVP acceptance.

Run `python -m acceptance.run`. It invokes the complete `scripts/ci.sh` gate,
records actual setup/call/teardown outcomes and OAuth-inclusive coverage, runs frozen
recorded assessment controls, and emits JSON plus a readable report. Stage 0
release readiness remains BLOCKED on human calibration and operator review.
INT-12 closes synthetic SCN-07 consolidation; INT-13 connects authenticated
privacy controls; INT-14 connects the versioned synthetic SCN-12 matrix across
private, research, memory, mission and Threads paths. INT-18 adds connected
offline mission consumption and stale-intent holding. INT-19 adds the explicit
private HTTP to isolated fake-worker path. INT-20 records standalone public
research worker evidence; INT-21 records offline Threads HTTP contract evidence.
INT-22 connects the authenticated private path to a pilot bootstrap with a fake
SDK in a separate worker process. INT-23 adds an approved private-to-public-research
connection; INT-24 adds the public worker, approval, outbox and fake Threads HTTP
runtime. INT-25 composes them under shared application Stop and private canaries.
The connected slices do not establish semantic research verification, live-model
memory extraction, human behavioral evaluation, operational recovery or production
network and credential validation. All complete-MVP scenarios and H/M calibration
remain BLOCKED.

See [private runtime](private-conversation-runtime.md),
[private worker connection](private-worker-conversation.md),
[INT-22 pilot runbook](int22-private-pilot.md),
[private memory](private-memory-conversation.md), [shared intelligence](shared-intelligence.md),
[research worker](trusted-research-worker.md), [owner control](owner-control.md),
[durable scheduler](durable-scheduler.md), [Threads integration](threads-stage0-integration.md),
and [privacy/recovery](privacy-eventstore-integration.md) for their reviewed boundaries.
