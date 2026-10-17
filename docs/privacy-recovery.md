# Privacy consent, deletion, and recovery foundations

Current synthetic integration and exact scenario status are documented in
[integration-contracts.md](integration-contracts.md) and the validated
[acceptance manifest](../acceptance/scenarios.json). Historical v4/v5 inventories
below describe those schema versions; current live synthetic storage is EventStore
v7, with private/shared/generic reply edges and mission erasure. Authenticated
participant web ingress exists in an explicit synthetic composition; production
activation and restore activation remain absent.

Status: foundations plus INT-13 authenticated synthetic participant controls and
EventStore v8 erasure and quarantine integration. This does **not** enroll real
participants, activate a private-to-public
path, switch a production database, or make Stage 2 privacy complete.

## Security boundary and guarantees

`app/privacy` accepts control operations only with a `TrustedPrivacyContext`
whose origin is `authenticated_control`. Constructing that value is not
authentication. The explicit Private Chat composition constructs it only after
server-side authentication and Host/Origin/CSRF validation; chat
text, model output, an asserted participant ID, and an owner claim cannot do so.

## INT-13 authenticated synthetic controls

`PrivateWebApplication` accepts privacy controls only when their AuthStore,
EventStore and PrivacyReadGate match its conversation runtime. Normal CLI
construction omits these controls and retains disabled inference. The explicit
synthetic composition presents the current versioned `NOTICE_RU` and gates chat,
history and model input until the authenticated principal acknowledges that
version. `/privacy`, deletion status, revocation, reconciliation and logout stay
available when the notice changes or private reads are fenced.

The source view shows the exact escaped participant-owned immutable input. The
participant can grant a single exact source/audience/purpose scope for 1, 24 or
168 hours, revoke its consent ID, and confirm source deletion. The current UI
offers only the explicit public audience; named recipients remain unavailable
until a specific recipient identity can be bound to the grant. An expired,
unrevoked scope can receive a new revision, while a revoked scope cannot be
reactivated with a new operation ID. Consent, source, and deletion-status lists
use bounded cursor pages so older grants remain revocable, older source messages
remain selectable after chat history reaches its limit, and pending or blocked
deletion records remain inspectable. All changes use
the existing session, exact Host/Origin and CSRF checks; operation IDs are
bounded and durable. Server-side timestamps, principal and authenticated actor
come from the current session and trusted clock. The ledger serializes the final
source check and session revalidation with grant or pending deletion intent.
The browser preserves the operation ID after an uncertain network result so a
retry can recover the original durable outcome.
Deletion then runs the v7 adapter outside the ledger fence. Status distinguishes
`pending`, `incomplete_blocked` and `applied`, and a separate authenticated
reconciliation action can replay unfinished intent. The current-use consent
authorization denies a source with pending or completed deletion. Web grants
cannot renew a revoked scope under another operation ID; direct service revision
support remains an internal, separately authorized capability.
An applied deletion refreshes the read gate before the response, including an
identical operation retry, so normal history and chat access resume immediately
when reconciliation succeeds.

No participant route dispatches identifiable material, authorizes owner or model
actions, publishes to Threads, or activates an old backup. The synthetic tests
exercise the exact-use service with a closed callback, WSGI A/B isolation,
interpreter exit after HTTP deletion intent, v7 dependency erasure, later model
input, and old-backup quarantine replay. Local erasure does not establish
deletion from external processors, recipients, prior publication or forensic
storage. Legal, retention, processor and enrollment review remain prerequisites
for real participant activation.

