# OpenAI worker Stage 0 offline deployment

This runbook prepares and exercises the optional, offline worker boundary on an
Ubuntu host. It does not deploy to production, start the repository's existing
services, mount an OpenAI credential, or enable paid inference. Run every command
manually from a reviewed checkout. The example IDs and paths are deployment
parameters, not security properties by themselves.

## A. Prerequisites

- Ubuntu with a supported Docker Engine and the Compose v2 plugin.
- Permission to build images and create containers on this host.
- `sudo` only for the exact `install`, `stat`, and optional cleanup commands below.
- A clean checkout of the reviewed commit. Do not use participant data or secrets.
- No process already using the selected IDs or exact Stage 0 paths.

This image is pinned to the multi-platform digest for
`python:3.12.12-slim-bookworm`. The build installs no packages and copies only the
required `app/core`, `app/trusted`, and `app/ops` Python modules.

## B. Required Docker and Compose behavior

Confirm that Compose supports a top-level project `name`, long bind syntax with
`bind.create_host_path: false`, numeric `user`, `group_add`, `network_mode: none`,
read-only roots, PID/memory limits, capability drops, and `no-new-privileges`:

```console
docker version
docker compose version
docker info | sed -n '/rootless/Ip;/userns/Ip;/Security Options/,+8p'
```

Record whether Docker is rootless and whether user-namespace remapping is active.
Those settings can change bind-mount ownership and the UID visible through
`SO_PEERCRED`; the smoke test below is mandatory for the actual host.

## C. Allocate example UID and GIDs

The examples use worker UID/GID `11001`, planner UID/GID `11002`, and a dedicated
IPC GID `11000`. Choose unused values if any collision is reported:

```console
getent passwd 11001 || true
getent passwd 11002 || true
getent group 11000 || true
getent group 11001 || true
getent group 11002 || true
```

Set the deployment parameters in the current shell:

```console
export STAGE0_WORKER_UID=11001
export STAGE0_WORKER_GID=11001
export STAGE0_PLANNER_UID=11002
export STAGE0_PLANNER_GID=11002
export STAGE0_IPC_GID=11000
export STAGE0_ROOT="$(pwd -P)/data/openai-worker-stage0"
export STAGE0_SOCKET_DIR="$STAGE0_ROOT/socket"
export STAGE0_DATABASE_DIR="$STAGE0_ROOT/provider-db"
test "$STAGE0_SOCKET_DIR" != "$STAGE0_DATABASE_DIR"
```

The containers use numeric identities; creating host login accounts is neither
required nor recommended.

## D. Create only the required directories

The paths are beneath the repository's gitignored `data/` area. Create each exact
directory. Do not recursively change ownership or mode anywhere in the checkout:

```console
install -d -m 0750 -- "$STAGE0_ROOT"
sudo install -d -o "$STAGE0_WORKER_UID" -g "$STAGE0_IPC_GID" -m 0710 -- "$STAGE0_SOCKET_DIR"
sudo install -d -o "$STAGE0_WORKER_UID" -g "$STAGE0_WORKER_GID" -m 0700 -- "$STAGE0_DATABASE_DIR"
```

The Compose file requests `create_host_path: false`. A Compose implementation that
does not honor that setting is incompatible with this runbook. Always perform the
ownership checks below before invoking Compose; if an implementation nevertheless
creates a missing root-owned directory, the worker's ownership/mode validation
still fails closed, but the operator must remove that exact empty directory.

## E. Verify ownership and permission separation

```console
stat -Lc 'socket-dir uid=%u gid=%g mode=%a type=%F' -- "$STAGE0_SOCKET_DIR"
stat -Lc 'database-dir uid=%u gid=%g mode=%a type=%F' -- "$STAGE0_DATABASE_DIR"
test "$(stat -Lc '%u:%g:%a' -- "$STAGE0_SOCKET_DIR")" = "$STAGE0_WORKER_UID:$STAGE0_IPC_GID:710"
test "$(stat -Lc '%u:%g:%a' -- "$STAGE0_DATABASE_DIR")" = "$STAGE0_WORKER_UID:$STAGE0_WORKER_GID:700"
```

