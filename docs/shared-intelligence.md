# INT-05B — Synthetic shared intellectual state

This path is explicitly constructed by a Stage 0 application. It uses synthetic
principals and `FakeOpenAITransport`; the HTTP, worker and real provider v1
contracts remain unchanged. Production promotion, enrollment, restore activation,
publication, research dispatch and deployment are disabled.

## Ownership and authorization

| Boundary | Owner and authority |
|---|---|
| Private source envelopes and removable payloads | EventStore; authenticated same-principal input and retrieval |
| Abstract proposal | Cognition's existing closed `AbstractToSharedProposal`; never an authorization |
| H screening | Existing `AbstractToSharedValidator`; deterministic, versioned defense in depth |
| Exact G approval | Application-created `SyntheticSharedApproval` capability supplied at construction; exact proposal, complete source set, item/CAS revision, audience, conversation purpose and separate default-off mission-evidence grant |
| Shared revisions, attempts and replay identities | EventStore v6 `shared_operations` and removable `shared_payloads`; no writes to private `state_items` or `state_operations` |
| Protected origin and immutable bindings | `protected_shared_sources`, `protected_shared_audiences`, removable `protected_shared_manifests`; never serialized into model context |
| Cross-participant reply dependencies | `protected_reply_shared` plus immutable `agent_reply_operations.shared_count`; ordinary `agent_reply_sources` remains same-principal |
| Mission document and evidence bindings | v6 SQLite repository implementing the existing `MissionStateMachine` repository protocol; `protected_mission_shared` holds exact operation identities |
| Deletion intent and restore authority | Existing separate PrivacyLedger, DeletionCoordinator and PrivacyReadGate |

The Stage 0 application approves only four reviewed, general intellectual
sentences in `SYNTHETIC_ABSTRACT_VALUES`. This closed vocabulary is intentional:
arbitrary prose, personal facts, recognizable paraphrases, relationship records,
source-specific explanations, and operational commands cannot be promoted even
with a permissive H callback and a fabricated chat claim of consent. This is a
synthetic integration capability, not a production consent or semantic-review
service. An ordinary passing screen still returns `shared_commit_disabled`.

Before persistence, the trusted application screens the complete material source
set, including inherited private text and protected markers. This bounded internal
read belongs to the screening boundary; no raw inherited material enters another
participant's ordinary retrieval or model projection.

Every valid-source screened operation is durable and quarantined before approval.
It retains schema version, screen version, sanitized screening result/reason,
trusted decision time, evidence classifications and immutable binding. Rejected
or unreviewed candidate text is replaced with a fixed withheld-content marker.
Its digest is protected replay metadata and is removed on source erasure; no
content-derived fingerprint remains in tombstones. Malformed proposals or missing
sources are rejected before persistence and cannot become screened approvals.

## Revision and context behavior

An item has at most 32 revisions. Each revision has at most 50 distinct material
sources and 16 explicit conversation audiences. The new author's source set is
resolved within that author's existing private scope. Earlier material sources
are inherited in full; a revision may narrow its parent's audience but cannot
expand it. Exceeding a dependency bound rejects the change rather than dropping
sources. Competing, stale revisions fail compare-and-swap. Identical retries
return the existing operation, including after restart and later revisions;
changed content, source identities, classification, audience or proposal metadata
under the same operation ID is rejected.

Retrieval authorizes audience and purpose before deterministic item/revision
ordering. Only a current approved head is returned. Historical competing text is
retained as alternatives, and classifications/counts distinguish counterevidence
from supporting or inherited dependencies. A participant assertion never becomes
verified. No participant, source-event, manifest, or protected operation identity
is present in the model projection.

`Stage0ConversationRuntime(..., shared_intelligence=...)` combines its existing
private memory component and the independent shared component in the closed
`model-contract-shared-intelligence-v3` request. Both remain explicitly
`untrusted_memory_data`, with `can_instruct=false`, under unchanged Core Self.
The final complete serialized UTF-8 request respects the configured budget and
provider request bound. Optional material is selected as whole records; omitted
records contribute no reply edges. A small envelope allowance is reserved while
assembling the private component. Combined material dependencies are bounded at
100; a shared record is omitted whole when its complete dependency set cannot fit.

Reply reservation, its private source edges and exact shared revision edges commit
in one EventStore transaction before inference. No cross-participant source is
inserted into `agent_reply_sources`. The runtime rechecks privacy, sources, stop,
spend and delivery before inference, result persistence and release. Interrupted
inference remains a held pending operation and is not repeated on retry.

## Mission evolution

`SharedMissionAdapter` uses the existing proposal/confirmation validation,
capability-policy invariants, purpose vocabulary, source-authority checks and
mission document serialization. It neither replaces the state machine nor adds
external actions. Mission text and reasons use reviewed closed vocabulary;
operation, correlation, parent/proposed version and exact evidence identity are
bound durably. Counterevidence review must be explicitly acknowledged. A proposal
requires the separate `allow_mission_evidence=True` application grant, bound in
the protected immutable manifest. Conversation authorization alone grants no
mission use. A proposal uses a current shared revision and leaves the current mission unchanged.