Consent is an immutable, typed grant for one principal, exact content reference,
audience, purpose (`identifiable_quotation` or `identifiable_disclosure`), grant
time, expiry, trusted actor, authentication event, revision, operation ID, and
correlation ID. Use is checked against the current ledger with a service-owned
UTC clock; callers cannot backdate it. The required dispatch callback runs while
the privacy ledger holds a write fence, so revocation cannot commit between the
check and the outbound effect. Its use time is sampled only after that fence is
held. Before invoking the callback, the ledger durably commits a payload-free
`dispatch_pending` attempt keyed by correlation ID. Success changes that attempt
to `completed`; a callback error changes it to `dispatch_uncertain`, while a
process interruption leaves `dispatch_pending`. Neither uncertain state can be
retried automatically, so a send followed by a lost acknowledgement cannot
cause a duplicate disclosure. A caught failure proven to occur before callback
entry is recorded as `pre_dispatch_failure` and may be retried with the same
operation identity; it does not falsely imply possible disclosure. Dispatch code
must not re-enter the privacy ledger.
Missing, expired, revoked, wrong-principal, wrong-content, wrong-audience, and
wrong-purpose grants fail closed. Revocation is a separate authenticated,
idempotent operation and takes effect at its recorded UTC timestamp. Neither
purpose grants administrative or infrastructure
privilege. Revocation prevents later use; it cannot recall an earlier
publication or external transmission.

Consent use requires a trusted control context for the same principal named by
the authorization request and writes a payload-free audit decision for every
allow or denial before returning. Valid grant/revocation state and its audit row,
and each persisted deletion transition and its audit row, commit in the same
privacy-ledger transaction; an audit write failure rolls back the control state.
Rejected-operation audits attribute the trusted context actor rather than
submitted grant fields. Rejected notice acknowledgements are likewise recorded
as payload-free audit decisions. Authentication is checked before untrusted
request fields; a malformed submitted correlation ID is replaced in rejection
audits by the trusted authentication-event reference. Rejection audit timestamps
always come from the service clock rather than rejected request fields. The
ledger persistence boundary independently validates every notice-acknowledgement
token and its UTC timestamp before inserting trusted acknowledgement state.
Revocation persistence likewise validates all raw fields and confirms consent
ownership inside the same write transaction before changing control state.

Ordinary derivatives must retain every source audience. The only proposed
cross-scope transition is `abstract_to_shared`. Its validator requires a valid
`private:<principal>` source audience and rejects explicit identifier,
private-canary, personal-information, quotation, recognizable
paraphrase, relationship, attribution, excessive-overlap, missing-provenance,
and failed/uncertain semantic-screen flags. An accepted result is only eligible
for later integrated policy evaluation: `activation_allowed` is always false in
this branch. It never publishes or authorizes identifiable disclosure. Protected
source references remain available to deletion/reassessment and must not be
rendered to another participant. The screen reduces risk but cannot prove
anonymity; with two participants, source inference remains possible. Safe
general intellectual abstraction does not require a separate consent per change.

## Protected privacy ledger

`PrivacyLedger` is a separate, schema-versioned SQLite database with owner-only
mode `0600`. First-time schema creation is one explicit SQLite transaction, and
idempotent control inserts serialize their identity check with `BEGIN IMMEDIATE`.
Before an existing ledger or sidecar is inspected or reused, its pinned inode
must be owned by root or the service account and must not be group- or
world-writable; unsafe files are rejected rather than repaired in place. A
long-lived ledger repeats sidecar validation before every SQLite connection, so
permission or ownership drift cannot be consumed before rejection.
Initialization rejects a nonempty database without privacy-ledger metadata, so an
application/EventStore file cannot silently become the independent ledger. An
existing file is inspected through an inode-matched connection before its mode
or journal configuration is changed, so rejection does not mutate an unrelated
database. The ledger retains that file descriptor, normalizes relative paths at
construction, and opens every later SQLite connection through the descriptor;
both the configured path and SQLite's resolved main file must still match the
original inode before an operation proceeds.
Its records contain control metadata, not message payloads, prompts,
provider responses, secrets, or an unkeyed hash of private content. It stores:

- exact consent grants and revocations;
- payload-free consent-use attempts and their dispatch state;
- deletion operation identity, source reference/category, times, disposition,
  reason code, authenticated actor, authentication event, and correlation;
