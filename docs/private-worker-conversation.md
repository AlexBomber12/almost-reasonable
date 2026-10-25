# INT-19 private conversation worker connection

The ordinary `python -m app.private_web.cli serve` path still constructs disabled
`ConversationService`. An operator can explicitly select the private worker path:

```text
python -m app.private_web.cli serve \
  --auth-db /private/auth.db --event-db /private/events.db \
  --privacy-ledger /private/ledger.db --owner-directory /private/owner \
  --ipc-state-db /private/app-ipc-state.db \
  --worker-socket /private/worker/worker.sock --worker-uid 1001 \
  --allowed-host chat.example --allowed-origin https://chat.example
```

All five application stores must already exist. The application IPC state database
must be distinct from the conversation EventStore and owned by the application UID.
It holds durable IPC reservations and the application spend suspension control.
The worker uses its own provider database, owned by the worker UID, and never opens
either application database. The worker checks its own spend state before provider
use; a worker-reported suspension is recorded as a terminal conversation outcome.
Both spend controls can independently stop inference. The owner store is read with its current stop
state; startup never clears it. The privacy gate must reconcile the matching
EventStore and ledger before serving. This mode installs `ParticipantPrivacyControls`,
`SharedIntelligence` for already approved records, and `WorkerConversationRuntime`
with an exact `OpenAIWorkerClient`. It does not provision accounts, acknowledge
notices, create consent or shared approvals, change provider suspension, or start
the worker. The worker UID is a trusted operator value checked with Linux
`SO_PEERCRED` before any private request bytes are sent. The worker independently
checks the application peer UID and owns its own model, transport, credentials,
retry, timeout, tool, storage and spend configuration. The one-shot Stage 1 smoke
activation remains a separate fixed authorization and does not authorize chat.

The legacy `openai-worker-context-v2` request envelope carries exactly one typed
`MemoryModelRequest` (`model-contract-private-memory-v2`) or `SharedModelRequest`
(`model-contract-shared-intelligence-v3`). The original `openai-worker-v1` and
`model-contract-v1` paths remain intact. The v2 codec rebuilds the immutable Core
Self, bounded participant conversation, memory records, and shared records from
exact field allowlists. Memory and shared values are marked as untrusted data,
with `can_instruct=false`. Protected source event IDs, revision operation IDs,
principal and authenticated-source identifiers, consent, owner controls, account
state, and credentials stay out of the model projection. The application's
reply operation retains complete private, generic-memory, and shared dependencies
for every selected record. Model output cannot add research or publication
authority; the worker runtime does not install a research bridge or memory
extractor.

B02 adds `openai-worker-transcript-v3` with
`model-contract-conversation-transcript-v4`. Its `conversation` allows
participant and agent roles, each explicitly marked
`untrusted_conversational_data`. The v1 and v2 wire parsers retain their
original participant-only rules. The application selects released replies from
the durable input sequence and release resolution, authorizes all dependencies
before selection, and keeps the current input mandatory. The worker codec,
provider builder and pilot request gate accept the bounded v4 projection;
protected event, operation, principal, consent and authentication identifiers
remain outside it. This provides continuity with earlier released answers; it
does not create structured long-term memory, complete B03 or M5, or reactivate
the closed pilot.

The authenticated HTTP path uses the same privacy fence, original-input identity,
reply reservation, application spend state, `ActionPolicy`, zero-regeneration Russian
screen, durable result, protected dependency checks, release and history as the
offline Stage 0 runtime. The Stage 0 constructor still requires the exact fake
transport. Worker reply metadata says `worker-owned` instead of claiming that the
application selected the provider model.

A durable `ipc:<reply operation>` reservation is committed before the network
exchange. If the process exits or the response is lost after that marker, the
reply remains `uncertain` after restart and is never automatically inferred
again. A connection failure before private transmission is terminal
`unavailable`. Distinct terminal `stopped`, `spend_suspended`, `rejected`,
`failed`, `unsupported`, and `no_action` outcomes are stored in EventStore v9;
operational errors use fixed Russian messages without a second model call.
Current session, stop, spend, privacy and protected sources are rechecked at
inference admission, result persistence and release. A revoked session or erased
source leaves no deliverable draft. Completed means the server released a reply
for response, not confirmed browser receipt.

The offline process tests use synthetic A/B accounts, actual authenticated WSGI,
a separate AF_UNIX worker process, deterministic fake responses, controlled
in-flight stop/spend/session changes, and real worker exits. They do not make
paid provider calls or authorize participant enrollment. Before a separately
authorized synthetic live-model pilot, the operator still needs a non-smoke
worker authorization and configuration review, threat and privacy review,
current-head CI and Codex Review, human calibration, and explicit pilot approval.
Production deployment and release readiness remain BLOCKED.

## B03A: separately authorized private memory extraction

