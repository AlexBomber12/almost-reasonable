# INT-22 bounded private conversation pilot

This runbook describes an **offline rehearsal** and a later, separately approved
synthetic paid pilot. No provider credential was loaded, provider request sent,
service deployed, or participant enrolled while implementing INT-22. Ordinary
`app.private_web.cli serve` still constructs disabled inference. The fixed Stage 1
smoke authorization, artifact, socket and operation remain separate and unchanged.
INT-22 does not authorize production participants or public effects.

For the optional reusable public-research connection on the ordinary deployment,
see [B05D research deployment](b05d-research-deployment.md). It preserves the
effective protected overrides, historical allowance, shared worker usage and
owner emergency Stop. It does not authorize deployment or a paid check.

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
file retains its existing loopback publication. The retired separate acceptance
network configuration is no longer provided. The worker alone has a provider-egress network. Confirm host firewall, DNS, network
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

## Retired separate live acceptance environment

The separate synthetic deployment and its orchestration have been retired.
The operator removed its containers, networks, SSH tunnel, nginx configuration,
synthetic state, copied credentials, hosts entry and acceptance worktrees.
Do not recreate them. Future browser checks use the ordinary application and
existing domain. This retirement does not deploy or change that application.

The product remains one persistent, evolving AI agent with private conversations,
durable memory, independent research, beliefs and goals, and Threads interaction.
The offline CI entry points (`acceptance.run` and `acceptance.plugin`), scenario
traceability, fake-provider rehearsals and functional regressions remain active.
See [B03D/B03E evidence and remaining validation](b03e-combined-acceptance.md).
