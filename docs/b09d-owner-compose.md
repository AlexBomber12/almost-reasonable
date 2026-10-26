# B09D owner and worker controls in Compose

`ops/compose.owner-control.yaml` is a separate three-service project. It reuses
`ops/Dockerfile.private-pilot-app` for all three processes: these existing
modules need no SDK installation. Supply one explicit approved release tag or
registry digest via `PILOT_OWNER_CONTROL_IMAGE`; there is no build or floating
image default in the deployment file. All processes retain the pilot's read-only
root, dropped capabilities, no-new-privileges, resource limits and numeric UIDs.

A separate project is the smallest arrangement that preserves the current
four-file pilot configuration, including its allowance database override. It
contains no inference worker or dependency on one. The owner keeps host networking
and binds only `127.0.0.1:18026`, preserving the existing proxy route and HTTP
admission checks. Telemetry and worker Stop use `network_mode: none`. Each can
be started, stopped or restarted independently. No existing participant service
mount changes, Docker socket, privileges, broker or new API are involved.

The owner receives only its existing application directory and read-only mounts
of two dedicated IPC directories. It receives no worker database, worker control,
activation, allowance or credential mount. The control services run as
`11021:11021`, with IPC group `11020`; the owner runs as `11022:11022` with that
same supplementary group. IPC directories are worker owned `0710`, sockets
`0660`. Both clients and services retain their existing `SO_PEERCRED` checks.

Telemetry mounts the existing worker-state and control **directories read-only**
so committed SQLite WAL data and atomic Stop replacement remain visible. It
queries only `provider.sqlite3` and reads only `emergency-stop.json`. These
are directory-level mounts: other files co-located there are readable under the
worker UID, including allowance or activation if present. Do not put credentials
in either directory. The Stop service receives only the existing control directory
writable, required for atomic replacement and directory sync; it gets no worker
state, allowance database or credential mount and no network. Its existing fixed
protocol can only assert an existing valid Stop. This is process/container
isolation, not protection against arbitrary compromised code running as the
worker UID inside the mounted control directory.

Neither service creates a source database or Stop file. The owner launcher
rejects missing, unsafe or uninitialized application stores. A missing/invalid
telemetry source is unavailable, never zero. A reachable telemetry service is
not inference readiness. A missing Stop acknowledgement is uncertain or
unavailable, never confirmed. See [telemetry](worker-telemetry.md),
[Stop](b09b-worker-emergency-stop.md) and [owner console](int26-owner-pilot.md)
for the unchanged protocols and HTTP authorization behavior.

## Migration on AI-Server

These are operator commands for a separately authorized deployment, not steps
executed during development. Work in an approved release checkout. Do not edit
Cloudflare, nginx, tunnels or routing. Keep application Stop enabled, leave the
provider worker stopped, and preserve expired/exhausted paid authorizations.
No step below prepares allowance, provisions stores, resumes, or starts inference.

1. Prepare and record the current deployment before stopping anything. Preserve
   the existing env file, all four overrides, old owner container, its image ID,
   and the approved new image pin. Keep records owner-only and outside Git;
   resolved Compose configuration can contain secrets. Use the complete pilot
   invocation whenever inspecting that project:

   ```bash
   pilot=(docker compose --env-file /home/alexey/.config/almost-reasonable/int22.env
     -f ops/compose.private-pilot.yaml
     -f /home/alexey/.config/almost-reasonable/int22.proxy.yaml
     -f /home/alexey/.config/almost-reasonable/int22.network.yaml
     -f /home/alexey/.config/almost-reasonable/int22.multiturn-20261022-02.yaml)
   umask 077
   migration=/home/alexey/.config/almost-reasonable/b09d-migration
   mkdir -p "$migration"
   # Save once; never overwrite the pre-migration record on a retry.
   if test ! -e "$migration/pilot-before.json"; then
     "${pilot[@]}" config --format json > "$migration/pilot-before.json"
   fi
   "${pilot[@]}" ps -a
   docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}'
   ```

   Identify the manually launched owner by its command, mounts and loopback
   listener, not a guessed name. Set `OLD_OWNER` to that container's name.
   Save `docker inspect "$OLD_OWNER"` to a protected file in `$migration` once
   (it may contain sensitive environment values); record its restart policy,
   retry count, image ID and the previous manual startup recipe. Keep this
   container for rollback. Verify that the new image contains all three modules
   and retain the old image; do not retag the old image or use `:local`/`:latest`
   for the new deployment.