A separate constructor-bound `SyntheticMissionConfirmationApproval` grants only
one exact proposal/evidence/outcome combination. Confirmation requires later
shared evidence and material private events arriving after the original proposal,
from principals outside the original material source set. Original evidence,
old assertions repackaged later, synthetic owner claims, forged verification,
stale parents, changed operational invariants and absent counterevidence checks
cannot establish independent authority. Rejection and `no_change` are valid
results. Trusted decision timestamps are retained on replay.

Mission document and evidence bindings commit atomically in EventStore. The
PrivacyLedger is still a separate store: its existing pending-first intent and
short read fence order transitions and erasure. There is no claim of a distributed
transaction. Model execution occurs outside storage transactions. Screening is a
short deterministic offline read fence; promotion runs in a separate transaction.
A crash between them leaves quarantine. A crash inside promotion rolls back;
a crash after commit is recovered by immutable operation identity.

## Erasure and recovery

Deleting any material private source invalidates every revision of the affected
shared item, removes its payloads and replay manifests, and removes all dependent
reply payloads, including replies to other participants. Their normal reads return
unavailable without identifying the originating participant. Protected identities
and graph edges remain content-free. Re-establishment requires a fresh item
identity and new complete source evidence; invalidated identities cannot be
reused to bypass the deletion ledger. Mission payloads depending on any affected
revision are removed, retaining the version identity with `reassessment` status;
further mission use fails closed until a separately reviewed reassessment path.

Graph validation covers exact schema objects, foreign keys, counted edge
completeness, complete parent dependency inheritance, audience manifests, screen
eligibility, reply bindings and mission-document bindings. Malformed, missing,
unsupported or forged graph data blocks erasure/reconciliation and quarantine
eligibility. Existing legacy shared rows in private-state tables remain rejected;
the v6 purpose-specific schema adds validated sharing without weakening that rule.

Migrations v1–v6 are atomic and preserve earlier rows; the reply table adds a zero
shared-dependency count for historical operations. Backup validation accepts exact
historical v4/v5 and exact v6 schemas. Older snapshots are restored only into
read-only quarantine after replaying the current external ledger. Eligibility
remains distinct from production activation.

## Acceptance evidence and remaining work

The table names executable tests; final gate and remote review evidence belong to
the PR. A passed deterministic scenario does not establish semantic anonymity or
full M5 acceptance.

| Requested acceptance scenario | Status / evidence |
|---|---|
| A's safe abstraction reaches B; raw A text and IDs excluded | Completed: `test_cross_participant_runtime_influence_and_erasure` |
| B's independent viewpoint revises shared belief without verification; history retained | Completed: `test_exact_approval_replay_cas_and_inherited_sources` |
| Cross-participant reply invalidation on A deletion | Completed: runtime influence/erasure and v6 backup tests |
| Restart, identical retry, conflicting identity, revision CAS | Completed: exact approval replay and competing commit tests |
| Missing/outdated screen evidence, uncertain quarantine | Completed: closed constructor and versioned screen tests |
| Permissive H screen cannot supply G authority | Completed: permissive-screen and no-approval erasure tests |
| Injection, private paraphrase, quotation and canary denial | Completed: closed-vocabulary promotion tests plus preserved cognition screening suite |
| Whole-record UTF-8 and complete dependency bounds | Completed: context limits and protected reply source tests |
| Two-phase mission activation and restart | Completed: `test_mission_two_phase_restart_and_deletion_reassessment` |
| Independent rejection and no_change | Completed: mission rejection and missing-check tests |
| Self-confirmation, stale evidence/parent, owner/verification claims, changed invariants | Completed: mission authority, current revision and invariant tests |
| Mission invalidation after deletion | Completed: mission restart/deletion test |
| Source deletion during screening, after intent, after capture | Completed: deletion during screen, after durable intent and capture/commit tests |
| Multiple material revision sources and complete inheritance | Completed: exact approval replay and dependency bound tests |
| Partial transition and uncertain inference interruption | Completed: transition crash matrix and held reply recovery tests |
| Concurrent promotion/deletion and competing SQLite commits | Completed: shared recovery tests using actual stores and threads |
| Old backup followed by deletion; genuine v4/v5 and exact v6 | Completed: shared recovery and preserved historical recovery suites |
| Missing graph edges and forged audience metadata | Completed: forged graph and missing edge tests |
| Stop/spend guards and no external effects | Completed: shared guard tests; only offline fake transport constructed |
| INT-01 through INT-05A and INT-08 preservation | Complete local suite required by `scripts/ci.sh`; recorded in PR validation |
| General prose abstraction and real-model privacy evaluation | Blocked for production: closed synthetic vocabulary only; calibrated semantic screening and behavioral evaluation required |
| Real independent corroboration and general mission reassessment | Blocked for production: this adapter proves synthetic authorization and invalidation, not external truth or recovery-policy adequacy |
| Enrollment, processor disclosures, structured real authorization and retention policy | Blocked for production: existing pre-enrollment decisions remain unresolved |
| Production database/restore activation, shared promotion, public/research dispatch, deployment | Disabled; requires separate authorized work and review |

Before real shared-intelligence use, establish a reviewed production approval
service and privacy notice, general-text screening with calibrated adversarial
corpora, consent/revocation and retention controls, independent evidence quality
criteria, operational reassessment after deletion, and production restore
activation. Full M5 additionally needs real-model behavioral trials of privacy,
source inference, intellectual disagreement, confidence calibration, substantive
Russian dialogue and mission continuity. Coverage and this synthetic graph do not
replace those evaluations.
