# Privacy consent, deletion, and recovery foundations

Status: foundation only. This change does **not** enroll participants, activate a
private-to-public path, or make Stage 2 privacy complete.

## Security boundary and guarantees

`app/privacy` accepts control operations only with a `TrustedPrivacyContext`
whose origin is `authenticated_control`. Constructing that value is not
authentication. The future Private Chat or Owner Control boundary must construct
it only after server-side authentication and anti-replay/CSRF validation; chat
text, model output, an asserted participant ID, and an owner claim cannot do so.

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
always come from the service clock rather than rejected request fields.

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

Every committed deletion or revocation advances a monotonic privacy-control
generation. Restore reconciles one transactionally consistent snapshot, blocks
if that generation changes during its work, and writes the reconciled generation
into both its typed result and readiness marker. Immediately before an eventual
database switch, an integration must atomically fence the ledger generation and
bind the verified quarantine path/inode, readiness marker, and sanitized outcome
to that exact switch. This package deliberately provides no activation operation
until that deployment-specific boundary exists. The readiness file and typed
eligibility result are not activation authority.

Deletion has five operator-visible dispositions: `pending`, `applied`,
`incomplete_blocked`, `invalid_authorization`, and `not_eligible`. Reports use
references, categories, and reason codes only; they do not display deleted
content. Invalid-authentication attempts are payload-free audited outcomes but
are not persisted as authoritative deletion operations, so they cannot reserve
an operation ID and suppress a later authenticated request.

## Application erasure adapter and migration specification

EventStore schema v3 is the known source model, but it exposes no reviewed public
API that can atomically remove a private payload and invalidate every dependent
memory, summary, revision, index, abstraction, and duplicate key. This branch
therefore does not mutate EventStore tables and does not present partial SQL as a
complete deletion. `ErasureAdapter` is the strict integration boundary.

For one `DeletionRequest`, an integrated adapter must make an idempotent
transaction in the application database which:

1. selects the exact authenticated principal-scoped eligible source;
2. deletes or irreversibly anonymizes the private payload;
3. preserves only a content-free event tombstone;
4. follows protected provenance and invalidates derived private memory,
   summaries, revision rationales, shared abstractions, and search/index rows;
5. destroys bounded/keyed duplicate material where applicable; and
6. returns all required `ContentCategory` values only after those effects commit.

The adapter must return blockers, including missing source provenance, instead
of guessing. Legal holds/public actions must be reported as not eligible by the
authenticated integration layer. It must be safe to repeat after an uncertain
process crash.

If integration needs EventStore schema v4, its reviewed migration must add
protected source-to-derived edges for every memory/summary/revision/index/shared
abstraction, explicit retention/invalidation states, and references to
destroyable duplicate-key material. It must preserve v3 event identities,
principal audiences, and content-free tombstones; migrate in one application-DB
transaction; reject orphaned or untyped provenance; support rollback before
commit; and include v3-to-v4, interrupted-migration, cascade, concurrency, and
restore tests. The privacy ledger remains separately versioned. No such shared
schema migration is performed here.

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
undeclared sidecars. Restore uses the same immutable view of the backup.

Source, destination, and metadata paths must be absolute regular paths. Leaf
symbolic links, symbolic links anywhere in a parent chain, unsafe parents,
existing outputs, schema mismatch, integrity failure, interruption, and SQLite
busy/locked faults fail closed. The source inode remains pinned while schema
validation and the online copy run; SQLite opens the source through that
descriptor, and the resulting connection is checked against the same inode.
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
   then repeats schema-version and SQLite integrity verification after those
   writes;
7. confirms that every applicable ledger deletion was applied to this quarantine
   without changing the live database disposition;
8. durably records a payload-free restore outcome; and
9. only then writes an owner-only `.ready.json` eligibility marker.

The owned quarantine descriptor remains open through integrity checks,
reconciliation, schema revalidation, and readiness publication. Restore adapters
receive a descriptor-backed path valid for that callback lifetime, rather than
the replaceable public pathname; the public path must still name the owned inode
at every validation boundary and before eligibility is recorded.
Backup metadata is likewise opened through a no-follow pinned descriptor and
read with a strict size bound, so validation and parsing cannot be redirected to
a replacement or unbounded special file.

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

Stage 2 privacy remains blocked until these interfaces are implemented together,
participant/admin authentication and recovery are finalized, EventStore has a
reviewed complete erasure cascade, encryption/key management and retention are
configured, the notice receives operator/legal approval, and the combined
two-participant consent → revoke → delete → old-backup restore scenario passes.

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
