# INT-22 bounded private conversation pilot

This runbook describes an **offline rehearsal** and a later, separately approved
synthetic paid pilot. No provider credential was loaded, provider request sent,
service deployed, or participant enrolled while implementing INT-22. Ordinary
`app.private_web.cli serve` still constructs disabled inference. The fixed Stage 1
smoke authorization, artifact, socket and operation remain separate and unchanged.
INT-22 does not authorize production participants or public effects.

## Contract and limits

The application uses the explicit INT-19 `--worker-socket` composition. It
authenticates Host, Origin, CSRF and session, checks current participant privacy,
owner Stop and application spend state, builds the typed private-memory v2 or
shared-intelligence v3 context, then checks the worker UID before sending private
bytes. The worker checks the application UID with `SO_PEERCRED`, accepts only the
typed context protocol, and reserves the reply operation in its own allowance
database before the trusted gateway or SDK adapter can contact a provider.

The worker fixes `gpt-6-luna`, provider config `openai-private-pilot-v1`, prompt
`agent-proposal-v2`, 512 maximum output tokens, 30-second attempt and 40-second
overall timeouts, one attempt per logical operation, zero SDK retries, a strict
JSON proposal schema, `tools=[]`, `store=false`, `background=false`,
`stream=false`, and `truncation=disabled`. The credential loader reads only the
worker-owned `/run/secrets/openai-api-key` file. Project and organization come
from the protected activation artifact; no IPC field or HTTP caller can select
the provider, SDK parameters, tools, billing scope, or storage behavior. The
gateway still owns provider attempt accounting and confirmed spend suspension.
Cost estimates are not treated as a spend enforcement mechanism.

The artifact must be worker-owned `0400` in a worker-owned `0700` directory. Every
directory from the filesystem root through that parent must be a real directory
owned by root or the worker UID, with no group or world write bit. The sole
exception is a root-owned `01777` sticky directory such as `/tmp`; the immediate
parent still must be worker-owned `0700`. The same ancestry rule applies to the
emergency-stop file, allowance database, and provider state database. Symlink
components and unsafe ownership or permissions fail closed. The worker checks
the control paths on each authorization and pins the directory and file identity
of both databases across the running worker's transactions. A moved ancestor or
replacement database blocks further provider contact. This protects against
untrusted path manipulation; it does not prevent a privileged operator who can
restore all trusted state from doing so.

The artifact
has exact fields:

```json
{
  "version": "private-pilot-activation-v1",
  "pilot_id": "approved-synthetic-pilot-identifier",
  "application_peer_uid": 11022,
  "provider_config_version": "openai-private-pilot-v1",
  "model": "gpt-6-luna",
  "project_id": "proj_APPROVED_SCOPE",
  "organization_id": "org-APPROVED_SCOPE",
  "not_before": "YYYY-MM-DDTHH:MM:SSZ",
  "expires_at": "YYYY-MM-DDTHH:MM:SSZ",
  "max_operations": 5,
  "max_attempts_per_operation": 1
}
```

Replace placeholders only in a separately reviewed private operator artifact.
The validity interval must be positive and at most 600 seconds; the worker
requires the current UTC instant inside it. A shorter interval and fewer than
five operations are permitted. These are temporary validation limits, not
behavioral productivity targets. The allowance database pins the exact artifact
bytes by SHA-256 and stores each `reply:<sha256>` operation under an immediate
SQLite transaction with full synchronous commit. A duplicate, exhausted
allowance, changed artifact, missing/unsafe database or startup restart cannot
rearm it. Loss of a response or an uncertain gateway outcome also consumes the
reservation. Do not replace the allowance database or artifact to manufacture a
new pilot; a new pilot needs a separately reviewed change and approval.

The worker emergency-stop file is independently protected and defaults to
**stopped** on absence, malformed JSON, duplicate fields, unsafe permissions,
or an explicit `true`. Its only open value is:

```json
{"version":"private-pilot-emergency-stop-v1","stop":false}
```

The application OwnerStore Stop and application spend suspension remain
independent of this worker stop and worker provider spend suspension. Clearing
one does not clear another. In-flight responses are still subject to the INT-19
session, privacy, source, owner Stop, spend, Russian, and release checks.