The worker owns both directories. The planner receives only IPC-group membership;
it receives neither ownership nor a database mount.

## F. Build the image

```console
docker compose -f ops/compose.openai-worker.stage0.yaml build --pull
docker image inspect almost-reasonable-openai-worker-stage0:local \
  --format 'user={{.Config.User}} entrypoint={{json .Config.Entrypoint}} cmd={{json .Config.Cmd}}'
```

Expected image user: `11001:11001`. No build argument or secret is accepted.

## G. Validate the rendered Compose configuration

```console
docker compose -f ops/compose.openai-worker.stage0.yaml config --quiet
docker compose -f ops/compose.openai-worker.stage0.yaml config
```

Review the output. It must contain exactly `openai-worker` and `planner-probe`, the
separate project name `almost-reasonable-openai-stage0`, no `ports`, no external
network, and no reference to the production `compose.yaml` services.

## H. Start only the offline worker

```console
docker compose -f ops/compose.openai-worker.stage0.yaml up -d --no-deps --wait --wait-timeout 30 openai-worker
docker compose -f ops/compose.openai-worker.stage0.yaml ps
```

This command does not start Cloudflare, OAuth, or the probe. The worker executable
hard-wires `default_openai_provider_config()` and `FakeOpenAITransport([])`; no CLI
flag can select Stage 1 or a credential transport. Compose waits at most 30 seconds
for a shell-free health check to observe the worker-owned `0660` AF_UNIX socket.
If the service does not become healthy, stop here and inspect its sanitized logs;
do not launch the one-shot probe against an unready worker.

## I. Run only the synthetic planner probe

```console
docker compose -f ops/compose.openai-worker.stage0.yaml run \
  --name almost-reasonable-stage0-probe --no-deps planner-probe
```

Expected output is exactly:

```text
Stage 0 probe succeeded: paid inference is disabled.
```

The stopped probe container is deliberately retained for the inspection steps.
The probe sends one fixed synthetic private-conversation request, expects the
`disabled` outcome with zero attempts, prints no prompt, and exits zero. It does
not prove live model compatibility.

## J. Check effective container identities

```console
export STAGE0_WORKER_CID="$(docker compose -f ops/compose.openai-worker.stage0.yaml ps -q openai-worker)"
docker inspect "$STAGE0_WORKER_CID" \
  --format 'worker user={{.Config.User}} groups={{json .HostConfig.GroupAdd}}'
docker inspect almost-reasonable-stage0-probe \
  --format 'planner user={{.Config.User}} groups={{json .HostConfig.GroupAdd}}'
docker compose -f ops/compose.openai-worker.stage0.yaml exec -T openai-worker \
  python -B -c 'import os; print("worker", os.getuid(), os.getgid(), os.getgroups())'
docker compose -f ops/compose.openai-worker.stage0.yaml run --rm --no-deps \
  --entrypoint python planner-probe -B -c \
  'import os; print("planner", os.getuid(), os.getgid(), os.getgroups())'
```

The UIDs must differ. Both processes must report IPC GID `11000` (or the selected
replacement), while their primary GIDs remain distinct.

## K. Check the UID observed through `SO_PEERCRED`

The Stage 0 Compose command enables only a sanitized UID diagnostic. After the
probe, inspect the current worker logs:

```console
docker compose -f ops/compose.openai-worker.stage0.yaml logs --no-color openai-worker
```

There must be a current line of the form:

```text
openai-worker peer uid=11002 authorized=true
```

The number must equal `STAGE0_PLANNER_UID`. If it is an overflow UID, an unexpected
host-mapped UID, or any other value, the deployment fails. Adjust the trusted UID
only after understanding the rootless/userns mapping; never disable `SO_PEERCRED`
or broaden the allowlist merely to obtain a passing result.

## L. Check the socket inode and parent

Run while the worker is active:

```console
stat -Lc 'parent uid=%u gid=%g mode=%a type=%F' -- "$STAGE0_SOCKET_DIR"
stat -Lc 'socket uid=%u gid=%g mode=%a inode=%d:%i type=%F' -- "$STAGE0_SOCKET_DIR/openai-worker.sock"
test "$(stat -Lc '%u:%g:%a' -- "$STAGE0_SOCKET_DIR/openai-worker.sock")" = "$STAGE0_WORKER_UID:$STAGE0_IPC_GID:660"
test -S "$STAGE0_SOCKET_DIR/openai-worker.sock"
test ! -L "$STAGE0_SOCKET_DIR"
```

Expected layout is a worker-owned `0710` directory and a worker-owned `0660`
socket in the dedicated IPC group. Neither path may be world-writable, and the
directory may not be group-writable or reached through a symlink.

## M. Confirm the planner cannot access the provider database

First inspect mounts, then verify the path is absent inside a fresh probe container:

```console
docker inspect almost-reasonable-stage0-probe --format '{{json .Mounts}}'
docker compose -f ops/compose.openai-worker.stage0.yaml run --rm --no-deps \
  --entrypoint python planner-probe -B -c \
  'from pathlib import Path; p=Path("/var/lib/almost-reasonable"); print("provider-db-mounted", p.exists()); raise SystemExit(1 if p.exists() else 0)'
```

The planner mount list must contain only `/run/almost-reasonable`, read-only, and
the command must print `provider-db-mounted False`. This verifies this container
configuration, not arbitrary host-user access outside Docker.

## N. Confirm credentials are absent

```console
if docker inspect "$STAGE0_WORKER_CID" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^OPENAI_(API_KEY|PROJECT_ID|ORG_ID)='; then echo FAIL; else echo PASS; fi
if docker inspect almost-reasonable-stage0-probe --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^OPENAI_(API_KEY|PROJECT_ID|ORG_ID)='; then echo FAIL; else echo PASS; fi
docker inspect almost-reasonable-stage0-probe --format '{{json .Mounts}}'
```

Both checks must print `PASS`; neither mount list may include `secrets/` or a
credential file. Do not paste a credential to test absence.

## O. Confirm Fake transport and Stage 0

The successful probe's `disabled`/zero-attempt result is the runtime evidence.
Also inspect the immutable command and source references:

```console
docker inspect "$STAGE0_WORKER_CID" --format '{{json .Config.Cmd}}'
grep -n 'FakeOpenAITransport\|default_openai_provider_config' app/trusted/openai_worker_server.py
```

There is no Stage 1 activation argument. The provider SQLite schema remains v3,
but this disabled exchange makes no attempt reservation or paid provider call.

## P. Confirm network isolation

```console
docker inspect "$STAGE0_WORKER_CID" --format 'worker network={{.HostConfig.NetworkMode}}'
docker inspect almost-reasonable-stage0-probe --format 'planner network={{.HostConfig.NetworkMode}}'
docker compose -f ops/compose.openai-worker.stage0.yaml exec -T openai-worker \
  python -B -c 'import socket; print(socket.if_nameindex())'
```

Both network modes must be `none`; only loopback should be listed. Stage 0 does not
download artifacts at startup and intentionally cannot contact OpenAI.

## Q. Confirm no TCP port is published

```console
docker inspect "$STAGE0_WORKER_CID" --format 'worker ports={{json .HostConfig.PortBindings}}'
docker inspect almost-reasonable-stage0-probe --format 'planner ports={{json .HostConfig.PortBindings}}'
```

Both values must be `null` or `{}`. IPC is only the bind-mounted AF_UNIX socket.

## R. Inspect sanitized logs

```console
docker compose -f ops/compose.openai-worker.stage0.yaml logs --no-color openai-worker
docker logs almost-reasonable-stage0-probe
```

Expected content is limited to the peer UID authorization diagnostic and the probe
success line. Fail if logs contain a prompt, credential, authorization header,
provider body, participant data, or protected provenance record.

## S. Graceful shutdown

```console
docker compose -f ops/compose.openai-worker.stage0.yaml stop -t 10 openai-worker
docker compose -f ops/compose.openai-worker.stage0.yaml rm -f openai-worker
docker rm almost-reasonable-stage0-probe
```

The worker removes only the socket inode it originally created. The database is
retained. No production Compose project is addressed by these commands.

## T. Safe exact-path cleanup and stale sockets

