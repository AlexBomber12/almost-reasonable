# Integration contracts and Stage 0 evidence

This inventory follows the executable INT-01 through INT-11 implementation,
including EventStore v6, ThreadsStore v10 and the independent INT-09 journal v1.
Ordinary runtime defaults remain disabled. Explicit offline compositions use
synthetic identities, temporary SQLite/WAL stores, fake responses and ThreadsSimulator.
No fixture, H screen or recorded answer grants production authority.

## Current connected interfaces

| Owner | Actual connected boundary | Remaining boundary |
| --- | --- | --- |
| Private web | AuthStore -> Host/Origin/CSRF WSGI -> Stage0ConversationRuntime -> privacy-fenced context/reservation -> fake gateway -> Russian gate -> durable reply release -> session/route revalidation | Ordinary ConversationService has disabled inference. Enrollment and participant privacy-control UI are absent. |
| Private cognition | Runtime synthetic extractor -> CognitionEngine/SelfStateStore -> memory selector -> bounded private-memory v2 context and complete reply dependencies | Generic memory consolidation, research evidence bridge and real-model extraction remain absent. |
| Shared intelligence | Closed constructor-bound approval -> screened/quarantined EventStore v6 revision -> authorized v3 context -> protected cross-participant reply edges | Four reviewed literal abstractions only; arbitrary prose and production approval are absent. |
| Mission | SharedMissionAdapter -> independent later exact confirmation -> durable current mission; INT-10 test composition supplies it to DeliberationEngine's existing mutable-state interface | No ordinary daemon/scheduler/Threads mission consumption or generic reassessment policy. |
| Research | Authenticated private input -> sealed exact-input application grant -> fixed public fixture/worker-local handle -> OS-authenticated AF_UNIX ResearchWorkerServer -> fake search/provenance -> bounded durable Russian reply and authorized replay | Default-disabled; no arbitrary private-topic research, production issuer, real search adapter, semantic verification or belief bridge. |
| Behavior/scheduler | SQLiteSchedulerRepository/DurableScheduler -> bounded DeliberationEngine -> closed intent/outcome -> ThreadsStage0 | A wakeup authorizes deliberation only. Uncertain started inference is held, never blindly replayed. |
| Threads | Verified simulator polling/autonomous decision -> exact public route/catalog -> journal -> durable outbox -> separately installed operator approval -> current snapshot -> simulator dispatch/reconciliation | Two literal drafts only; no general private-derived publishing, production approval or Meta transport. |
| Privacy | Pending-first separate ledger -> real v4/v5/v6 erasure adapter -> gated startup replay; consistent backup -> current-ledger replay -> sealed read-only quarantine | No authenticated participant control ingress or production restore activation. Eligibility is not activation. |
| Owner | Independently provisioned OwnerStore/OwnerApplication -> hashed sessions/CSRF/replay -> atomic stop/operation/audit; current callback composed into synthetic runtime/scheduler/Threads | Stage 1 retains its separate worker stop file. No claim of one deployed global stop, complete usage inspection or participant authority. |

## Authority, context and effects

Only server authentication constructs participant identity. Extra JSON identities,
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
recorded assessment controls, and emits JSON plus a readable report. Stage 0 remains
BLOCKED on generic consolidation, authenticated privacy
control ingress, complete cross-channel adversarial coverage and human calibration. All complete-MVP scenarios remain BLOCKED.

See [private runtime](private-conversation-runtime.md),
[private memory](private-memory-conversation.md), [shared intelligence](shared-intelligence.md),
[research worker](trusted-research-worker.md), [owner control](owner-control.md),
[durable scheduler](durable-scheduler.md), [Threads integration](threads-stage0-integration.md),
and [privacy/recovery](privacy-eventstore-integration.md) for their reviewed boundaries.