- payload-free technical audit records;
- versioned enrollment-notice acknowledgements; and
- sanitized restore outcomes.

The privacy ledger and application database are independent SQLite files. Their
commits are **not atomic**. Deletion therefore commits `pending` to the privacy
ledger before calling the application erasure adapter. A crash at that boundary
leaves a durable pending entry. Startup/restore integration must reconcile every
`pending` or `incomplete_blocked` entry before participant reads. Adapter failure,
missing provenance, an incomplete invalidation set, or an unwritten tombstone is
`incomplete_blocked`, never `applied`. A reconciled transition records its actual
completion time, not the original request time. Once `applied`, an operation is
monotonic: a concurrent late failure cannot regress its ledger disposition.
The ledger rejects an initially `applied` record and its general update method
cannot create that terminal state. Only the verified-erasure transition accepts
a complete typed `ErasureReport` and records `applied:erasure_applied`.

Every committed deletion or revocation advances a monotonic privacy-control
generation. Restore reconciles one transactionally consistent snapshot, blocks
if that generation changes during its work, and writes the reconciled generation
into both its typed result and readiness marker. Successful eligibility insertion
rechecks that generation under the same privacy-ledger write transaction, so a
new control cannot race between the final check and the outcome record.
Immediately before an eventual database switch, an integration must atomically
fence the ledger generation and bind the verified quarantine path/inode,
readiness marker, and sanitized outcome to that exact switch. It must also hold
an exclusive database-writer fence, reject WAL/shared-memory sidecars, and
revalidate the sealed file's size, `0400` mode, and SHA-256 digest from the
readiness marker before switching. This package deliberately provides no
activation operation until that deployment-specific boundary exists. The
readiness file and typed eligibility result are not activation authority and
become stale if any bound value changes.

Deletion has five operator-visible dispositions: `pending`, `applied`,
`incomplete_blocked`, `invalid_authorization`, and `not_eligible`. Reports use
references, categories, and reason codes only; they do not display deleted
content. Invalid-authentication attempts are payload-free audited outcomes but
are not persisted as authoritative deletion operations, so they cannot reserve
an operation ID and suppress a later authenticated request.

## Application erasure and trusted read integration

`app/privacy/application.py` implements `EventStoreErasureAdapter`,
`EventStoreRestoreAdapter` and `PrivacyReadGate` against the exact reviewed v4
schema. See [INT-03 guarantees and table inventory](privacy-eventstore-integration.md)
for the supported cascade, deterministic evidence, unsupported categories and
activation blockers. No schema migration, participant WSGI or Owner Control route
change is introduced.

The adapter accepts only `PRIVATE_PAYLOAD` requests whose content reference is an
exact EventStore **event ID**, not a payload ID, conversation ID or text search.
Authenticated control ingress must call the pending-first `DeletionCoordinator`;
the adapter itself is an internal storage capability, not authentication.
The coordinator writes durable intent first and records success only after the
application transaction commits. Reconciliation remains idempotent after an
interruption between the independent application and ledger commits.

The trusted service must retain a closed `PrivacyReadGate` until `reconcile()`
succeeds and use its scoped event/state/history/evidence/reply methods for reads.
Even `APPLIED` records are replayed against the configured application database
at startup. New privacy controls invalidate the gate's generation; reads take a
short ledger write fence to serialize with durable deletion intent. Raw stores
remain internal capabilities and this synthetic gate is not wired into routes.
Production participant enrollment remains disabled pending that ingress wiring.

## WAL-consistent backup

