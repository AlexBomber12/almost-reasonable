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

INT-04 adds metadata-only v5 resolution/release and input-sequence tables without changing these
v4 draft columns. See [the runtime lifecycle](private-conversation-runtime.md).
Exact v4 backups and transactional upgrade remain supported; held drafts alone
remain non-deliverable.


## SQLite security handles and concurrent processes (B09E)

The supported ordinary deployment is Linux/glibc (glibc 2.28 or newer) with `/proc/self/fd`, `O_PATH`,
atomic [`renameat2(RENAME_NOREPLACE)`](https://man7.org/linux/man-pages/man2/rename.2.html), working local filesystem advisory locks
and private trusted state directories.
App, owner, inference and research processes can use their shared stores
concurrently; correctness does not require starting them one by one. SQLite's
existing WAL mode, busy limits and transaction boundaries remain unchanged.
Readiness must still be checked before submitting work. Database replacement,
unlinking live SQLite files, network filesystems with unreliable locks and
multiple SQLite libraries in one process are not supported concurrency modes.

[SQLite documents](https://sqlite.org/howtocorrupt.html#posix_advisory_locks_canceled_by_a_separate_thread_doing_close_)
that closing an unrelated ordinary descriptor can cancel all POSIX record locks
on that inode in the process. Opening/closing `-shm` during EventStore security
checks released an active WAL writer's lock; securing the database itself also
broke rollback-mode writer exclusion. No longer timeout or operator sequencing
can correct this.

`app/core/sqlite_files.py` supplies Linux metadata-only `O_PATH | O_NOFOLLOW`
handles. Linux excludes these handles from the lock cleanup in
[`filp_close`](https://github.com/torvalds/linux/blob/v6.12/fs/open.c).
Checks still inspect the pinned inode's file type and owner. Chmod resolves
`/proc/self/fd/<handle>` to that inode without opening an ordinary descriptor;
it cannot chmod a replacement reached through the original filename. New
0600 files are closed under a unique staging name before atomic no-overwrite
rename publication at the database name. Another constructor can admit the
published database before the publisher resumes; no transient second hard link
is exposed. Losing a creation race leaves the winning file intact. Staging names
are removed on success and failure.

The trace covers EventStore construction, `_open`, sidecar checks, diagnostics
and repeat initialization; the related AuthStore setup, PrivacyLedger sidecar
validation/configuration and retained database pin, and AuthorityJournal's
retained admission pin use the same helper. PrivacyLedger keeps its existing
constructor serialization on the pinned parent directory's flock rather than a
raw database handle. Regular transactions still use SQLite. Owner reply and
worker telemetry diagnostics use metadata queries and SQLite-managed read-only
connections, including live WAL reads; they do not acquire raw data descriptors
for the observed EventStore. Standalone backup/quarantine code has separate
snapshot/descriptor lifetimes and is not a concurrent in-process diagnostic API;
run the documented owner CLI in its own process. This task does not change its
snapshot, quarantine or writer-fencing protocol.

`tests/test_sqlite_lock_security.py` synchronizes a fresh interpreter over pipes
before each immediate writer attempt. The writer must remain blocked before and
after file/sidecar security checks, another normal connection, constructor
admission and diagnostics, then succeed after commit or rollback. Both WAL and
rollback journals are exercised. Additional cases cover privacy checks and pin
finalization, authentication setup and research admission pin finalization.
Four explicitly synchronized processes initialize an empty or v2 store,
migrate, write/read concurrently, close and reopen. Exact event/operation IDs,
reservations, token totals, integrity and foreign keys must survive.
A deterministic last-connection close also removes sidecars after pinning,
without rejecting that normal SQLite lifecycle. `tests/test_sqlite_files.py`
retains unsafe-file failures and verifies pinned
chmod during filename substitution. Existing permission, unsafe-directory,
schema/migration, Stop and private/research tests remain in the full CI gate.

The offline Compose fixture in `tests/test_b05d_research_deployment.py` exercises
simultaneous admission/restart with synthetic durable state and fake providers.
No production deployment, real participant data, credentials, paid provider call
or Meta request is needed. Passing this regression establishes lock preservation
and the tested deployment behavior. It does not establish that the previously
reported SIGBUS crashes had this exact cause, nor certify untested hosts.

The focused lock/security suite passed with host SQLite 3.45.1 and with SQLite
3.53.1 built separately from the official `sqlite-autoconf-3530100.tar.gz`
(SHA-256 `83e6b2020a034e9a7ad4a72feea59e1ad52f162e09cbd26735a3ffb98359fc4f`).
The unchanged-main sidecar regression also fails with that 3.53.1 library:
the independent writer acquires the transaction before its owner finishes.
This test-only library is outside the repository and does not change runtime
SQLite or dependency pins. The shipped pinned Docker base uses SQLite 3.40.1.

One controlled run of the same simultaneous Docker rehearsal against unchanged
main also passed. Neither that baseline run nor the corrected rehearsal
reproduced SIGBUS; the deterministic independent-writer failure is the evidence
for the lock-loss fix, rather than an inferred crash-causality claim.
