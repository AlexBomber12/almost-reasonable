# INT-05A — Private cognition and memory-aware conversation

The historical Stage 0 description below applies to the synthetic extractor.
The independently metered B03A worker extraction path, audit and recovery
contract are documented in [private-worker-conversation.md](private-worker-conversation.md).
It accepts one new generic memory or no-change from only the initiating
participant input; it does not enable synthetic extraction in the worker
runtime. The production store is now EventStore v9.

Current synthetic integration and exact scenario status are documented in
[integration-contracts.md](integration-contracts.md) and the validated
[acceptance manifest](../acceptance/scenarios.json). Historical v4/v5 inventories
below describe those schema versions; current live synthetic storage is EventStore
v7, with private/shared/generic reply edges and mission erasure. Participant privacy-control
web ingress and production restore activation remain absent.

`Stage0ConversationRuntime(..., memory_extractor=SyntheticMemoryExtractor(...))`
connects the actual cognition engine, self-state store, memory selector, privacy
read gate, reply store and offline model gateway. The constructor accepts only an
exact synthetic extractor and exact fake transport. The ordinary web service,
Stage 1 smoke request and worker protocol retain their existing v1 contract.
There is no provider, enrollment, deployment, mission or public-effect enablement.

## Ordering and durable state

The authenticated participant and exact original event identify the operation.
The existing durable reply reservation owns both extraction and inference. A
concurrent duplicate returns pending; interrupted work remains pending rather
than repeating an uncertain extraction or model call. A committed memory remains
a real state change even if later inference fails. Failed or malformed extraction
never manufactures a successful memory transition. No-change and unsupported
generic proposals retain explicit non-persisted outcomes unless trusted
`generic_memory_write` is enabled. Parent-based private consolidation additionally
requires `private_consolidation`.

Before extraction, the engine resolves the exact source and optional additional
evidence within the reconciled privacy gate. Extraction runs outside the ledger
fence. Before persistence, the gate rechecks controls and the engine rechecks
source/evidence membership and candidate revisions. SelfStateStore retains its
transactional evidence checks, compare-and-swap and operation identity checks.
All interest, belief and goal provenance, stance, confidence, rationale and
revision history remain distinct. Participant assertions stay unverified.

The runtime supplies source/additional evidence to the synthetic extractor,
without rendering current candidate text into the extraction request. Candidate
snapshots remain internal for revision authorization and CAS. This prevents a
new item from copying another item's text without retaining that item's complete
protected dependencies. When generic memory writes are enabled, the extractor
receives a deterministic subset of at most 16 private generic heads and 16 KiB
of their serialized view data. The complete generic and typed revision snapshots
are checked again in the write transaction; consolidation parents and new
revision targets must be in the exposed subset. Scripted synthetic revisions
may target a known item and expected revision; live model-guided cross-item
consolidation is not enabled.
The generic-enabled extraction path does not expose typed candidate values and
does not persist typed proposals. Typed extraction uses the separate path with
generic exposure disabled, so cross-type source dependencies cannot be omitted.
Supporting and counterevidence are not relabeled to manufacture contextual taint.
Delivery, stop and spend capabilities are rechecked after extraction; a changed
capability records a durable skipped outcome before any model contact.
The runtime's optional `evidence_event_ids` argument is a trusted synthetic API;
HTTP JSON still accepts only `message_id` and `content`.

Context is captured before extraction. A new/revised memory becomes eligible for
subsequent requests, including after restart; the current request uses its
already reserved snapshot. Memory extraction has no separate persisted decision
audit: the reply reservation suppresses replay even for no-change, failure or an
interrupted extraction. This conservative pending state has no automatic resolver.

## One bounded request and complete provenance

The memory assembler and reply reservation share one `BEGIN IMMEDIATE` SQLite
transaction under the current privacy fence. Scoped event history, authorized
memory candidates, complete revision-chain source sets, relevance ranking,
selection and reply-source binding therefore observe one consistent graph.
Authorization occurs before relevance ranking or truncation. Core read/store
APIs accept an internal caller-owned connection for this narrow composition;
they leave its commit/rollback and lifetime to the caller.