2. Copy `ops/owner-control.env.example` to
   `/home/alexey/.config/almost-reasonable/b09d-owner.env` with mode `0600`.
   Set its image to the approved release tag/digest. Copy actual application,
   worker-state and control source paths from the protected resolved pilot
   configuration, rather than assuming the example paths. Confirm the effective
   provider database is `/var/lib/pilot-worker/provider.sqlite3`; if it differs,
   use a protected owner-project override to point telemetry to the exact
   existing database inside that directory. Never change the worker's
   `--allowance-database` selection to fit an example.

   For the existing deployment, set the owner host to
   `almostreasonable-owner.alexbomber.com`, origin to
   `https://almostreasonable-owner.alexbomber.com`, participant origin to
   `https://almostreasonable.alexbomber.com`, copy the exact allowed client from
   the retained owner's protected configuration, and keep trusted proxy
   `127.0.0.1`. Keep the operator's IP in the host file rather than public Git.
   No configuration file is injected as a
   service environment; Compose uses it only for interpolation.

   Verify existing application files `events.db`, `ipc-state.db`, `ledger.db`
   and `owner/owner.sqlite` are initialized, application owned `0600`; owner
   directory is `0700`. Preserve all stores and their WAL/SHM sidecars. Worker
   directories remain worker owned `0700`, provider DB/sidecars `0600`, Stop
   `0400`. Inspect directory contents for co-located state as described above.
   Do not copy a live database, initialize an empty substitute, change Stop
   contents, or modify activation/allowance. Take the deployment's normal
   consistent protected backup without resetting accounting.

   Create only the two new IPC directories. These example paths must match
   the new env file and must differ from each other, inference IPC, state and
   control directories. On retries inspect existing ownership/mode before
   proceeding; do not recursively chmod/chown existing state.

   ```bash
   sudo install -d -o 11021 -g 11020 -m 0710 \
     /srv/almost-reasonable/int22/telemetry-ipc \
     /srv/almost-reasonable/int22/stop-ipc
   controls=(docker compose
     --env-file /home/alexey/.config/almost-reasonable/b09d-owner.env
     -f ops/compose.owner-control.yaml)
   "${controls[@]}" config --quiet
   "${controls[@]}" config --services
   ```

   Expect exactly `owner-console`, `worker-telemetry`, `worker-stop`. If a
   protected owner-project override is needed, append it to **every** controls
   invocation, including rollback. Keep this project separate from `pilot`.
   Confirm the rendered image pin, UIDs, mounts, numeric loopback bind and
   `none` network modes. Stage/load the approved pinned image before cutover;
   startup below never builds or pulls. Host networking requires Linux Docker
   with the normal host loopback semantics used by this deployment.

3. Start just the two control services; this does not touch the old owner or
   pilot services:

   ```bash
   "${controls[@]}" up -d --no-deps --no-build --pull never worker-telemetry worker-stop
   "${controls[@]}" ps -a
   ```

   Verify both run as `11021`, directories `0710`, socket owner/group
   `11021:11020` and mode `0660`. Use targeted `exec`/`stat` or host-side
   inspection; do not print protected file contents into logs. Failure here is
   partial progress: stop these two services and retain the old owner unchanged.

4. Cut over the listener. Save the old restart policy before disabling it so
   an external restart cannot compete for port 18026. Disable any known local
   manual-owner restart job during cutover and record how to restore it. Do
   not stop or change unrelated deployment services.

   ```bash
   : "${OLD_OWNER:?set the verified manually launched owner container name}"
   docker update --restart=no "$OLD_OWNER"
   docker stop --time 10 "$OLD_OWNER"
   sudo ss -ltnp 'sport = :18026'
   ```

   Require port 18026 to be free. If another listener remains, identify it and
   stop here; do not kill an unknown process or change routing/ports. The old
   container is stopped, not removed or renamed. Then:

   ```bash
   "${controls[@]}" up -d --no-deps --no-build --pull never owner-console
   "${controls[@]}" ps -a
   sudo ss -ltnp 'sport = :18026'
   "${pilot[@]}" ps -a provider-worker
   ```

   Require only `127.0.0.1:18026`, all three new services running, and inference
   still stopped. `up` is restartable and does not renew authority. On a crash
   or SIGKILL a stale socket can intentionally prevent restart. Stop its
   specific service and verify no container/process owns that directory's
   socket before deleting **only that stale socket** under the worker UID;
   then repeat the service-targeted `up`. Never unlink a live socket or remove
   a whole directory. Graceful `stop`/`restart` removes each service's socket.

