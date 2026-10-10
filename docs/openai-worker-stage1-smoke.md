# OpenAI worker Stage 1 controlled smoke

This runbook prepares one synthetic, non-publishing OpenAI Responses API smoke
operation on Ubuntu. It does not authorize a provider request by itself. A real
request requires a separate operator decision in Phase C, a current activation
artifact, a cleared worker-only stop artifact, and independently verified host
egress policy. Stop on the first failed check.

The implementation was developed and tested without a real API credential or
provider call. Stage 0 remains the default and is unchanged. The Stage 1 worker
accepts only the fixed `stage1-openai-smoke-v1` operation through its AF_UNIX
socket; its output is an untrusted proposal and is never delivered or executed.

## Fixed contract and current provider facts

The trusted application configuration is:

- model `gpt-6-luna`;
- provider configuration `openai-stage1-smoke-config-v1`;
- prompt `agent-proposal-v1` and the repository's approved Core Self;
- maximum 512 output tokens;
- one logical operation and one transport attempt;
- 30-second attempt timeout and 40-second overall timeout;
- `store=false`, `tools=[]`, `background=false`, `stream=false`, and
  `truncation=disabled`;
- strict Responses Structured Outputs through `text.format`;
- SDK retries disabled with `max_retries=0`; and
- standard short-context pricing snapshot
  `openai-pricing-2026-10-10-standard-short-v1`: USD 0.10/M input,
  USD 0.01/M cached input, and USD 0.50/M output.