The ordinary worker and web startup still perform reply-only conversation. To
enable extraction for a separately authorized pilot, the application must add
`--enable-memory-extraction` to the existing `serve --worker-socket ...` command.
The credential-owning pilot worker must separately receive
`--memory-extraction-permission /protected/control/memory-extraction.json`.
The protected worker-owned file must be mode 0400 in a protected 0700 directory:

```json
{"version":"private-memory-extraction-permission-v1","pilot_id":"synthetic-pilot-id","max_operations":1}
```

The `pilot_id` must match the current activation, and `max_operations` must be
between one and that activation's total allowance. The same worker-owned
allowance counts reply and extraction operations separately; its extraction
subset has its own cap. The existing closed INT22 activation grants no
extraction permission, and this flag does not create or renew an activation,
reset Stop, clear spend suspension, deploy a service, or make a provider call.

`openai-worker-memory-extraction-v1` carries `extract_memory` and the closed
`model-contract-private-extraction-v1` projection. Its only variable model input
is the initiating participant's text, capped at 8 KiB. No transcript, previous
reply, memory, shared record, research result, principal, account, consent,
source, operation or storage identifier enters the provider input. The fixed
instructions ask for one `new_memory` with a generic kind, statement and
confidence, or explicit `no_change`. The output schema and application parser
reject additional fields, forged authority or provenance, unsupported kinds,
invalid confidence and oversized text. The application binds its own operation,
item, principal, source, timestamps and `unverified` status before using the
existing cognition validator and GenericMemoryStore. Revision and parent links
are unavailable in this slice.

The application commits an extraction intent in its separate IPC database and
reserves an `ipc:memory:` attempt before sending. The worker independently
checks Stop, spend, protected permission, total and extraction allowances, and
its provider attempt ledger. A lost response stays `uncertain`; a reserved
operation is never automatically retransmitted. A received proposal is stored
temporarily in the source EventStore and can be reconciled after a crash using
the same operation identity. The IPC audit keeps only an identity hash of the
principal and source event, without message content, and a
sanitized `accepted`, `no_change`, `rejected`, `unavailable`, or `uncertain`
outcome. Receipt text is erased after resolution, on replay if finalization
stopped between the two databases, and by source deletion, including quarantine
of an older backup. The reply has an independent IPC
reservation and provider budget; extraction failure can leave that reply
eligible. Current session, source, privacy, application Stop and spend state
are checked again before receipt and generic memory commit. No privacy lock or
SQLite transaction spans provider I/O.

The synthetic HTTP plus separate AF_UNIX fake-SDK tests prove the versioned
codec, provider builder, parser, deterministic validation, SQLite durability,
restart continuity, privacy graph, Stop/spend/session gates and operation
accounting. They do not establish live-model memory quality.

## B03B: bounded private generic-memory evolution

The application can explicitly select `--enable-memory-evolution` for the
worker conversation path. When selected, it uses one evolution operation for
the input and does not run B03A extraction for that input. Both options remain
off by default. The worker needs its own protected 0400 file in a protected
0700 directory, supplied through `--memory-evolution-permission`:

```json
{"version":"private-memory-evolution-permission-v1","pilot_id":"synthetic-pilot-id","max_operations":1}
```

The extraction permission cannot authorize evolution. The pilot ID and cap
must match the current activation and its total allowance. Evolution consumes
the same bounded pilot operation and provider attempt ledgers as extraction
and replies; evolution and extraction share the conservative memory-operation
subset cap. The worker rechecks Stop, protected permission, activation,
provider-store identity and spend suspension before the provider and before
returning an actionable result. The application separately checks its current
session, privacy gate, source, Stop and spend state before sending and before
committing. Neither the privacy fence nor a SQLite transaction spans provider
I/O.

`openai-worker-memory-evolution-v1` contains one
`model-contract-private-evolution-v1` request. It carries only the initiating
participant input (at most 8 KiB) and up to eight whole current generic
memories from that participant, with a 16 KiB serialized projection cap.
Selection ranks authorized generic heads by overlap with the input and
recency, and excludes records whose complete provenance role-row union would
exceed the 100-row generic graph bound. A full generic operation ledger blocks
evolution before provider use. Every shown record has an operation-local
opaque handle, kind, statement, confidence, and explicit untrusted-data
markers. Omitted records have no handle. No principal, event, item, revision,
storage, permission or provenance identifier enters the provider input.
Fixed instructions treat both participant text and memory text as data.

The closed model output is one `new_memory`, `revise_memory` (one shown handle),
`consolidate_memories` (at least two distinct shown handles), or explicit
`no_change`. Kind, statement and confidence remain bounded and unverified.
The parser rejects extra fields, duplicates, unknown decisions, invalid
handles, oversized text and invalid or huge numeric confidence. The
application resolves handles only against the immutable snapshot stored
before provider I/O in a source-bound EventStore receipt. It owns source,
operation, item, expected revision, timestamps, review state, verification,
parents and contextual lineage. The GenericMemoryStore compares the original
visible head set and exact selected heads in its write transaction. A newer
revision or changed snapshot is rejected; it is never silently retargeted.
Consolidation creates a new derived item with exact parent edges and leaves
its sources and their revision histories intact. All shown records, including
unselected context, contribute complete protected source dependencies. A
repeated or consolidated participant assertion is still unverified and is
not independent factual corroboration.

