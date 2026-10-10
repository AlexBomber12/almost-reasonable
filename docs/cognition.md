# Cognition: memory extraction and intellectual evolution

This slice connects participant-private experiences to bounded memory proposals,
authorized private self-state revisions, and privacy-first retrieval. It uses the
existing `EventStore`, `SelfStateStore`, Core Self, provenance, and self-state
screening unchanged. A model remains a proposer: it cannot write SQLite, broaden
an audience, grant a capability, declare an assertion verified, or publish shared
state.

## Concrete pipeline

An application explicitly calls `CognitionEngine.process` with a trusted
principal context and one source event ID. The engine retrieves that participant's
authorized event view before invoking the optional extractor. The extractor sees
the experience, current participant-private candidates, and Core Self version,
but no participant ID. Its configured model and prompt identity come from the
adapter boundary, not its generated output.

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

The engine verifies that the source and every evidence ID are visible in the
same authenticated participant scope. It rejects invented or cross-participant
IDs, overlapping supporting/counterevidence roles, stale revisions, invalid
status transitions, false verification, inconsistent belief confidence, and
disabled capabilities. `SelfStateStore` remains the final transactional boundary
for optimistic concurrency, operation idempotency, evidence integrity, text
screening, and SQLite persistence.

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
| `unresolved_question` | Typed `pending_not_persisted` result |
| `candidate_useful_memory` | Typed `pending_not_persisted` result |
| `contradiction_counterevidence` | Typed `pending_not_persisted` result; counterevidence attached to a belief revision is durable now |

Durable revisions retain their full version chain, rationale, initiating event,
supporting evidence, belief counterevidence, before/after confidence, decision
time, model/prompt identity, Core Self version, and private scope through the
existing store. Interest strength may rise or fall. Beliefs may change stance,
confidence, or status without becoming verified merely because a participant
said something. Goals may be activated, suspended, resumed, completed, or
abandoned; completed and abandoned goals are terminal in this boundary.

Generic extracted-memory persistence is deliberately not implemented. Adding it
requires a versioned migration with removable payloads, audience, protected
taint, review/expiry state, operation identity, and deletion invalidation. This
PR does not add an unversioned table or claim generic persistence.

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

Ranking is deterministic across topic overlap, active status, confidence or
priority, recency, and item ID. The selector emits only whole records; a record
that does not fit is omitted rather than truncated into an ambiguous fragment.
Belief records retain their explicit `supports`, `opposes`, or `uncertain`
stance and counterevidence count, so disputed claims remain distinguishable;
both fields are included in byte-budget accounting.
Every record carries its kind, private scope, status, confidence or priority,
verification state, origin-derived provenance class, decision recency, review
deadline, and the fixed `untrusted_memory_data` trust label. Model-authored
revisions are exposed as `model_inference`, never as participant assertions.
`can_instruct` is always false. No protected evidence IDs or participant IDs
are rendered into model context.
Terminal records are excluded by default and may be requested only for an
explicit revision purpose; `include_terminal` is rejected for conversation or
general deliberation.

The retrieval request can receive externally supplied invalidated event IDs and
will omit current candidates when any revision in their complete provenance
chain is tainted by one of them. `plan_source_invalidation` finds all affected
private revisions without applying the core 100-revision presentation cap, but
returns `pending_integration` and performs no durable deletion. This is an
explicit hook for the future authenticated deletion ledger, not a claim that
cascade deletion or backup reconciliation exists now.

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

- a versioned generic extracted-memory schema and persistence adapter;
- durable abstraction attempt/quarantine records and a disabled-by-default shared
  intellectual-state commit path;
- structured consent, revocation, and identifiable-disclosure authorization;
- authenticated deletion ledger, taint-complete cascade, index rebuild, and
  quarantine-before-restore handling;
- two-phase mission revision and its independent confirmation record;
- independently verified research evidence and public-fact publication gating;
- durable no-change/rejection audit and application-level model usage linkage;
- behavioral evaluation of semantic relevance, paraphrase leakage, calibration,
  and resistance to persuasion or personality drift.

This component is evidence toward SCN-01, SCN-02, SCN-07, SCN-08, SCN-09,
SCN-11, and SCN-12, but it does not by itself satisfy full M5 or SCN-07
acceptance. In particular, generic memory durability, deletion/restore
reconciliation, shared-state promotion, mission revision, and real-model
behavioral evaluation are intentionally incomplete.