## A. Prepare without activating

Use a reviewed checkout and inspect its exact revision. Do not use real
participant data. The following paths and IDs are examples; resolve collisions
and verify host ownership before use. The Docker Compose file requires absolute
precreated bind directories and never creates them for you.

```sh
git status --short --branch
git rev-parse HEAD
export PILOT_WORKER_UID=11021 PILOT_WORKER_GID=11021
export PILOT_APP_UID=11022 PILOT_APP_GID=11022 PILOT_IPC_GID=11020
export PILOT_SOCKET_DIR=/srv/almost-reasonable/int22/ipc
export PILOT_WORKER_STATE_DIR=/srv/almost-reasonable/int22/worker
export PILOT_APP_STATE_DIR=/srv/almost-reasonable/int22/app
export PILOT_CONTROL_DIR=/srv/almost-reasonable/int22/control
export PILOT_SECRET_DIR=/srv/almost-reasonable/int22/secrets
export PILOT_ALLOWED_HOST=chat.example.invalid
export PILOT_ALLOWED_ORIGIN=https://chat.example.invalid
sudo install -d -o 11021 -g 11020 -m 0710 "$PILOT_SOCKET_DIR"
sudo install -d -o 11021 -g 11021 -m 0700 "$PILOT_WORKER_STATE_DIR" "$PILOT_CONTROL_DIR" "$PILOT_SECRET_DIR"
sudo install -d -o 11022 -g 11022 -m 0700 "$PILOT_APP_STATE_DIR"
docker compose -f ops/compose.private-pilot.yaml config --quiet
docker compose -f ops/compose.private-pilot.yaml build
```

Build uses pinned base and SDK dependency versions. The app container has no
provider credential mount, no worker provider/allowance database mount and no
SDK dependency. The worker has no application store mount. Only the AF_UNIX
socket directory is shared. The app's network is internal. The base Compose
file retains a loopback publication for the existing pilot, but that port is
not reachable on the intended AI-Server host. The acceptance override removes
it and assigns a fixed backend IP for the existing SSH-forward path. The
worker alone has a provider-egress network. Confirm host firewall, DNS, network
namespace, proxy, UID/GID and mount behavior on the target host; Compose text is
configuration evidence, not proof of deployed isolation.

Create **empty** stores under the application UID and an empty worker provider
store under the worker UID. These commands do not provision accounts, clear
Owner Stop, reset spend suspension, create activation, or read credentials:

```sh
docker compose -f ops/compose.private-pilot.yaml run --rm --no-deps private-app python -B -c 'from app.private_web.auth import AuthStore; from app.core.store import EventStore; from app.privacy.ledger import PrivacyLedger; from app.owner_control.store import OwnerStore; p="/var/lib/pilot-app/"; AuthStore(p+"auth.db"); EventStore(p+"events.db"); PrivacyLedger(p+"ledger.db"); EventStore(p+"ipc-state.db"); OwnerStore(p+"owner")'
docker compose -f ops/compose.private-pilot.yaml run --rm --no-deps provider-worker python -B -c 'from pathlib import Path; from app.core.store import EventStore; EventStore(Path("/var/lib/pilot-worker/provider.sqlite3"))'
```

If any store already exists, preserve it and inspect its current controls;
never delete it to rearm an operation or clear suspension. The application
stores and worker stores have different service identities and permissions.

## B. Run the offline rehearsal

From the reviewed checkout, without a credential or live container:

```sh
python -m pytest -q tests/test_int22_pilot.py tests/test_int22_controls.py tests/test_int22_rehearsal.py
scripts/ci.sh
```

`test_authenticated_http_to_fake_sdk_reply_history_restart_and_quota` uses
synthetic A/B accounts, actual authenticated WSGI, a separate worker process,
the production `build_worker` composition with only its credential factory
replaced by a fake SDK object, the real Responses adapter, trusted gateway,
provider database, durable allowance, Russian reply release, and history.
It verifies closed SDK options, A/B input separation, duplicate HTTP replay,
worker restart, and a sixth operation denied after five. A separate process-exit
test loses the worker response during the fake SDK call and verifies the
uncertain HTTP operation and consumed allowance after restart. Other INT-22
tests exercise concurrent allowance reservations,
expiry, malformed and changed artifacts, wrong application peer UID, worker
stop/spend readiness, malformed SDK output, and in-flight session revocation,
source deletion, application Stop and application spend suspension.
`test_int22_paths.py` covers unsafe and symlinked ancestors, a redirected old
allowance snapshot, protected-path changes after admission, and accepted sticky
temporary-directory ancestry.