The IPC intent is content-free. Its source-bound application receipt holds
the selected handle bindings, visible head references and source IDs without
plaintext memory or a content-derived digest. It is erased on finalization,
source deletion and contextual-source deletion. A lost response stays
uncertain; replay never calls the provider again. A received proposal can be
reconciled after a crash before or after the memory store commit using the
original operation identity, creating no second revision. A deleted receipt
becomes unavailable. Quarantined restore reapplies ledger deletion to old
backups, including contextual dependencies.

Synthetic fake-SDK tests inspect the actual provider projection and exercise
the authenticated HTTP, AF_UNIX worker, parser, codec and SQLite path. They
do not establish live-model semantic quality. Typed interest/belief/goal
evolution, shared promotion, research and narrative memory, autonomous
behavior, deployment and live semantic evaluation remain separate work.
`complete_mvp=BLOCKED` remains unchanged.

## B03C: participant-private typed memory evolution

The separate default-off interest, belief/hypothesis and goal/intention worker
path is documented in [private typed worker memory](private-typed-worker-memory.md).
It uses EventStore v10 contextual typed provenance, the existing SelfStateStore,
and its own application flag and protected worker permission. `complete_mvp=BLOCKED`
remains unchanged.

## B03D: combined participant-private memory

The application may enable typed memory with generic extraction or generic
evolution in one ordinary worker conversation. The worker still requires each
operation's separate protected permission. Generic evolution takes precedence
over extraction when both generic flags are set; no input incurs both generic
operations. For each admitted turn the finite order is generic evolution (or
extraction), typed evolution, then the reply. The reply uses the context and
dependencies reserved before these writes. Newly written memories become
eligible on later turns, including after restart. At most three provider
operations occur per turn with only combined memory enabled; extraction and
typed memory without generic evolution have the same bound. A separately
enabled shared proposal adds its existing fourth operation and permission.
Each operation has its own durable identity,
attempt, receipt, permission check and shared allowance charge. An uncertain
operation is never automatically retried.
The worker requests at most 512 output tokens per operation, hence at most
1,536 requested output tokens for the three memory-and-reply operations.
Generic and typed evolution
projections each fit 16 KiB; the reserved reply projection fits 32,000 UTF-8
bytes, for a maximum 64,768 serialized input bytes across a turn. Actual
provider token usage is recorded and constrained by the pilot allowance.

Generic and typed model operations see only their own family's selected heads
and the initiating input. Their receipts bind the complete exposed source set;
the reply projection can select both families and binds all selected sources
and generic operation dependencies. No-change is allowed for either family.
Statements, interpretations, interests, beliefs and goals remain unverified
data without authority to instruct, publish, use tools or confer consent.
Source deletion and old-backup reconciliation use the existing protected
provenance graph, receipts and dependent reply invalidation. No schema migration
is needed because the graph already represents both families independently.
The combined fake-provider HTTP/IPC regression checks both family writes,
revision, restart, bounded history, participant isolation, duplicate requests,
the serialized reply projection and source deletion. The offline robot journey
uses five dialogue events and five short filler turns to force persistent recall
beyond recent history. Its nine synthetic turns cover creation, revision,
fillers, post-restart recall and recall after source deletion, with 27 fake
provider operations and accounting checks.

The separate live deployment is retired. Successful B03D/B03E technical evidence
is historical; outstanding browser and broader product validation use the
ordinary application and existing domain. See the
[historical evidence note](b03e-combined-acceptance.md).
The earlier B03D run `0f31eadff07048ec8f51f7482efa9b9f` rejected a typed
proposal on its first turn; all three billed attempts and allowance charges
reconciled. The rejected provider text was not retained, so its exact invalid
field remains unknown. The subsequent typed provider contract and diagnostic
fixes remain: `private_typed_memory_proposal_v2` constrains static decision,
kind, field, handle, role and numeric combinations; local validation checks
shown handles, provenance, whitespace and UTF-8 bounds. Bounded diagnostic
codes retain billed usage without storing model text.

## B04B: reviewed shared proposal generation

An explicitly enabled private worker can suggest one bounded general abstraction
per admitted turn. The application binds its source snapshot and configured
audience, then submits it to the existing reviewed shared quarantine. See
[reviewed shared abstractions](reviewed-shared-abstractions.md) for the separate
worker permission, review, consent, erasure, and recovery controls. Semantic
privacy and live-model evaluation remain open; `complete_mvp=BLOCKED`.
