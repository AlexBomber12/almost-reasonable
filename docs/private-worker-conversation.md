# INT-19 private conversation worker connection

The ordinary `python -m app.private_web.cli serve` path still constructs disabled
`ConversationService`. An operator can explicitly select the private worker path:

```text
python -m app.private_web.cli serve \
  --auth-db /private/auth.db --event-db /private/events.db \
  --privacy-ledger /private/ledger.db --owner-directory /private/owner \
  --provider-db /private/provider.db \
  --worker-socket /private/worker/worker.sock --worker-uid 1001 \
  --allowed-host chat.example --allowed-origin https://chat.example
```

All five stores must already exist. The provider database must be distinct from
the conversation EventStore and match the database configured for the separate
worker process; the worker does not open private conversation storage. The owner
store is read with its current stop
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

The new `openai-worker-context-v2` request envelope carries exactly one typed
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

The authenticated HTTP path uses the same privacy fence, original-input identity,
reply reservation, provider-spend state, `ActionPolicy`, zero-regeneration Russian
screen, durable result, protected dependency checks, release and history as the
offline Stage 0 runtime. The Stage 0 constructor still requires the exact fake
transport. Worker reply metadata says `worker-owned` instead of claiming that the
application selected the provider model.

A durable `ipc:<reply operation>` reservation is committed before the network
exchange. If the process exits or the response is lost after that marker, the
reply remains `uncertain` after restart and is never automatically inferred
again. A connection failure before private transmission is terminal
`unavailable`. Distinct terminal `stopped`, `spend_suspended`, `rejected`,
`failed`, `unsupported`, and `no_action` outcomes are stored in EventStore v8;
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