These values matched the official OpenAI documentation on 2026-10-10. Recheck
the [Responses create reference](https://developers.openai.com/api/reference/resources/responses/methods/create),
[Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses),
[GPT-6 Luna model page](https://developers.openai.com/api/docs/models/gpt-6-luna),
[pricing](https://developers.openai.com/api/docs/pricing), and
[error codes](https://developers.openai.com/api/docs/guides/error-codes)
immediately before a paid run. Documentation does not prove this project's model
access. A model, schema, refusal, incomplete-response, or access failure is a
classified smoke outcome; it must not cause a model change, larger token limit,
schema fallback, or second request.

The official error guide currently distinguishes
`project_spend_limit_exceeded` and `organization_spend_limit_exceeded` from
temporary rate limiting. Only those confirmed spend-limit codes persist the
application suspension. A generic HTTP 429 does not.

## Trust model

The planner image has the fixed synthetic request, AF_UNIX client, and protocol
codec only. It has no OpenAI SDK, credential loader, secret mount, control mount,
provider database, or network. Its JSON cannot select a model, billing scope,
tools, storage, retry behavior, or stop state.

The worker authenticates the planner with Linux `SO_PEERCRED`, then independently
checks the operation ID, correlation ID, canonical SHA-256 request digest, model,
provider configuration, activation window, and the unchanged activation artifact.
The smoke-session ID is present only in that worker-owned artifact and never in
IPC. The worker checks the protected stop file before authorization and the
gateway checks it again immediately before transport.

The existing SQLite attempt reservation for
`stage1-openai-smoke-v1:1` is the durable consumption record. Completion,
refusal, invalid output, timeout, connection uncertainty, or a provider error
consumes the one attempt. Restarting with the same database does not rearm it.
There is no reset or rearm operation in the Stage 1 interface. Do not delete or
replace the live database to obtain another call; another real operation requires
a new reviewed change and authorization.

The worker reads the credential only from
`/run/secrets/openai-api-key`. It requires a non-symlink, worker-owned regular
file with mode `0400`, one link, bounded nonempty content, and no surrounding
whitespace. Project and organization IDs come from the separately protected
activation artifact and are passed explicitly to the fixed
`https://api.openai.com/v1` SDK client. The key is never accepted through an
environment variable, argument, image layer, IPC message, log, or exception.

## Phase A — readiness

### A1. Verify the reviewed revision and stopped Stage 0

From the reviewed checkout:

```console
git status --short --branch
git rev-parse HEAD
git show --no-patch --format=fuller HEAD
docker compose -f ops/compose.openai-worker.stage0.yaml ps
docker compose -f ops/compose.openai-worker.stage1.yaml ps
```

Pass only if the tree is clean, `HEAD` is the human-approved revision, and no
Stage 0 or old Stage 1 container is running. Do not use production participant
data in any directory below.

### A2. Allocate identities and separate paths

The examples preserve the previously tested IDs: worker `11001:11001`, planner
`11002:11002`, and IPC GID `11000`. Confirm they do not collide unexpectedly:

```console
getent passwd 11001 || true
getent passwd 11002 || true
getent group 11000 || true
docker info | sed -n '/rootless/Ip;/userns/Ip;/Security Options/,+8p'
```

Set exact paths. Keep dry-run and live databases separate; both are separate from
the retained Stage 0 data.

```console
export STAGE1_WORKER_UID=11001
export STAGE1_WORKER_GID=11001
export STAGE1_PLANNER_UID=11002
export STAGE1_PLANNER_GID=11002
export STAGE1_IPC_GID=11000
export STAGE1_ROOT="$(pwd -P)/data/openai-worker-stage1"
export STAGE1_SOCKET_DIR="$STAGE1_ROOT/socket"
export STAGE1_CONTROL_DIR="$STAGE1_ROOT/control"
export STAGE1_DRY_DATABASE_DIR="$STAGE1_ROOT/provider-db-dry-run"
export STAGE1_LIVE_DATABASE_DIR="$STAGE1_ROOT/provider-db-live"
export STAGE1_SECRET_DIR="$STAGE1_ROOT/worker-secret"
export STAGE1_CREDENTIAL_FILE="$STAGE1_SECRET_DIR/openai-api-key"
test "$STAGE1_ROOT" != "$(pwd -P)/data/openai-worker-stage0"
test "$STAGE1_DRY_DATABASE_DIR" != "$STAGE1_LIVE_DATABASE_DIR"
```

Create only the exact directories:

```console
install -d -m 0750 -- "$STAGE1_ROOT"
sudo install -d -o "$STAGE1_WORKER_UID" -g "$STAGE1_IPC_GID" -m 0710 -- "$STAGE1_SOCKET_DIR"
sudo install -d -o "$STAGE1_WORKER_UID" -g "$STAGE1_WORKER_GID" -m 0700 -- "$STAGE1_CONTROL_DIR"
sudo install -d -o "$STAGE1_WORKER_UID" -g "$STAGE1_WORKER_GID" -m 0700 -- "$STAGE1_DRY_DATABASE_DIR"
sudo install -d -o "$STAGE1_WORKER_UID" -g "$STAGE1_WORKER_GID" -m 0700 -- "$STAGE1_LIVE_DATABASE_DIR"
sudo install -d -o "$STAGE1_WORKER_UID" -g "$STAGE1_WORKER_GID" -m 0700 -- "$STAGE1_SECRET_DIR"
```

Pass only if `stat -Lc '%u:%g:%a:%F'` reports worker ownership, socket mode
`710`, and control/database/secret-parent mode `700`. No path may be a symlink.

### A3. Build and inspect the two images

```console
export STAGE1_DATABASE_DIR="$STAGE1_LIVE_DATABASE_DIR"
docker compose -f ops/compose.openai-worker.stage1.yaml build --pull
docker image inspect almost-reasonable-openai-worker-stage1:local \
  --format 'worker user={{.Config.User}} entrypoint={{json .Config.Entrypoint}} cmd={{json .Config.Cmd}}'
docker image inspect almost-reasonable-openai-planner-probe-stage1:local \
  --format 'planner user={{.Config.User}} entrypoint={{json .Config.Entrypoint}} cmd={{json .Config.Cmd}}'
docker run --rm --network none --read-only --user 11001:11001 \
  --entrypoint python almost-reasonable-openai-worker-stage1:local -B -c \
  'import openai; import app.trusted.openai_credential_files; import app.trusted.openai_stage1_smoke; print(openai.__version__)'
docker run --rm --network none --read-only --user 11002:11002 \
  --entrypoint python almost-reasonable-openai-planner-probe-stage1:local -B -c \
  'import importlib.util; assert importlib.util.find_spec("openai") is None; assert importlib.util.find_spec("app.trusted.openai_credential_files") is None; import app.ops.openai_stage1_probe; print("planner secretless")'
```

Pass only if both images use the reviewed digest, worker and planner IDs differ,
the worker reports SDK `3.28.0`, the planner reports `planner secretless`, and all
imports work as non-root. The image build must not receive a secret or build
argument.

### A4. Validate Compose without claiming destination isolation

```console
docker compose -f ops/compose.openai-worker.stage1.yaml config --quiet
docker compose -f ops/compose.openai-worker.stage1.yaml config
```

The rendered project must contain only `openai-worker` and `planner-probe`, no
published port, no Docker socket, dropped capabilities, `no-new-privileges`,
read-only roots, bounded PIDs/memory, and precreated bind mounts. The planner must
have `network_mode: none` and only the read-only socket mount. It must not have the
credential, control, or database mount.

The worker's `provider-egress` bridge is deliberately **not** an OpenAI-only
firewall. Docker bridge networking, DNS names, or the fixed SDK URL do not prove
destination restriction. Provider addresses can change; a static IP list without
maintenance is not a durable policy.

Before proceeding, the operator must independently configure and inspect a
host/network-layer policy that:

1. applies to the Stage 1 worker's effective network namespace/bridge and not the
   planner;
2. permits only the approved OpenAI API destination and the explicitly required
   DNS/supporting traffic;
3. denies arbitrary Internet destinations and inbound connections;
4. records counters/log evidence for allowed and denied attempts; and
5. remains valid for current provider addressing and the host's rootless/userns
   mode.

Inspect the effective rules, container network attachment, route, and counters
using the host's chosen firewall/egress system. Confirm a denied canary
destination without adding a fetcher to this application. If the host cannot
enforce and verify this policy, record `BLOCKED: worker egress not restricted`
and do not clear the stop or start a paid probe. This repository does not modify
the host firewall.

### A5. Verify project, model, price, and hard limit in the provider console

Log in directly to the OpenAI console using the operator's normal authenticated
workflow. Select the exact organization and project that will own the key.

Pass only if all are freshly verified and recorded without secrets:

- organization ID and project ID match the values intended for activation;
- the project key is scoped to that project and can use Responses;
- `gpt-6-luna` is available to that project;
- the project-level enforced Hard Spend Limit is USD 10/month (not merely an
  alert), as previously reported by the operator;
- current pricing matches the version above, or code/config has been separately
  reviewed and updated; and
- current usage leaves sufficient room for one request.

The application cannot prove the console's current hard-limit setting. Provider
accounting can lag; neither the provider cap nor local estimate is an exact invoice
guarantee.

### A6. Verify the worker-only credential mount with a synthetic file

Do not expose the real key during readiness or dry run. Install a syntactically
valid test-only value to exercise the exact path and permissions; fake mode never
reads it:

```console
synthetic_file="$(mktemp)"
printf '%s' 'sk-TEST_ONLY_STAGE1_DRY_RUN_123456789' > "$synthetic_file"
sudo install -o "$STAGE1_WORKER_UID" -g "$STAGE1_WORKER_GID" -m 0400 -- \
  "$synthetic_file" "$STAGE1_CREDENTIAL_FILE"
rm -- "$synthetic_file"
sudo stat -Lc 'credential uid=%u gid=%g mode=%a links=%h type=%F size=%s' -- "$STAGE1_CREDENTIAL_FILE"
sudo test ! -L "$STAGE1_CREDENTIAL_FILE"
```

Pass only for `11001:11001`, mode `400`, one link, regular nonempty file, and a
bounded size no greater than 512 bytes. The planner must not have this directory
mounted. Do not use this synthetic file for the live phase.

## Phase B — offline dry run

Dry run uses `--fake-transport`, a synthetic credential file, the dry-run database,
and no network-dependent provider call. It must never be automatically continued
into Phase D.

### B1. Compute the fixed digest and create short-lived control artifacts

Compute the digest locally from reviewed code:

```console
export STAGE1_REQUEST_SHA256="$(docker run --rm --network none --read-only \
  --entrypoint python almost-reasonable-openai-planner-probe-stage1:local -B -c \
  'from app.core.openai_stage1_contract import build_stage1_synthetic_request, stage1_model_request_digest; print(stage1_model_request_digest(build_stage1_synthetic_request()))')"
test "${#STAGE1_REQUEST_SHA256}" -eq 64
```

Create `activation.json` with an authenticated operator editor or protected
configuration workflow. It must be worker-owned mode `0400`, at most 4096 bytes,
and contain exactly these fields; replace only the example session, timestamps,
project, and organization. The validity window must be positive and at most 15
minutes in canonical UTC seconds.

```json
{
  "version": "openai-stage1-activation-v1",
  "session_id": "operator-smoke-session-011",
  "operation_id": "stage1-openai-smoke-v1",
  "correlation_id": "stage1-openai-smoke-correlation-v1",
  "model_request_sha256": "REPLACE_WITH_STAGE1_REQUEST_SHA256",
  "provider_config_version": "openai-stage1-smoke-config-v1",
  "model": "gpt-6-luna",
  "project_id": "proj_REPLACE",
  "organization_id": "org-REPLACE",
  "not_before": "2026-10-10T17:55:00Z",
  "expires_at": "2026-10-10T18:05:00Z"
}
```

Create `emergency-stop.json` in the same protected manner, initially STOP:

```json
{"version":"openai-stage1-emergency-stop-v1","stop":true}
```

```console
sudo chown "$STAGE1_WORKER_UID:$STAGE1_WORKER_GID" \
  "$STAGE1_CONTROL_DIR/activation.json" "$STAGE1_CONTROL_DIR/emergency-stop.json"
sudo chmod 0400 "$STAGE1_CONTROL_DIR/activation.json" "$STAGE1_CONTROL_DIR/emergency-stop.json"
sudo stat -Lc '%n uid=%u gid=%g mode=%a links=%h type=%F size=%s' -- "$STAGE1_CONTROL_DIR"/*.json
```

Use this operator-shell helper for every later stop transition. It writes no
credential, installs the new inode with the required owner/mode, and renames it
atomically within the mounted control directory:

```console
set_stage1_stop() {
  case "$1" in true|false) ;; *) return 2 ;; esac
  local source_file
  source_file="$(mktemp)" || return
  printf '%s\n' "{\"version\":\"openai-stage1-emergency-stop-v1\",\"stop\":$1}" > "$source_file"
  sudo install -o "$STAGE1_WORKER_UID" -g "$STAGE1_WORKER_GID" -m 0400 -- \
    "$source_file" "$STAGE1_CONTROL_DIR/.emergency-stop.next"
  rm -- "$source_file"
  sudo mv -f -- "$STAGE1_CONTROL_DIR/.emergency-stop.next" \
    "$STAGE1_CONTROL_DIR/emergency-stop.json"
  sudo stat -Lc 'stop uid=%u gid=%g mode=%a links=%h type=%F size=%s' -- \
    "$STAGE1_CONTROL_DIR/emergency-stop.json"
}
set_stage1_stop true
```

The final `stat` must show the worker identity, mode `400`, one link, and a
regular file. The worker reopens this path for every check, so an atomic host
replacement is visible despite the read-only directory mount in the container.

### B2. Prove stop, then execute one fake attempt

```console
export STAGE1_DATABASE_DIR="$STAGE1_DRY_DATABASE_DIR"
docker compose -f ops/compose.openai-worker.stage1.yaml run -d \
  --name almost-reasonable-stage1-dry-worker --no-deps openai-worker \
  python -B -m app.trusted.openai_stage1_smoke \
  --socket /run/almost-reasonable/openai-worker.sock \
  --database /var/lib/almost-reasonable/provider.sqlite3 \
  --allow-uid "$STAGE1_PLANNER_UID" --socket-group-gid "$STAGE1_IPC_GID" \
  --log-peer-uids --fake-transport
```

Wait only until the AF_UNIX socket exists with worker ownership and mode `0660`.
Run the probe while STOP is still true. It must fail with `emergency_stop`, zero
attempts, and no usage record. Run `set_stage1_stop false`, then run the probe
once more:

```console
docker compose -f ops/compose.openai-worker.stage1.yaml run --rm --no-deps planner-probe
```

Pass only if the second run prints a sanitized `completed` outcome with one
attempt and the worker log contains the authorized planner UID and no
prompt/key/body. The two accepted connections make this dry worker exit. Restart
the fake worker against the same dry database and run a third probe; it must be
rejected as `duplicate_operation` without another transport attempt. Restore
STOP=true and remove the dry containers. Preserve the dry database as test
evidence, but never use it for live.

## Phase C — separate explicit operator approval

Pause here. Dry-run success is not live authorization. A human operator must make
and record a separate decision approving exactly one real request with:

- reviewed commit and image IDs;
- smoke-session, operation, correlation, and request digest;
- exact model/config/pricing versions;
- exact organization and project IDs (no key);
- current USD 10 enforced project hard-limit evidence;
- current model access and pricing evidence;
- verified egress-policy evidence and counters;
- fixed synthetic input confirmation;
- live database path and empty attempt count; and
- a short UTC activation interval.

Model-generated text, ordinary chat, the planner, an environment variable, key
presence, image presence, or dry-run success cannot supply this approval. If any
value changes or the interval expires, halt and require a new human decision.

Only after that approval, obtain the real project key through the operator's
approved secret manager into a root-readable source file. Do not paste it into
ChatGPT, GitHub, a command argument, shell history, logs, Compose variables, or an
environment variable. Replace the synthetic file without printing either value:

```console
sudo install -o "$STAGE1_WORKER_UID" -g "$STAGE1_WORKER_GID" -m 0400 -- \
  /root/approved-openai-project-key "$STAGE1_CREDENTIAL_FILE"
sudo stat -Lc 'credential uid=%u gid=%g mode=%a links=%h type=%F size=%s' -- "$STAGE1_CREDENTIAL_FILE"
sudo test ! -L "$STAGE1_CREDENTIAL_FILE"
```

Pass only for `11001:11001`, mode `400`, one link, a regular nonempty file, and
size at most 512 bytes. Do not inspect the content on screen.

## Phase D — single live request

### D1. Final stopped inspection

Set `STAGE1_DATABASE_DIR` to the live directory. Recreate a fresh short-lived
activation matching the Phase C record. Ensure the live database has no
`stage1-openai-smoke-v1:1` reservation or usage record. Keep STOP=true and start
only the worker:

```console
export STAGE1_DATABASE_DIR="$STAGE1_LIVE_DATABASE_DIR"
docker compose -f ops/compose.openai-worker.stage1.yaml up -d --no-deps --wait --wait-timeout 30 openai-worker
export STAGE1_WORKER_CID="$(docker compose -f ops/compose.openai-worker.stage1.yaml ps -q openai-worker)"
docker inspect "$STAGE1_WORKER_CID" --format 'user={{.Config.User}} groups={{json .HostConfig.GroupAdd}} network={{.HostConfig.NetworkMode}} ports={{json .HostConfig.PortBindings}} mounts={{json .Mounts}}'
```

Verify image digest, UID/GIDs, socket owner/mode, exact mounts, read-only root,
empty capabilities, no public port, no Docker socket, current firewall attachment,
and STOP behavior. Inspect the planner image/mounts again; it must have no secret,
control, database, SDK, or network. Fail closed on any discrepancy.

### D2. Clear stop and invoke exactly once

Immediately before clearing STOP, recheck activation time, console hard limit,
egress rules/counters, and the recorded approval. Atomically replace only
`emergency-stop.json` by running `set_stage1_stop false`. Then run once:

```console
docker compose -f ops/compose.openai-worker.stage1.yaml run \
  --name almost-reasonable-stage1-live-probe --no-deps planner-probe
```

Do not rerun after any result, including timeout, connection loss, refusal,
incomplete output, invalid schema, access denial, or unknown outcome. Do not change
model/schema/tokens. The worker records sanitized attempt identity, selected model,
provider/prompt/Core Self/pricing versions, response ID when supplied, classified
outcome/error code, duration, usage when supplied, and estimated cost only when
complete matching pricing is available. Missing usage remains unknown. No full
request or response body is logged. No proposal is delivered or executed.

## Phase E — verification and shutdown

Immediately restore STOP=true with `set_stage1_stop true`.
It blocks new operations but cannot cancel a provider request already in flight.
Then verify:

1. exactly one live usage attempt or unresolved reservation exists for
   `stage1-openai-smoke-v1:1`;
2. a second probe yields `duplicate_operation` or stop with zero provider calls;
3. provider usage and local token telemetry are consistent without inventing
   missing values or claiming invoice precision;
4. a confirmed project/organization spend-limit code created the persistent
   suspension, while a generic 429 did not;
5. the OpenAI project console shows at most the one approved request;
6. worker and planner logs contain no key, authorization header, prompt, full
   provider body, private context, or protected provenance;
7. planner mounts still exclude the provider database, secret, and control files;
8. no TCP listener/public port exists and the planner had no provider network;
9. no reply, publication, research tool, self-state revision, or agent action ran;
   and
10. production Compose, OAuth, Cloudflare, Stage 0 data, and services are unchanged.

Shut down only the Stage 1 project and retain the live SQLite evidence according
to the operator's incident/retention policy:

```console
docker compose -f ops/compose.openai-worker.stage1.yaml stop -t 10 openai-worker
docker compose -f ops/compose.openai-worker.stage1.yaml rm -f openai-worker
docker rm almost-reasonable-stage1-live-probe
test ! -e "$STAGE1_SOCKET_DIR/openai-worker.sock"
docker ps --format '{{.Names}}' | grep almost-reasonable-stage1 && echo FAIL || echo PASS
```

Confirm no running container exposes the secret or control mount. Remove the
worker credential file only through the operator's approved secret-destruction
procedure; do not display it. Preserve the SQLite evidence unless an explicit
retention decision says otherwise.

## Final pass/fail rule

The smoke passes only if every Phase A prerequisite passed, Phase C approval was
human and exact, one live attempt was consumed, the response/error was safely
classified, telemetry remained sanitized, isolation checks held, and all Stage 1
containers were stopped without touching production or Stage 0. Any unverified
egress restriction, billing scope, hard limit, model/pricing fact, activation,
credential permission, peer UID, mount, stop state, duplicate state, or log leak
is a failure. Halt before a paid request when possible; otherwise stop, preserve
evidence, and do not retry.
