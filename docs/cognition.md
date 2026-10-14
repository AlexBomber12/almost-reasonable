# Cognition: memory extraction and intellectual evolution

INT-05A connects these components to the opt-in synthetic
[private-memory conversation](private-memory-conversation.md). The component
description below retains the original standalone pipeline boundaries.

This slice connects participant-private experiences to bounded memory proposals,
authorized private self-state revisions, and privacy-first retrieval. It uses the
existing `EventStore`, `SelfStateStore`, Core Self, provenance, and self-state
screening unchanged. A model remains a proposer: it cannot write SQLite, broaden
an audience, grant a capability, declare an assertion verified, or publish shared
state.

## Concrete pipeline

An application explicitly calls `CognitionEngine.process` with a trusted
principal context, one source event ID, and an optional bounded tuple of evidence
event IDs. The engine resolves that participant's authorized event view before
invoking the optional extractor. The extractor sees the initiating experience,
the resolved additional evidence, current participant-private candidates, and
Core Self version, but no participant ID. Its configured model and prompt
identity come from the adapter boundary, not its generated output.

The persisted decision timestamp comes from an application-owned clock injected
into `CognitionEngine`; model-supplied `proposed_at` is only validated proposal
metadata and never controls durable recency. Duplicate comparison normalizes this
trusted timestamp so a delayed retry still resolves to its original revision.

The deterministic path is:

```text
authenticated private event
  -> bounded ExtractionRequest (untrusted experience)
  -> optional MemoryExtractor proposal
  -> closed proposal/type and capability checks
  -> source/evidence scope and provenance checks
  -> revision/status/confidence/idempotency checks
  -> existing SelfStateStore transaction
  -> private candidate or explicit non-persisted outcome
```

The engine verifies that the source and every evidence ID are in the exact
participant-scoped material supplied to the extractor. It rejects invented,
unexposed, or cross-participant IDs, overlapping supporting/counterevidence
roles, stale revisions, invalid status transitions, false verification,
inconsistent belief confidence, and disabled capabilities. `SelfStateStore`
remains the final transactional boundary for optimistic concurrency, operation
idempotency, evidence integrity, text screening, and SQLite persistence.

The core conversation/state retrieval methods deliberately cap model-facing
results at 100 records. A cognition-owned read-only adapter performs exact
participant-scoped identity and revision lookups so those presentation caps do
not become authorization limits. It also checks durable operation identity
before current-revision validation, preserving duplicate retry semantics after
later revisions. The adapter uses only the existing versioned schema and has no
write or migration path.

No-change is a first-class result. Insufficient evidence does not force a new
interest, belief, or goal revision. It currently has no durable audit record
because the existing schema has no general cognition-decision table.

## Supported memory kinds

The proposal vocabulary is closed:

| Kind | Current result |
|---|---|
| `durable_interest` | Durable participant-private interest candidate and revisions through `SelfStateStore` |
| `belief_or_hypothesis` | Durable participant-private belief candidate and revisions; participant assertions remain `unverified` |
| `goal_or_intention` | Durable participant-private goal candidate and controlled lifecycle revisions |
| `unresolved_question` | Durable private generic revision when trusted `generic_memory_write` is enabled; otherwise `pending_not_persisted` |
| `candidate_useful_memory` | Durable private generic revision when enabled; supports exact private parent revisions |
| `contradiction_counterevidence` | Durable private generic revision when enabled; contrary source roles remain protected and unverified |

Durable revisions retain their full version chain, rationale, initiating event,
supporting evidence, belief counterevidence, before/after confidence, decision
time, model/prompt identity, Core Self version, and private scope through the
existing store. Interest strength may rise or fall. Beliefs may change stance,
confidence, or status without becoming verified merely because a participant
said something. Goals may be activated, suspended, resumed, completed, or
abandoned; completed and abandoned goals are terminal in this boundary.

