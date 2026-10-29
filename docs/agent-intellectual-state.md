# Durable agent intellectual state (PR2)

Authorized private reply and public decision calls can now return one optional
agent-owned state proposal in the same provider response. There is no additional
provider call, reflection loop, scheduler, grant, or automatic action. A null
`intellectual` proposal records `no_change`. Existing action, extraction, typed
participant-memory, narrative, research, and mission contracts remain usable.
The extension is identified by `agent-intellectual-state-v1` in requests and
provider results; older peers can safely refuse an unfamiliar request without
obtaining replacement inference authority.

The state categories reuse the existing typed interest, belief/hypothesis, and
goal/intention fields. An unresolved question adds a bounded question and an
open/resolved/abandoned status. The subject is always the agent; audience is
separately fixed by the application to a public account or one authenticated
private participant. No interests, opinions, goals, biography, or mission are
seeded. Belief stance, confidence, alternatives, supporting and conflicting
source roles remain unverified model judgments. A goal records an intention,
including its status, and never grants operational permission. A resolved
question never grants research authority.

## Sources and privacy

The existing participant candidate tables require participant-assertion evidence.
They are not used to impersonate a public agent principal or reinterpret an agent
response as participant input. The new namespace reuses their typed field/schema
validation, mutable-state screen, and existing storage and authorization fences.

The public adapter uses PR88's locally verified inbound events, confirmed owned
publications/replies, and recorded no-action decisions. It also distinguishes
application-rejected decisions and observed unknown/failed publication outcomes.
Unknown or failed publication is never a completed publication. Confirmation
proves provider confirmation under the existing transport contract, not factual
truth or present availability on the platform.

The private adapter distinguishes participant assertions from agent expressions.
It reads an earlier agent response only when the existing reply resolution is
exactly `completed` with its authenticated `release:<operation>` identity and
all its source, shared, narrative, and research checks remain current. A persisted
or withheld draft is not released experience. Server release never means that a
human received or read it. Responses depending on narrative episodes are excluded
from factual source handles. Fiction visible in a conversation retains its
existing labels, permission checks, and protected dependencies; it cannot become
verified operational history through a state revision.

Only opaque, operation-local state and source handles enter the new model
projection. Principals, accounts, event/reply identities, protected source edges,
permissions and revision identities stay in application storage. Every material
context dependency is retained, including dependencies behind previously released
output and selected state. Selecting a convenient supporting handle cannot remove
other dependencies. A/B private scopes and public account scopes remain separate.
There is no private-to-shared transition in this PR. Public state is consumed by
public decisions; private state is consumed in its corresponding private scope.
The broader shared-personality integration remains PR3 work.

## Persistence, binding, and recovery

EventStore v14 adds a typed agent-owned revision namespace and immutable operation
snapshots without rewriting participant histories or candidate revisions. The
public journal uses the same schema and revision implementation in its existing
account boundary. These databases remain independent. This is a source adapter
extension to typed state, rather than another service or background memory pass.

Each operation reserves its exact selected state revisions, model projection,
full protected dependency union, context binding, and control generation before
provider contact. A private operation binds its final SDK request after any
same-operation narrative projection and before worker contact, under its existing
one-use IPC admission. State and prior output enter SDK input as untrusted data.
Neither can modify tools, routing, ownership, approvals, spending, or system policy.
Public intellectual projection bindings are separate from PR88 experience and
legacy research/mission context digests. Existing public research shown in the
same SDK input is pinned as a contextual dependency on its actual cached record.
It is not a new supporting-source handle, research permission, or evidence-driven
research feature. Catalog/evidence changes after admission hold the new operation.
Completed historical research keeps its source-based validation; pending effects
also recheck current intellectual revisions.

Accepted revisions retain previous/new fields, expected/resulting revision,
reason, review condition, source classifications and roles, decision time,
initiating operation, actual configured worker model, prompt/protocol and Core
Self versions, and exact context/projection bindings. Compare-and-swap checks
reject competing changes. An operation's own accepted revision is distinguished
from another operation's revision: it retains its original inference projection
and still needs independent authorization for any pending action. New snapshots
use a fixed-size fingerprint of eligible heads, with bounded selected-revision
records; old list snapshots retain their original validation. Accumulating state
items cannot overflow the heads representation. Replaying an already authenticated
server-released reply rechecks sources and authorization without requiring its
historical inference snapshot to match today's revisions. Pending first releases
still require current revision checks.

The local receipt and revision write commit together. A crash before that commit
leaves consumed inference authority and no revision; a crash afterward preserves
one revision. Reply/outbox completion remains governed by its existing recovery
boundary. Pending or uncertain work is not given a newer snapshot or another
paid call. Duplicate delivery cannot create a second revision. Public publication
approval and UNKNOWN reconciliation retain their original identities and controls.

Private erasure deletes dependent revision/receipt payloads and context hashes,
while retaining protected identity and dependency metadata for replay. Shared
source revocation invalidates dependent private state through the inherited
context edges. Quarantine validation checks scope, source bindings, selected
revisions, projection and receipt bindings before an old backup becomes eligible.
Public source withdrawal or changed controls prevent future use and dependent
effects. Rephrasing or citing an earlier agent response does not bypass an
invalid ancestor.

## Bounds and evidence

A projection contains at most six whole state records and six source records,
with a 2 KiB typed field/source record bound and a 16 KiB aggregate UTF-8 bound.
Reasons and review conditions are separately bounded. Selection filters source
validity and audience before deterministic ordering and truncation; complete
private and public request limits still apply. Private selection also budgets
against the actual serialized conversation frame. Required context dependencies
are reserved before optional state/experience selection. If that union has no
room for the extension, the ordinary conversation retains its full existing
source set and acquires no intellectual write authority. Taint is never dropped.
Storage applies separate receipt, metadata, revision and snapshot byte limits.

`tests/test_agent_intellectual_state.py` exercises real SQLite persistence,
authenticated private HTTP, AF_UNIX worker exchange and actual fake-SDK input.
It covers public/private continuity after restart, server release versus
withholding, all four state categories, confidence reduction and counterevidence,
no-change, A/B and account separation, forged handles/classifications, concurrent
changes, control/source races, erasure, quarantine tampering, one-use recovery,
Russian/JSON bounds, and migration rollback. Existing narrative, typed/generic
memory, PR88 continuity, finite grants/windows, rootless startup, Stop/spending,
approval and UNKNOWN suites remain required by `scripts/ci.sh`.

These are deterministic fake-provider guarantees. Real-model intellectual
judgment, groundedness, natural continuity and privacy semantics have not been
behaviorally evaluated here. Nothing in this PR activates production, permits
live provider calls, changes a selected model, authorizes autonomous publication,
or establishes complete persistent-personality/MVP acceptance.
