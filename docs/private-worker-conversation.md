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
the same operation identity. The IPC audit keeps only a binding hash and a
sanitized `accepted`, `no_change`, `rejected`, `unavailable`, or `uncertain`
outcome. Receipt text is erased after resolution and by source deletion,
including quarantine of an older backup. The reply has an independent IPC
reservation and provider budget; extraction failure can leave that reply
eligible. Current session, source, privacy, application Stop and spend state
are checked again before receipt and generic memory commit. No privacy lock or
SQLite transaction spans provider I/O.

The synthetic HTTP plus separate AF_UNIX fake-SDK tests prove the versioned
codec, provider builder, parser, deterministic validation, SQLite durability,
restart continuity, privacy graph, Stop/spend/session gates and operation
accounting. They do not establish live-model memory quality. B03B still needs
revision and consolidation of accumulated memories, typed interest/belief/goal
evolution, shared promotion, research and narrative memory, autonomous
behavior and live semantic evaluation. `complete_mvp=BLOCKED` remains unchanged.