INT-12 adds opt-in generic persistence through EventStore v7. `GenericMemoryProposal`
binds an immutable operation, item and expected revision, initiating/supporting/
counterevidence events, exact parent item/revisions, bounded statement and
confidence, unverified status, trusted `pending_review` state and review time.
The engine resolves the complete private revision snapshot before synthetic
extraction. It exposes at most 16 generic heads and 16 KiB of serialized generic
view data to the extractor; a proposal may name only exposed parents or revision
targets, except for exact replay of an existing operation. The store
rechecks the complete generic and typed candidate revision snapshots, source
evidence and parent heads inside one SQLite write transaction.
Generic-enabled extraction hides typed candidate values and accepts only generic
proposals or no-change outcomes. Typed extraction runs with generic memory
exposure disabled. This keeps cross-type derivatives from losing source
dependencies during erasure. Every raw evidence event supplied to generic
extraction must appear in its persisted supporting or counterevidence roles.
`private_consolidation` must also be enabled for parent references. An identical
retry returns its original revision even after later revisions; changed bindings,
stale heads, other participants' parents and unsupported sharing fail closed.

The v7 generic tables separate `generic_memory_items` and immutable operation
identities from removable `generic_memory_payloads`. Item and operation IDs are
scoped to each participant, so matching model-generated IDs remain independent
through reads and erasure. Protected source roles and exact parent-operation
edges retain the full union across prior item revisions
and consolidation levels, with a 100-edge and 32-revision bound. The graph digest binds retained
content-free edges after deletion; active replay bindings live only with
removable payloads. `protected_reply_generic` records which exact generic
operations a later reply used. The validator checks counts, ownership, role
union, parent cycles, exact replay bindings, canonical UTC decision/review times
and reply dependencies before erasure or
read eligibility. The synthetic v7 graph is capped at 128 lifetime generic
operations and 4,096 generic reply links per database, so whole-graph validation
has a finite cost; writes at either limit fail closed. Deleting any material
source invalidates the entire affected item chain, parent descendants and
dependent replies, while retaining only
content-free recovery metadata. Old backups replay the external ledger into
quarantine before reads are eligible. No live extractor or automatic periodic
consolidation is enabled.

## Private-to-shared abstraction

`AbstractToSharedProposal` is separate from ordinary extraction. Its public-shaped
candidate contains only a closed shareable kind, abstract value, confidence, and
verification status. Protected source event IDs live in a separate audit object.
The validator requires non-empty, same-participant protected provenance and
rejects source participant IDs or event IDs in the candidate value.

The default versioned H screen rejects obvious direct identifiers, quotations,
and excessive lexical overlap with private sources. This is deliberately
conservative and does not guarantee anonymity or recognize every personal fact
or paraphrase. A failed screen is rejected; uncertainty is quarantined. Even a
candidate that passes screening is quarantined with
`shared_commit_disabled`—there is no shared-state mutation or publication path.
Consequently, a deliberately permissive H screen still cannot create authority
or cause a shared write. Protected origin data is absent from the quarantined
candidate and retained only in the internal audit result.

The attempt/result and protected audit object are not durable yet. Durable
quarantine, shared intellectual state, consent/revocation, and deletion cascade
must be integrated behind a versioned schema and authorized retrieval boundary
before shared promotion can be enabled. With only two participants, residual
source inference remains possible even for a safe abstraction.

## Bounded retrieval

`MemoryContextSelector` accepts an explicit purpose, participant context,
relevant topic, maximum record count, and total UTF-8 byte budget. It obtains the
complete participant-filtered candidate set through the read-only cognition
adapter before calculating any relevance score or applying a result cap. Thus an
older exact-topic match remains eligible beyond the core 100-record presentation
window, while a highly relevant candidate owned by another participant never
enters ranking.

Each selection reads the candidate snapshot, current decision time, and complete
revision-chain provenance inside one SQLite read transaction. A concurrent
revision therefore cannot combine old candidate text with new recency or taint.
The cognition adapter also rejects event and state stores backed by different
database paths, preventing cross-shard reads and writes.

