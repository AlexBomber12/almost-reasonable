# B09B owner emergency Stop

The INT-26 owner console can assert both the application Stop and the independent
worker Stop. Configure the owner console with `--worker-stop-socket` and
`--worker-stop-uid` together. Without these flags, worker control is unavailable.
Use a separate socket from worker telemetry and inference. Start the dedicated
service under the worker OS UID with an existing protected Stop file:

```sh
python -B -m app.trusted.worker_stop \
  --socket /run/pilot-worker-stop/stop.sock \
  --stop-file /run/almost-reasonable-private-pilot/control/emergency-stop.json \
  --allow-uid OWNER_UID --socket-group-gid IPC_GID
```

The socket directory must be worker owned, mode `0710`, with a dedicated IPC
group containing the worker and owner UIDs. The socket is `0660`; the protected
Stop directory remains worker only `0700`, and its existing JSON file must be
worker owned `0400`. Same-UID installations may omit the group flag and use a
worker owned `0700` socket directory and `0600` socket. The service and owner
client check peer UIDs using `SO_PEERCRED`. Neither service accepts paths,
provider requests, a clear command, or credentials over the socket. The
telemetry service remains read only.

The owner must pass Host, source address, Origin, owner session and CSRF checks
before obtaining a server generated worker Stop operation ID. The ID has its
own worker scope and expires after five minutes or at session expiry. An
application-only operation ID cannot authorize worker control. Repeating a
worker assertion is safe; it never calls a provider. The application writes
its own Stop and attempts the worker assertion separately. The worker replies
only after an atomic protected-file replacement and directory sync. A missing
reply is **uncertain**, not confirmation; an unavailable socket is reported
separately. Audit and operation outcomes are durable in the owner store. Retry
the same authorized operation before it expires to reconcile a lost reply.
After expiration or a new login, the console shows the latest sanitized
outcomes for that owner; inspect the independent current states and issue a
fresh operation if needed. Stopping or restarting either control service never
clears either Stop.

Stop prevents subsequent calls at the existing application and worker gates;
the application checks again before releasing an in-flight answer. It cannot
cancel an already submitted provider request or undo its charge. The console
does not change activation, allowance, spend suspension, credentials, grants,
or usage accounting. Application-only resume is labelled as such. This
endpoint never clears the worker Stop.

Worker recovery is an explicit, separate local operator procedure. First
verify operator access to the worker UID, inspect the worker's protected
activation, allowance, spend suspension, provider accounting and current
Stop, and reconcile any uncertain in-flight usage. Keep the application Stop
on during inspection. Only after a separate authorization to resume, use a
reviewed local tool under the worker UID. Validate the existing worker-owned
`0700` directory and regular, single-link `0400` Stop file as in
`docs/int22-private-pilot.md`. Write exactly
`{"version":"private-pilot-emergency-stop-v1","stop":false}` to a new
`O_EXCL` temporary file in the same directory, set mode `0400`, sync the file,
verify that the original file and directory identities did not change,
atomically replace the file, and sync the directory. Verify the new file with
worker-side inspection before clearing the application Stop. No part of this
B09B endpoint performs recovery.
`complete_mvp=BLOCKED` remains unchanged.