`create_sqlite_backup` uses the SQLite online backup API against a read-only
source connection. It never copies only the main file, so committed WAL content
is included in a consistent SQLite snapshot. Before returning, it verifies the
matching expected `schema_metadata` and `PRAGMA user_version` values plus
`PRAGMA integrity_check`. A separate owner-only metadata file records only format
version, database schema version, UTC creation time, and backup filename; it
contains no participant content.
The destination WAL and shared-memory names are exclusively reserved during the
copy, and the completed database is normalized to rollback-journal mode before
those owned reservations are removed. Descriptor-backed validation then treats
the self-contained snapshot as immutable, so it does not create or consume
undeclared sidecars. Existing source WAL and shared-memory files are trust-checked
and pinned before the first source connection, then identity-checked throughout
the copy. Restore uses the same immutable view of the backup.

Source, destination, and metadata paths must be absolute regular paths. Leaf
symbolic links, symbolic links anywhere in a parent chain, unsafe parents,
existing inputs owned by an untrusted account or writable by other accounts,
existing outputs, schema mismatch, integrity failure, interruption, and SQLite
busy/locked faults fail closed. Input ownership and permissions are checked both
before opening and again on the pinned descriptor. The source inode remains
pinned while schema validation and the online copy run; SQLite opens the source
through that descriptor, and the resulting connection is checked against the
same inode.
Output parent directories are traversed with directory-relative no-follow opens,
and outputs are created through the pinned parent with `O_EXCL|O_NOFOLLOW` and
mode `0600`; metadata and readiness markers are written completely before
`fsync`, including retrying short writes. Pinned parent directories are fsynced
after their output entries are complete and before success is reported.
Failure removes only files whose filesystem identity proves they were created by
that backup attempt; a concurrently created or replaced path is preserved.
Existing production databases are never overwritten by default. This is a
purpose-specific SQLite workflow, not a general file-copy capability.

Deployment still must decide an encrypted backup location, encryption/key
lifecycle, backup schedule, retention/expiry, restore drills, and which operating
system account owns the directories.

## Quarantined restore

Restore always creates a new, non-existing quarantine database. Opening SQLite
successfully is insufficient. The workflow:

1. validates protected metadata, expected schema, and the backup database;
2. makes a SQLite-consistent quarantine copy;
3. loads the current external privacy ledger;
4. idempotently applies every retained deletion entry, including entries later
   than the snapshot, through `RestoreAdapter`;
5. applies every current consent revocation;
6. requires the adapter to rebuild or invalidate dependent state and indexes,
   release every writer, and checkpoints those writes into a self-contained
   rollback-journal database;
7. confirms that every applicable ledger deletion was applied to this quarantine
   without changing the live database disposition, acquires an exclusive SQLite
   writer fence, and makes a logical SQLite copy into a fresh inode that was never
   exposed to the adapter;
8. writes and validates an owner-only `.ready.json` eligibility marker; and
9. rechecks the reconciled generation under the privacy-ledger write lock while
   durably recording the payload-free eligible outcome.

Copying, adapter reconciliation, finalization, and immutable verification occur
under an unguessable staging name in the trusted destination directory; the
requested quarantine pathname remains absent during that interval. Only the
sealed, verified inode is linked to the requested path with no-replace semantics,
so a concurrent file at that path fails publication rather than contributing
unreconciled rows to the eligible snapshot. If readiness creation or validation
then fails, that published inode is still tracked as owned and is identity-safely
removed; a concurrent replacement is preserved.

The adapter-exposed quarantine descriptor remains pinned through reconciliation
and finalization, then is discarded after the exclusive-fenced copy. A separate
descriptor for the fresh sealed inode remains open through schema and integrity
revalidation and readiness publication. Restore adapters receive a
descriptor-backed path valid for their callback lifetime, rather than the
replaceable public pathname; retained adapter descriptors can refer only to the
discarded inode. The current private or public path must still name the owned
sealed inode at every later validation boundary and before eligibility is
recorded.
Backup metadata is likewise opened through a no-follow pinned descriptor and
read with a strict size bound, so validation and parsing cannot be redirected to
a replacement or unbounded special file.
Before readiness publication, the reconciled quarantine is changed to owner-read
only mode and its device, inode, size, mode, and SHA-256 digest are recorded in
the owner-read-only marker. The marker is a revalidation contract, not a durable
lock: activation must enforce the exclusive fence and comparisons described
above because a process with the service account's authority can still alter
owner-controlled files.