5. Verify through the unchanged owner HTTPS domain: login, exact Host/Origin,
   allowed-client and trusted-proxy admission, real recorded provider usage,
   separate application/worker Stop and recent diagnostics. A working status
   socket does not prove model readiness or restore authority. Use the existing
   authenticated emergency Stop action; confirm **both** acknowledgements and
   independently observe both Stops active. Do not clear either Stop to test.
   Compare worker accounting/allowance and activation against the protected
   pre-migration evidence. Restart only these three services and verify Stops
   remain active and accounting unchanged. With either control service stopped,
   the owner must report unavailable telemetry or a partial worker Stop outcome;
   restore it using targeted `up`. Leave inference stopped throughout.

## Rollback and ordinary management

For failure after listener cutover, stop the new owner first and confirm port
18026 is free. Then start the **retained** old container, which still has its
original image, arguments, mounts and routing. Restore its exact recorded
restart policy (including `on-failure` retry limit) and any recorded manual
restart job only after checking its listener. Never launch both owners together.

```bash
"${controls[@]}" stop owner-console
sudo ss -ltnp 'sport = :18026'
docker start "$OLD_OWNER"
# Set OLD_OWNER_RESTART from the protected pre-migration inspect record.
: "${OLD_OWNER_RESTART:?restore the exact saved policy, e.g. no or on-failure:3}"
docker update --restart="$OLD_OWNER_RESTART" "$OLD_OWNER"
"${controls[@]}" stop worker-telemetry worker-stop
```

Rollback preserves current Stops, usage, allowance, owner audit/session state
and all existing image pins/overrides; it does not restore an older database
snapshot. The old owner may lack worker-control wiring, so verify worker Stop
independently and keep inference stopped. If failure occurred before cutover,
leave the old owner running and stop only the new control services. If the old
container was already stopped when retrying, use the saved record to identify
that partial stage; do not overwrite it with the Compose owner's metadata.
Keep the reviewed Compose file and env file together for subsequent targeted
`stop`, `up` or `restart` commands. Do not run bare pilot `up`, `down`,
`--remove-orphans`, preparation commands, or any inference startup as part of
this migration. No step modifies the four existing pilot inputs.

## Disposable validation

Existing owner/telemetry/Stop behavioral tests cover protocol faults, dropped
acknowledgements, admission checks and provider gates. The additional test
resolves the actual Compose model, then uses its exact commands and mounts
with real sockets, production numeric UIDs/GIDs and disposable synthetic state.
It keeps committed usage in SQLite WAL, authenticates over real owner HTTP,
asserts both Stops, tests unauthorized kernel peers, graceful restart/atomic
replacement, unavailable services and accounting preservation with no inference
worker, provider credentials or external network calls.

```bash
docker build -f ops/Dockerfile.private-pilot-app \
  -t almost-reasonable-private-pilot-app:b09d-test .
B09D_TEST_IMAGE=almost-reasonable-private-pilot-app:b09d-test \
  python -m pytest -q tests/test_b09d_owner_deployment.py
B09D_TEST_IMAGE=almost-reasonable-private-pilot-app:b09d-test scripts/ci.sh
```

The container test builds the existing app Dockerfile automatically when a
Docker daemon is available; `B09D_TEST_IMAGE` reuses an already built image.
It skips with an explicit reason when Docker/Compose/the daemon is unavailable;
report that separately from protocol validation. The seed helper is privileged only enough to assign
**disposable fixture ownership**; deployed services have no added capabilities.
This local validation cannot verify AI-Server's unprovided host overrides,
actual file inventory/ownership, selected image or proxy configuration.