These tests execute process separation, AF_UNIX peer checks under the test UID,
typed IPC, closed SDK request construction, SQLite consumption and private
reply/history behavior. They do **not** execute distinct host UIDs, Docker
mount permissions, provider egress restrictions, actual SDK credentials, paid
model behavior, real billing limits, fluent-human quality calibration or a
deployed HTTPS reverse proxy. Verify those on the target host before any
separately approved live run.

## C. Separately authorize a synthetic paid run

Stop here until the human operator gives explicit approval for one identified
synthetic pilot, the exact reviewed commit, project and organization billing
scope, validity window, maximum operations, named synthetic accounts, budget,
host and shutdown owner. Recheck **current official** [Responses API create
contract](https://developers.openai.com/api/reference/resources/responses/methods/create),
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses),
[model access and capabilities](https://developers.openai.com/api/docs/models/gpt-6-luna),
[pricing](https://developers.openai.com/api/docs/pricing) and
[billing/error behavior](https://developers.openai.com/api/docs/guides/error-codes)
immediately before paying. Verify this exact project can use the model and
schema, actual billing limits and remaining budget, processor/privacy terms,
host egress and current-HEAD CI/review. A failure requires a new review; do not
switch models, relax schema, increase retries or renew allowance in place.

After approval, place the reviewed activation JSON in the protected control
directory and the key in the protected secret directory as worker-owned `0400`
regular files with no symlinks or extra hard links. Never put the key in an
environment variable, Compose file, shell history, PR, log or command argument.
Use a protected source file and `sudo install`, then remove that staging copy
through the approved secret-handling procedure. Keep the worker stop set to
`true` during preparation. Prepare the allowance **once**:

```sh
sudo install -o 11021 -g 11021 -m 0400 /protected/approved-activation.json "$PILOT_CONTROL_DIR/activation.json"
sudo install -o 11021 -g 11021 -m 0400 /protected/openai-api-key "$PILOT_SECRET_DIR/openai-api-key"
docker compose -f ops/compose.private-pilot.yaml run --rm --no-deps provider-worker python -B -m app.ops.private_pilot_ops prepare --activation /run/almost-reasonable-private-pilot/control/activation.json --allowance /var/lib/pilot-worker/allowance.sqlite3
```

Use the existing authenticated owner control to inspect and, only under this
separate approval, clear the application Owner Stop. Provision only the named
synthetic accounts through the existing private-web provisioning command; do
not create real participants. The commands below prompt for passwords rather
than putting them in arguments or environment variables. Replace the example
names with the approved synthetic identities. The application spend state and
worker provider spend state must independently show unsuspended. A previously
suspended state is a blocker, not an invitation to reset a database.

```sh
docker compose -f ops/compose.private-pilot.yaml run --rm --no-deps -i private-app python -B -m app.owner_control --directory /var/lib/pilot-app/owner --username approved-operator
docker compose -f ops/compose.private-pilot.yaml run --rm --no-deps -i private-app python -B -m app.private_web.cli provision --auth-db /var/lib/pilot-app/auth.db --username synthetic-a --participant-id synthetic-participant-a
docker compose -f ops/compose.private-pilot.yaml run --rm --no-deps -i private-app python -B -c 'from getpass import getpass; from app.owner_control.store import OwnerStore; from app.private_web.auth import LoginStatus; s=OwnerStore("/var/lib/pilot-app/owner"); r=s.login("approved-operator",getpass("Owner password: "),"127.0.0.1"); assert r.status is LoginStatus.SUCCESS; print(s.change(r.session_token,s.issue(r.session_token),False))'
docker compose -f ops/compose.private-pilot.yaml run --rm --no-deps private-app python -B -c 'from app.core.store import EventStore; from app.core.provider_spend import ProviderStateStore; from app.owner_control.store import OwnerStore; p="/var/lib/pilot-app/"; print("owner_stop",OwnerStore(p+"owner").active(),"application_spend_suspended",ProviderStateStore(EventStore(p+"ipc-state.db")).is_spend_suspended())'
```

The owner login command must print `applied`, and inspection must show
`owner_stop False` and `application_spend_suspended False`. Provision every
additional approved synthetic account separately. Write the exact protected
worker stop JSON with `stop=false` only at the live execution step, then run
the read-only worker readiness inspection:

```sh
sudo sh -c 'printf %s "{\"version\":\"private-pilot-emergency-stop-v1\",\"stop\":false}" > "$1"; chown 11021:11021 "$1"; chmod 0400 "$1"' sh "$PILOT_CONTROL_DIR/emergency-stop.json"
```

```sh
docker compose -f ops/compose.private-pilot.yaml run --rm --no-deps provider-worker python -B -m app.ops.private_pilot_ops inspect --activation /run/almost-reasonable-private-pilot/control/activation.json --allowance /var/lib/pilot-worker/allowance.sqlite3 --stop /run/almost-reasonable-private-pilot/control/emergency-stop.json --provider-database /var/lib/pilot-worker/provider.sqlite3
```

`ready=true` covers current worker activation, remaining allowance, worker
Stop and worker spend state. It does not attest application Owner Stop,
application spend, privacy reconciliation, account scope, model access or host
firewall. Inspect those separately and retain the exact results in private
operator records. Startup fails on missing/expired/malformed activation or
missing/changed allowance. No ordinary startup flag can bypass these checks.

Only after all checks and the approval are recorded, start the worker and app:

```sh
docker compose -f ops/compose.private-pilot.yaml up -d provider-worker private-app
docker compose -f ops/compose.private-pilot.yaml ps
```

Use the approved HTTPS front end and only the approved synthetic sessions.
Submit at most five distinct messages before expiry. Inspect the application
Owner Stop/status, application IPC state, worker readiness/remaining allowance,
provider attempt records and private reply/history under their respective
service identities. Do not expose private content, credentials or token values
in inspection logs. A timeout, malformed result, lost response or uncertain
outcome consumes allowance and must not be retried as a fresh operation.
If the worker exits abruptly, first confirm that no worker process remains.
Inspect the old socket's type, owner, group and mode; remove only that stale
socket before an authorized restart. Preserve both worker databases and the
activation file. A restart never extends the original window or allowance.

## D. Shutdown and preserve evidence

Set worker Stop to `true` with the protected exact JSON, set the independent
application Owner Stop through its authenticated control, and stop both
containers. Preserve application and worker databases, the activation digest,
operation/attempt counts, sanitized outcomes, CI/review evidence and host
configuration for review. Do not remove a database to restart or extend this
pilot.

```sh
sudo sh -c 'printf %s "{\"version\":\"private-pilot-emergency-stop-v1\",\"stop\":true}" > "$1"; chown 11021:11021 "$1"; chmod 0400 "$1"' sh "$PILOT_CONTROL_DIR/emergency-stop.json"
docker compose -f ops/compose.private-pilot.yaml run --rm --no-deps -i private-app python -B -c 'from getpass import getpass; from app.owner_control.store import OwnerStore; from app.private_web.auth import LoginStatus; s=OwnerStore("/var/lib/pilot-app/owner"); r=s.login("approved-operator",getpass("Owner password: "),"127.0.0.1"); assert r.status is LoginStatus.SUCCESS; print(s.change(r.session_token,s.issue(r.session_token),True))'
docker compose -f ops/compose.private-pilot.yaml stop private-app provider-worker
docker compose -f ops/compose.private-pilot.yaml ps
```

The participant-facing identity and disclosure policy is unchanged. The
screened Russian reply is an untrusted model proposal until the existing
application policy and release path approve it; owner opinions are not
privileged evidence. H/M semantic quality, human calibration, legal/privacy
review, real processor behavior, production enrollment, deployment hardening
and later-stage public activity remain release blockers.

## Deferred one-command synthetic acceptance

The repository includes an operator runner for the ship-name, restart and Mars
journey. It is **not authorized for paid execution by this documentation or by
CI**. On the approved host, first install a root-owned `0400` authorization JSON
and activation JSON outside the checkout. The authorization must name the exact
reviewed commit, task `synthetic-private-pilot-ship-restart-mars`, model
`gpt-6-luna`, project and organization billing scope, UTC `valid_from` and
`valid_until`, positive `billing_budget_usd`, `max_aggregate_operations`,
`max_aggregate_input_tokens`, `max_aggregate_output_tokens`, and
`max_requests_per_run` (at least three). Its project, organization and model
must match the protected activation. The activation must have at least three
operations and one attempt per operation. Its entire window must fit within the
authorization validity period; the runner rechecks approval before each
message. A separate explicit human approval
must cover these values, the synthetic account, host and execution window.

Before invoking the command, stage the worker-owned `0400` credential at
`STATE_ROOT/secrets/openai-api-key` through the approved secret procedure and
precreate `STATE_ROOT` as a root-owned `0700` directory beneath protected
ancestors. The runner also requires root-owned `0700` `runs` directories and
rejects symlinked state or service directories before changing their ownership.
The worker-owned `secrets`, `worker`, and `control` entries remain inside that
root-owned state root. The state root's parent must already exist. Keep
`STATE_ROOT` and `AGGREGATE_LEDGER` outside the working server state;
retain both across run IDs and commits. The command prompts for synthetic owner
and participant passwords before opening the activation window. The activation
window can start up to ten minutes after preparation begins; provide enough
time for image build and state initialization. Image builds have a separate
ten-minute timeout.

Place authorization, activation, credential, control, aggregate-ledger, and report
files beneath directories owned by root or the file's service owner, without
group or world write permission. The runner verifies each parent directory
and opens authorization, activation, and the report through checked descriptors.

### AI-Server: prepare the acceptance backend

Choose a private subnet and container address unused by all Docker networks
and host routes. The values below are examples; replace them if the preflight
finds a conflict. The acceptance override uses a fresh Compose project per
run, an internal app network, and a fixed container address. It publishes no
host port and does not attach the app to provider egress. Keep the existing
pilot's project, network, container IP, state, and services untouched.

```sh
cd /path/to/reviewed/almost-reasonable
export ACCEPTANCE_SUBNET=172.30.252.0/29 ACCEPTANCE_IP=172.30.252.3
ip -4 route show
docker network ls
scripts/probe_private_pilot_backend.sh "$ACCEPTANCE_SUBNET" "$ACCEPTANCE_IP"
```

The probe checks Docker and host-route conflicts before creating anything,
starts a temporary HTTP server on a temporary internal network at the chosen
address, checks HTTP from inside that network and from the AI-Server host,
restarts that container, checks its fixed address and HTTP again, then removes
only its own container and network. It uses the locally built app image and no
provider credential. Do not continue if the host HTTP check fails. Recheck
network conflicts immediately before the runner; inspect any old acceptance
network and have its owner remove it before choosing the same subnet again.

### SERVER: prepare the existing HTTPS and SSH path

Use a dedicated acceptance HTTPS hostname and a free SERVER loopback port.
Check the port before starting the forward; if it is occupied, identify the
owner instead of replacing the working pilot's forward. Configure the existing
nginx HTTPS server block for that hostname to proxy to
`http://127.0.0.1:18032` with the original Host and HTTPS scheme headers.
For example, its location can contain `proxy_pass http://127.0.0.1:18032;`,
`proxy_set_header Host $host;`, and
`proxy_set_header X-Forwarded-Proto https;`. Keep its existing TLS configuration.
In a supervised SERVER session, establish the forward before starting the
runner on AI-Server:

```sh
ss -H -ltnp '( sport = :18032 )'  # must be empty before creating this forward
ssh -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:18032:172.30.252.3:8000 operator@192.168.50.4
```

The target IP is fixed, so this same forward survives the application's
restart between messages. The AI-Server runner starts the exact acceptance
app while both owner and worker Stops remain enabled, then requires an
authenticated `GET /session` through the approved HTTPS origin to return its
new per-run account name. Wrong routing or a failed tunnel stops the run before
aggregate reservation, worker allowance preparation, or either Stop is opened.
The exact app route cannot be tested until the app starts; the runner performs
that test in its stopped readiness phase, before the paid window. From SERVER,
`curl -fsS https://approved-synthetic-host.example/login` can independently
confirm the HTTPS path at that point. The runner also checks the authenticated
route after the application-only restart. A route failure is recorded as a
bounded `backend_error` in the protected report; subnet conflicts are printed
by the runner before it creates acceptance state or networks.

Worker readiness is a bounded, credential-free connection to
`/run/pilot-ipc/worker.sock` from the running `private-app` container, using
its configured application user and IPC group. The host's longer bind-mount
pathname is not a Unix socket address for this check. The runner prepares
stores with a temporary app container before startup, uses `compose exec -T`
for owner changes and event lookup while the fixed-IP app is running, then
inspects owner state with a temporary app container only after shutdown. A
worker-readiness failure keeps paid execution closed and still runs both Stop
actions and container cleanup. Preserve the protected report and inspect the
acceptance project's service state and logs; post-cleanup exit codes alone do
not establish that the worker crashed.

### AI-Server: run only after separate paid authorization

The one command is:

```sh
sudo python -m acceptance.private_pilot_live \
  --authorization /protected/ship-acceptance-authorization.json \
  --activation /protected/ship-activation.json \
  --state-root /srv/almost-reasonable/synthetic-ship-acceptance \
  --aggregate-ledger /srv/almost-reasonable/synthetic-ship-aggregate.sqlite3 \
  --origin https://approved-synthetic-host.example \
  --backend-subnet "$ACCEPTANCE_SUBNET" \
  --backend-ip "$ACCEPTANCE_IP" \
  --report /protected/ship-acceptance-report.json
```

The runner prints only fixed phase names (`passwords`, `preparation`,
`readiness`, `inference`, `restart`, `cleanup`) and builds the pinned app and
worker images, creates a fresh synthetic
application state, preserves worker state and allowance, provisions only the
synthetic account, waits for the activation start, reserves three aggregate
operation slots, and runs all three messages through authenticated HTTPS. It
restarts only `private-app` between the recall and Mars turns. It stops the
worker and application, sets worker and owner Stop, checks running container
state, and writes a protected compact report with commit, image IDs, captured
synthetic name, completed steps, provider attempts, token usage, safe rejection
categories and remaining allowance. The fake-provider integration additionally
inspects the actual serialized provider request after restart, including roles,
trust labels and the earlier released assistant reply. The destination answers
must match the requested single-line `Корабль: <имя>; Назначение: <планета>`
format exactly; merely mentioning Mars in a denial does not pass.
After the run, stop only the dedicated acceptance SSH forward on SERVER and
inspect the runner's `compose_project` before removing acceptance-owned
containers or its network. Preserve the acceptance worker state and aggregate
ledger for accounting review; leave the existing working pilot untouched.

The aggregate ledger reserves all three operations before contact. Failed or
interrupted runs keep that reservation; the narrow zero-call continuation below
can use the original three slots once without creating more slots. The worker's separate SQLite allowance
also consumes an operation before provider contact. A changed authorization
policy, including a new commit, cannot reset either ledger: it requires review
of the preserved state and a separately approved continuation. The runner
reconciles cumulative provider usage and worker allowance before every request
and after each response. It stops before another request if either observed
aggregate token limit is reached or usage is unavailable. Success requires
exactly three completed, correlated current-run attempts without retries and
three matching allowance consumptions. These checks cannot interrupt an
in-flight provider request. The worker enforces 512 output tokens per request,
the existing serialized input byte bound, one attempt and the 30-second attempt
and 40-second overall timeouts. The dollar amount is an authorization ceiling
for the operator and is **not** a guaranteed dollar cutoff: provider billing
can differ from local estimates, and no exact dollar cutoff is enforced before
each request. If provider pricing is unavailable, the report records a null
estimated total instead of zero. Missing provider token usage fails acceptance;
an in-flight call
can still exceed an observed aggregate token limit before reconciliation.
Confirm project-level billing controls separately before approval.

On an ordinary Ctrl+C, the runner writes an early sanitized `interrupted`
report, ignores further SIGINT during cleanup, attempts each Stop and container
action, and writes the final protected report. Each cleanup subprocess has a
15-second timeout. This is best effort, not a wall-clock guarantee: SIGKILL,
SIGTERM, host loss, filesystem stalls, Docker daemon failure, and a failed Stop
or report write can still leave uncertain state. Inspect both Stops, containers,
the report, and preserved ledgers before any further run. Live-model acceptance
remains pending until a separately approved execution succeeds.

### AI-Server: one explicit zero-call continuation

Use this procedure only for an interrupted acceptance run whose aggregate
ledger contains exactly one three-slot run and whose worker has **zero provider
attempts, zero provider reservations, and zero consumed allowance**. Keep the
original authorization, activation, run directory, app and worker databases,
aggregate ledger, and any original report. Do not rerun the preparation block,
overwrite the original authorization, delete state, edit SQLite, or extend the
window. The command refuses an open worker or Owner Stop, running or missing
acceptance containers, changed activation/allowance binding, extra run history,
a contradictory old report, or any uncertain database state. A missing old
report is allowed only with the other verified zero-call evidence. Any attempted
or uncertain paid operation needs separate human review instead.

Read the preserved run ID without changing the ledger:

```sh
sudo python - <<'PY'
import sqlite3
path = '/srv/almost-reasonable/synthetic-ship-aggregate.sqlite3'
with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
    print(db.execute('SELECT run_id, slots FROM runs').fetchall())
PY
```

The original authorization and activation windows must still be live. If either
has expired, stop; this procedure never renews them. If the reviewed checkout
commit differs from the commit named by the original authorization, the human
operator must explicitly review and approve that exact new full SHA for this
zero-call continuation under the **same** three-call, one-attempt, 512-output-
token-per-call, 6000-input-token, 1536-output-token, and $0.10 limits. Setting
`REVIEWED_SHA` below records that explicit commit choice; it does not add paid
capacity. Do not use this command without that review.

Choose an unused acceptance subnet/IP and run the credential-free AI-Server
backend probe above. An old stopped Compose project may still own its network,
so choose a different unused subnet instead of deleting old state. On SERVER,
point only the dedicated acceptance SSH forward at the new fixed IP, verify its
loopback port is free, and keep the existing nginx HTTPS origin. Establish and
verify this route before the paid window. Then on AI-Server, from the clean
reviewed checkout, run **once** with the preserved files and a new report path:

```sh
export INTERRUPTED_RUN_ID=<32-character-run-id-from-ledger>
export REVIEWED_SHA=<human-reviewed-full-current-commit-sha>
sudo python -m acceptance.private_pilot_live \
  --authorization /protected/ship-acceptance-authorization.json \
  --activation /protected/ship-activation.json \
  --state-root /srv/almost-reasonable/synthetic-ship-acceptance \
  --aggregate-ledger /srv/almost-reasonable/synthetic-ship-aggregate.sqlite3 \
  --origin https://approved-synthetic-host.example \
  --backend-subnet "$ACCEPTANCE_SUBNET" \
  --backend-ip "$ACCEPTANCE_IP" \
  --report /protected/ship-acceptance-continuation-report.json \
  --continue-zero-call "$INTERRUPTED_RUN_ID" \
  --previous-report /protected/ship-acceptance-report.json \
  --reviewed-commit "$REVIEWED_SHA"
```

The runner validates the original authorization and activation, the explicit
current SHA, both Stops, stopped old containers, preserved original run and
aggregate records, and zero worker use before creating a new run. It makes an
atomic, one-use `zero_call_continuations` receipt in the original aggregate
ledger; the new protected report also records that receipt. The original
three-slot `runs` row remains unchanged; no new aggregate `runs` row or paid-call
capacity is created. If this continuation fails or is interrupted, it cannot be retried
under the same receipt; preserve the evidence for separate review. Stop only
acceptance-owned containers and the dedicated acceptance SSH forward after the
run. Leave the working pilot untouched.