Any adapter exception, non-`ErasureReport` deletion response, incomplete cascade,
revocation failure, rebuild failure, unknown schema, malformed metadata, or
pending deletion blocks activation. The quarantine file is never exposed through
a participant retrieval API by this package. An operator/integration layer may
activate only a database with a current successful outcome and matching readiness
marker. There is still a non-atomic boundary between quarantine data, the privacy
ledger, and the marker: crashes before the marker remain fail-closed, and all
operations are idempotent for reconciliation.

Applying an erasure to quarantine does not mark the authoritative live deletion
record `applied`: the live database may still contain the payload if later
restore checks fail or the quarantine is never activated. Activation integration
must commit the database switch and live-ledger disposition coherently, with a
recoverable intent/reconciliation record; that interface is not yet implemented.

## Russian pre-enrollment notice

The versioned `NOTICE_RU` template explains before first use that:

- private conversation content may be processed by OpenAI;
- Cloudflare processes tunnel traffic;
- Meta processes published Threads material and replies;
- the infrastructure owner/operator may technically access local storage;
- private ideas may influence shared abstractions;
- with two participants, another person may infer the likely source;
- identifiable quotation/disclosure needs exact structured consent;
- eligible local data can be deleted and consent can be revoked; and
- local deletion cannot guarantee deletion by OpenAI, Cloudflare, Meta,
  recipients, caches, or other external systems.

The template deliberately invents no processor promise or final retention
period. Before real enrollment, operator/legal review must confirm jurisdiction
and legal basis, current processor terms and regions, local and provider
retention, deletion SLA and legal holds, export/incident policy, encryption and
key lifecycle, backup expiry, the final Russian wording, and the authentication,
consent, revocation, and acknowledgement UX.

## Integration contract

| Consumer | Required integration |
| --- | --- |
| Private Chat | Authenticate before persistence; construct trusted control contexts server-side; render the versioned notice; obtain acknowledgment; expose exact grant, revoke, deletion, and payload-free status UI with CSRF/anti-replay protection. |
| Cognition | Preserve protected source edges on every derivative; call ordinary-audience and abstraction validators; implement erasure/recovery adapters; invalidate/reassess all materially tainted state without exposing origin metadata. |
| Research | Minimize briefs, exclude private payload/provenance and identifiers, and keep attribution/public-evidence gates independent of consent to quote. |
| Threads | Check the exact current consent immediately before an identifiable outbound side effect; a passed abstraction result is not publication approval. |
| Behavior | Filter by principal/audience/purpose before ranking or model exposure; use the fail-closed abstraction result and never interpret chat/model output as control authority. |
| Owner Control | Protect the ledger/backup directories and keys; show payload-free audit/deletion/restore status; reconcile pending operations before reads; require human authorization for activation. |

Stage 2 privacy remains blocked while participant recovery and real enrollment,
encryption/key management, retention and processor decisions, notice approval,
and production restore activation remain unresolved. The synthetic two-participant
consent → revoke → delete → old-backup quarantine journey now runs through WSGI;
it does not authorize real-participant use.

## Deterministic evidence in this branch

The synthetic suite covers exact consent and idempotent revocation; wrong
principal/audience/purpose/content and expiry; chat/model/owner forgery;
cross-scope abstraction canaries and direct identifiers; deletion idempotency,
incomplete status, missing provenance, adapter exceptions, crash after ledger
commit, restart and retry; owner-only ledger persistence; concurrent WAL writes;
backup interruption, schema, malformed/symlink path, busy/locked and sanitized
failure cases; old-snapshot payload reconciliation before eligibility;
revocation, tainted-category, and dependency rebuild failures; sanitized audit;
and versioned Russian notice acknowledgement. Tests contain only synthetic data,
make no provider call, and require no credential.