Priority is the complete current input, then up to 16 ranked active private
memory records, then recent whole participant messages. Suspended records retain
their explicit status; completed/abandoned records are excluded by the selector.
One UTF-8 budget counts the complete serialized v2 request, including labels,
JSON escaping, Core Self and envelope overhead. A current input that cannot fit
is rejected. Whole memories/messages that cannot fit are omitted with explicit
flags. Ranking uses a bounded 1,024-character topic prefix, never a truncated
model-visible message. Earlier agent replies remain display/history content;
agent-authored inference roles are not enabled in this extension.

`model-contract-private-memory-v2` contains the approved immutable Core Self,
v1 participant segments and closed memory values: kind, text, status, confidence,
stance, priority, rationale, alternatives, constraints, review condition,
evidence counts and independent verification/provenance classifications.
Every memory carries `untrusted_memory_data` and `can_instruct=false`. Protected
participant IDs, item IDs, revision keys, source IDs, owner markers and privacy
controls are absent from the memory projection. Literal participant text remains
untrusted data; this is not semantic redaction of information the participant
chose to write. Classification and field/collection bounds are validated.

The Stage 0-only fake adapter replaces the fake transport's input with
this explicit v2 serialization. Execution-local payloads isolate concurrent calls; the same gateway retains
its fail-closed persistence state across requests. The unchanged v1 gateway owns provider
reservations, closed reply-proposal parsing, spend/stop checks and error handling.
It cannot accept v2 through the real provider builder or worker protocol. Provider
telemetry retains the v1 wrapper configuration; the durable reply records
`private-memory-stage0-v2` as its context/prompt version. No new real-provider
capability is inferred from synthetic success.

Protected reply dependencies come from storage, never model text. Every included
memory contributes its complete revision-chain source-event union, even when
sources lie outside the bounded history window. Those sources and all included
participant events are bound to the durable reply operation. A memory whose
complete dependencies exceed the 100-source operation bound is omitted; its
source set is never truncated. Missing dependencies fail closed. Reservation,
persistence and release revalidate live scoped sources in their own consistent
transactions. No new content-bearing table or migration is needed.

## Deletion, restart and evidence

The existing INT-03/INT-04 erasure adapter removes source payloads, all tainted
item revision payloads and every dependent reply payload. Retained identities
suppress deleted retries. Pending/failed reconciliation blocks reads and writes;
startup replay and old-backup quarantine apply the current external ledger.
Production restore activation remains disabled. Unrelated participant state
remains unchanged.

`tests/test_private_memory_runtime.py` exercises authenticated HTTP admission,
A/B isolation, memory-informed synthetic replies, restart, weak/new/contradictory
and uncertain belief evidence, stale/false-verification rejection, goal rationale,
malformed/unsupported extraction, interruption, concurrent duplicates, deletion
at extraction/commit/release, multi-source replies outside history, old-backup
reconciliation and injected owner/policy/shared-promotion instructions.
`tests/test_memory_conversation_context.py` covers closed request fields, combined
serialized budgets, ranking/count bounds, dependency overflow/missing edges,
v1 provider compatibility and concurrent revision exclusion during capture.
Existing INT-01 through INT-04 and INT-08 suites remain required. Fakes establish
deterministic data flow and authorization, not real-model semantic quality.

## INT-05B and complete M5 gaps

INT-05B requires durable screened shared abstractions and protected private-origin
taint; shared revisions and deletion invalidation; authorized shared-context
assembly; and independent two-phase mission proposal/confirmation. H screening
or an accepted model proposal cannot broaden an audience, publish, or activate a
mission. Behavioral evaluation must measure A/B influence and evidence-based
mission retention/change with independent confirmation and counterevidence.

Complete M5 still requires arbitrary live model-guided consolidation, durable
no-change/rejection auditing, calibrated real-model relevance and belief quality,
authorized research evidence integration, privacy-safe shared influence, semantic
injection/paraphrase-leakage evaluation and approved production enrollment,
processor/retention/deletion controls and restore activation. This PR completes
the synthetic private slice; it does not claim full M5 acceptance.

INT-05B added the explicit offline [shared-intelligence path](shared-intelligence.md)
with EventStore v6 protected cross-participant edges and mission evidence, retained
in v7. The
older component description above does not authorize production sharing.