An explicit `revision` retrieval includes every active revision of each
authorized belief rather than only its current snapshot. Each revision record
retains its revision number and rationale, stance, confidence, alternatives,
supporting/counterevidence counts, review time, decision time, origin, model and
prompt identifiers, and Core Self version. Other retrieval purposes continue to
expose only the current belief snapshot. Current revisions always rank before
historical snapshots. If a chain head cannot fit the record or byte budget, its
history is omitted too, so bounded context cannot return an obsolete stance
while omitting the current one.

Ranking is deterministic across topic overlap, active status, confidence or
priority, recency, and item ID. The selector emits only whole records; a record
that does not fit is omitted rather than truncated into an ambiguous fragment.
Belief records retain their explicit `supports`, `opposes`, or `uncertain`
stance and counterevidence count, so disputed claims remain distinguishable;
alternatives remain available for revision. Goal records retain rationale,
constraints, and review conditions, and interest records retain their reason.
All of these fields participate in relevance ranking and byte-budget accounting.
Every record carries its kind, private scope, status, confidence or priority,
verification state, origin-derived provenance class, decision recency, review
deadline, and the fixed `untrusted_memory_data` trust label. Model-authored
revisions are exposed as `model_inference`, never as participant assertions.
`can_instruct` is always false. No protected evidence IDs or participant IDs
are rendered into model context.
Terminal records are excluded by default and may be requested only for an
explicit revision purpose; `include_terminal` is rejected for conversation or
general deliberation.

Retrieval reads invalidated provenance directly from the participant-scoped
store and omits a candidate when any revision in its complete provenance chain
is tainted, without requiring callers to enumerate the deletion ledger. A
request may also provide a byte-bounded set of in-flight invalidation hints that
have not reached durable provenance yet; this supplemental set is not the source
of completeness. `plan_source_invalidation` authorizes retries from the retained
event envelope and finds active or already-invalidated affected revisions
without applying the core 100-revision presentation cap. It returns
`pending_integration` and performs no durable deletion. This is an explicit hook
for the future authenticated deletion ledger, not a claim that cascade deletion
or backup reconciliation exists now.

## Model and security boundary

- There is no provider implementation and no autonomous scan of real participant
  content. Tests use a synchronous fake extractor with synthetic output.
- Extracted text, prior memories, and self-state are data, never privileged
  instructions. They cannot change Core Self, policy, permissions, tools,
  credentials, budgets, shutdown state, or deployment configuration.
- Operational capabilities are trusted constructor inputs and are never read
  from model output or participant text.
- Owner markers do not broaden conversational scope or add epistemic weight.
- H screening is defense in depth. Authorization comes from closed types,
  authenticated scope, evidence membership, state-machine checks, capabilities,
  and the existing transactional store, even when a test H screen deliberately
  allows malicious text.
- Duplicate operation IDs are delegated to the existing durable idempotency
  mechanism. Reusing an ID for a different logical change fails closed.

## Integration still required

The following remain cross-stream work:

- durable abstraction attempt/quarantine records and a disabled-by-default shared
  intellectual-state commit path;
- structured consent, revocation, and identifiable-disclosure authorization;
- production privacy control ingress and restore activation; INT-03/INT-04
  adapters now cover private state and INT-05A memory-derived replies;
- two-phase mission revision and its independent confirmation record;
- independently verified research evidence and public-fact publication gating;
- durable no-change/rejection audit and application-level model usage linkage;
- behavioral evaluation of semantic relevance, paraphrase leakage, calibration,
  and resistance to persuasion or personality drift.

This component contributes connected synthetic SCN-07 evidence without completing
M5. Production restore activation, participant privacy controls, arbitrary live
model consolidation and calibrated behavioral evaluation remain incomplete.

INT-05B added the explicit offline [shared-intelligence path](shared-intelligence.md)
with EventStore v6 protected cross-participant edges and mission evidence, retained
in v7. The
older component description above does not authorize production sharing.
