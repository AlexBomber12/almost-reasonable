# Read-only worker telemetry for the owner console

Start `app.trusted.worker_telemetry` explicitly under the **worker OS UID**. It
is a separate process and AF_UNIX socket from inference. It needs only the
worker provider-state SQLite database and the worker emergency Stop JSON file.
Do not give the owner web process access to either source, to activation or
allowance files, or to credentials. The service has no provider configuration,
credential argument, network listener, or inference operation.

Example paths below are placeholders. Create the telemetry socket directory
under the worker UID with mode `0710`, group set to a dedicated IPC group that
contains both worker and owner UIDs. The socket is created as `0660` in that directory.
The group gets only traversal and socket connect permission. The provider
database and Stop directory remain worker-only `0700`; their files remain
`0600` and `0400` respectively. The service also supports a worker-only
`0700` socket directory and `0600` socket if the owner runs as the same UID.

```sh
python -B -m app.trusted.worker_telemetry \
  --socket /run/pilot-worker-status/status.sock \
  --provider-db /var/lib/pilot-worker/provider-state.db \
  --stop-file /run/almost-reasonable-private-pilot/control/emergency-stop.json \
  --allow-uid OWNER_UID \
  --socket-group-gid IPC_GID
```

Set `--worker-telemetry-socket` and `--worker-telemetry-uid` together on the
owner console. The UID is the worker UID and is checked with kernel
`SO_PEERCRED`. The telemetry server checks the owner UID with the same kernel
mechanism before it reads either protected source. Keep this UID allowlist
narrow; it is distinct from the inference worker's socket and protocol.
The owner reads only a fixed versioned status request. Requests contain no
paths, SQL, provider operation, or control instruction. The bounded response
contains only collection time, Stop validity/state, finalized attempt count,
recorded token totals, count of finalized attempts with incomplete usage,
count of unresolved attempt reservations, and spend
suspension. The service does not clear Stop, spend suspension, or allowance.

The service reads the current Stop pathname on every request, so atomic
replacement is visible. A missing, unsafe, or malformed Stop file is shown as
**unavailable**, not as a verified Stop state. Provider data is read through a
read-only SQLite connection after trusted path and current schema validation.
Existing WAL state is included. Missing, unsafe, replaced, incompatible,
malformed, contended, or slow provider storage is **unavailable**, never zero.
A known zero is shown only after a valid database query. The sums include every
recorded provider attempt regardless of operation name. They total recorded
token fields only; unknown or incomplete finalized usage is counted separately.
Token counters must also satisfy the provider contract's relationships when
the relevant fields are present. The AF_UNIX contract keeps exact integers;
the owner HTTP response carries recorded token totals as decimal strings so
browser number parsing cannot round them.
An unresolved reservation has no final usage record. It may precede provider
contact, so it is not a confirmed call or known charge. When one exists,
actual total usage remains uncertain even if recorded tokens are zero. A
matching reservation and usage row count only as finalized usage; inconsistent
rows make provider evidence unavailable. Stored identities and timestamps must
match the accounting writer's validated form, and a spend suspension must
reference its matching finalized usage record. Both aggregates come from one SQLite
read transaction.
The server applies one deadline to the complete request, and the owner applies
one deadline to the complete bounded response frame. Neither
these totals nor a reachable status socket prove provider billing, inference
worker liveness, model readiness, or authority to call a provider.

The Stop file and SQLite data are collected sequentially. The response is not
an atomic global snapshot. For a live database, keep the worker-owned database
directory available to SQLite, including its `-wal` and `-shm` sidecars. Do not
mount or copy just the main database file: that can omit committed WAL data.
Do not bind-mount a single Stop-file inode: atomic replacement would leave the
status process reading an old inode. Mount the protected directory instead.

Stop the telemetry process with `SIGTERM`, `SIGINT`, or its supervisor. It
removes only its own socket inode on exit. Stopping or restarting it changes
neither control source. The owner console remains usable without this service
and labels worker observations unavailable.

This is B09A observation only. Remaining B09 control-plane work includes an
authorized worker-side Stop operation, a defined recovery and authority model,
and validation of the complete end-to-end stop path. `complete_mvp=BLOCKED`
remains unchanged; this telemetry does not authorize live activation.
