# EventStore backup and quarantine rehearsal

Run this utility as the same trusted operating-system identity that owns the
EventStore and **current authoritative** privacy ledger. Use absolute paths in
trusted directories. The source database and ledger must already exist and have
safe owner permissions. Supply a new, absent backup or quarantine pathname.

```sh
python -m app.ops.eventstore_backup backup \
  --source /absolute/path/events.db \
  --destination /absolute/path/eventstore-snapshot.db

python -m app.ops.eventstore_backup restore \
  --backup /absolute/path/eventstore-snapshot.db \
  --ledger /absolute/path/current-privacy-ledger.db \
  --quarantine /absolute/path/rehearsal-candidate.db
```

The backup command uses SQLite's online backup API, so committed WAL data is
included. It validates EventStore schema and integrity and writes an owner-only
`.metadata.json` beside the snapshot. Restore validates that metadata and the
backup, then uses the existing privacy reconciliation adapter and the explicitly
supplied current ledger to replay deletions and revocations. It checks the ledger
generation, verifies the result, and seals a read-only quarantine database with
an owner-only `.ready.json` marker. Never use an old ledger snapshot as the
`--ledger` input. The command does not overwrite or replace the current ledger.

Output is a single JSON status and reason code. `backup_created` and
`quarantine_verified` exit zero; `backup_failed`, `restore_failed`, and
`quarantine_blocked` exit nonzero. Failure output omits paths, private content,
credentials, and raw exception text. The recovery layer removes only partial
files it created and preserves operator-owned files. A blocked quarantine may
remain for inspection, but it has no readiness marker. Treat an interrupted
invocation as incomplete and inspect its outputs before a fresh attempt.

`quarantine_verified` means only that this candidate passed the existing
EventStore and privacy reconciliation checks at that ledger generation. It is
**not an active application database**, an activation instruction, or production
readiness. This utility has no activation or database-switch command. It does
not start containers, clear Stop, change pilot authority, or contact a provider
or Meta.

Only EventStore is backed up. Authentication and session stores, owner controls,
worker provider accounting, allowance, activation files, and credentials are
outside this utility. A complete disaster recovery procedure requires separate
reviewed handling of those stores, including prevention of session resurrection
and spend or allowance rollback, plus a coherent database-switch protocol. This
command does not provide encryption or an atomic multi-store snapshot.

The underlying filesystem and privacy contracts are described in
[privacy-recovery.md](privacy-recovery.md).