After graceful shutdown, the socket should be absent:

```console
test ! -e "$STAGE0_SOCKET_DIR/openai-worker.sock"
```

If an unclean stop leaves a path, do not let the worker replace it and do not use a
recursive deletion. First confirm the Stage 0 project has no running worker, then
inspect the exact path and its inode/type:

```console
docker compose -f ops/compose.openai-worker.stage0.yaml ps
sudo stat -Lc 'uid=%u gid=%g mode=%a inode=%d:%i type=%F' -- "$STAGE0_SOCKET_DIR/openai-worker.sock"
sudo test -S "$STAGE0_SOCKET_DIR/openai-worker.sock"
```

Only after independently confirming that no worker owns that exact socket may the
operator remove exactly it:

```console
sudo rm -- "$STAGE0_SOCKET_DIR/openai-worker.sock"
```

To remove empty test directories, use exact non-recursive operations. Preserve the
database if its state is needed for inspection:

```console
sudo rmdir -- "$STAGE0_SOCKET_DIR"
# Remove provider.sqlite3 only after an explicit retention decision, then:
# sudo rmdir -- "$STAGE0_DATABASE_DIR"
# rmdir -- "$STAGE0_ROOT"
```

## U. Permission and user-namespace failures

- `socket directory is unsafe`: verify exact owner/GID/mode, absence of symlinks,
  and that the directory is `0710`, not group- or world-writable.
- `worker socket creation failed`: verify the worker has supplemental IPC-group
  membership and can set the socket to that group; inspect any pre-existing path.
- Probe permission denied: verify planner IPC-group membership and parent execute
  permission; do not change the socket to `0666` or the directory to `0777`.
- Probe connects but is immediately closed: compare the logged kernel UID with the
  explicit allowlist. Rootless Docker or userns remapping may translate IDs.
- Bind mount denied or files appear owned by an unexpected host UID: determine the
  daemon's subordinate-ID mapping and prepare the host paths for that mapping.
- Separate per-container user namespaces may make the planner UID unrepresentable
  to the worker. This topology is incompatible unless the operator can establish
  and verify one stable shared kernel mapping. Fail closed; do not replace peer UID
  authentication with JSON claims or group membership alone.
- A read-only socket bind works for AF_UNIX `connect`; if a platform rejects it,
  report the platform limitation rather than making source or database mounts
  writable.

## V. Pass/fail checklist

Pass only if every item is true:

- [ ] The image build used the reviewed digest and no credential/build secret.
- [ ] Only the separate Stage 0 Compose project and two expected services exist.
- [ ] Worker and planner effective UIDs differ.
- [ ] Only both intended identities have IPC GID membership.
- [ ] The kernel-observed planner UID equals the worker allowlist entry.
- [ ] Parent is worker-owned `0710`; socket is worker-owned/IPC-group `0660`.
- [ ] The planner socket bind is read-only and no provider database is mounted.
- [ ] The worker alone has the writable provider-database directory.
- [ ] Both roots are read-only; capabilities are empty; no-new-privileges is set.
- [ ] Both network modes are `none`, with no published TCP port or Docker socket.
- [ ] Neither container has OpenAI credentials or participant data.
- [ ] The probe exits zero only for `disabled` with zero attempts.
- [ ] Logs are sanitized and the graceful stop removes only the owned socket.

Failure of any item means host isolation is not validated. Automated repository
tests prove configuration and application behavior but do not substitute for this
host-specific smoke run.

## Future Stage 1 requirements (not implemented)

Live inference needs a separate reviewed change: a worker-only OpenAI credential
mount; trusted organization/project billing scope; a verified project hard spend
limit; explicit Stage 1 activation and emergency stop; verified model access;
provider-specific egress enforced outside Compose while keeping planner egress
disabled; request rate limits; authenticated participant ingress; protected audit;
synthetic cost-bounded live tests; confirmation of `store=false`; validation of
real response/error/usage shapes; and incident-response/rollback procedures.

`network_mode: none` is intentionally incompatible with OpenAI access. Removing it
is not a sufficient egress policy, and Compose networking alone must not be claimed
to provide a destination-level HTTPS allowlist.
