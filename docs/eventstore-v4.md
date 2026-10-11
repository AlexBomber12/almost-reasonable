# EventStore v4 private draft persistence

INT-02 adds three tables to the existing event/state/provider database. Startup
migrates supported v1/v2/v3 schemas to v4 under one `BEGIN IMMEDIATE` transaction.
The old envelopes, removable payloads, protected state provenance, revisions,
authenticated source references and provider records are retained unchanged.
Exact schema objects are checked before migration and at each storage boundary;
startup also rejects orphaned foreign keys. Unknown objects, missing indexes, mismatched versions and
orphaned references fail closed. WAL and bounded busy handling remain enabled.

`AgentOutputStore` shares the EventStore connection and security configuration.
Its APIs require upstream-authenticated `TrustedPrincipalContext`; constructing
a dataclass is not authentication. Only application code may construct
`ReplyOperation`, including its opaque conversation route, correlation, model,
prompt and approved Core Self version. The initiating event must be an active,
valid private input for that principal and have the exact correlation. Storage
cannot authenticate version identifiers or derive a conversation route from text;
the caller must bind them to the actual authorized request.

Call `reserve_reply` before downstream work. The global operation ID binds the
principal, initiating event, conversation, correlation, version evidence and a
sorted unique complete set of exposed private input events. Other principals
cannot read the reservation or reuse its ID. A pending reservation survives
restart but does not authorize repeating inference or any uncertain side effect.
The caller must consult provider attempt reservations before further paid work.
There is no automatic recovery worker in this change.

Call `persist_reply` only with a bound `ActionPolicy` private draft grant and
current capability state. It revalidates the exact proposal and trusted route,
then commits the removable payload and lifecycle transition atomically. An
identical retry returns the committed result; changed identity or content fails
closed. No content hash remains after payload removal. A failure before commit
leaves the reservation pending; interruption after commit preserves the result.
A completed internal decision, persisted result and delivery state are separate
columns. Every draft remains `held`; this API has no transmission or delivery
transition. Persisted text remains unverified model inference. The scoped result
is an application storage view containing protected metadata, not an HTTP or
model-facing serialization contract.

`agent_reply_sources` records protected event-to-output edges and their retention
state. Reads and repeated writes recheck all source payloads and protected taints
inside the same SQLite snapshot/transaction. Missing, removed, deleted, wrong-
principal or invalidated evidence hides the output. Missing/deleted output
payloads and invalidated operations likewise cannot be read or resurrected by a
retry. Identity metadata is retained for INT-03's tombstone reconciliation.
Existing cognition edges remain in `protected_state_provenance`; they are not
copied into a competing general provenance system. This API accepts event-only
context. Memory/revision-derived output is not supported until its complete
revision-chain dependencies can be recorded and validated by integration.

INT-03 now implements the reviewed v4 event erasure and quarantine adapters in
`app/privacy/application.py`: it follows complete declared state revision chains
and reply edges, removes affected payloads and invalidates retained provenance.
The trusted startup gate reconciles the external ledger before scoped reads.
See [the table inventory and coverage limits](privacy-eventstore-integration.md).
Raw storage APIs remain internal capabilities; participant routes are unchanged.
INT-04 must bind conversation routing at ingress and
implement delivery admission, durable dispatch evidence and uncertain-outcome
reconciliation. Reservations or held drafts never authorize transmission.

Privacy backup validation recognizes exact schemas v1–v4, including the new
objects; its copy, restore, WAL cleanup and sealing algorithms are unchanged.
A v4 backup is structurally supported. Production v4 restore activation remains
blocked until the ledger-generation/writer/activation fence and recoverable
database-switch protocol are implemented and independently validated. A readiness
marker or a fake adapter's eligible result is not activation authority. No
activation API, real participant enrollment, provider call, deployment change or
external effect is introduced here.
