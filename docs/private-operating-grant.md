# Ordinary private operating grants

## Design recorded before implementation

Base: `85d3ec906e26b354b428415710ac826a673801cf`. Traced private_pilot,
private_pilot_worker, private_pilot_ops, private_web CLI/runtime, all additional
operation permissions, private_research_config, INT22/23, B03D/E, narrative and
B05D/E. Reuse the worker, protected files, immediate SQLite allowance
transactions, provider ledger, IPC and application stores. Research's existing
AuthorityJournal demonstrates durable retired grants and globally unique
reservations; it remains separate because its scope and quotas differ.

Add `private-operating-grant-v1` with the exact ordinary pilot fields: finite
operator-selected UTC interval and positive SQLite-sized operation quota, no
acceptance task. Existing activation versions and opt-in checks retain their
limits. Add `private-narrative-permission-v3` with a positive finite shared
additional-operation limit bounded by the operating grant. Evolution and typed
permissions already support finite quotas and need no new format.

Extend the selected allowance only during explicit preparation. Keep original
`authority`/`consumed` rows untouched; ordinary grant history and consumption
live alongside them. Replacement explicitly retires the previous ordinary grant,
never deletes history, and checks new operations against both legacy and ordinary
consumption. Old narrative holds remain consumed and cannot be claimed by a new
grant. Startup never installs or selects authority. A durable per-grant maximum
observed UTC instant rejects rollback; observed expiry is irreversible. This is
bounded authority under the trusted host clock, not a defense against privileged
restoration of all protected state or an expiry never observed during downtime.

Planning reads the candidate artifact only; inspection reads existing state even
when expired/exhausted. Preparation is separate and never clears Stop or creates
provider/application stores. Keep all four existing Compose inputs and historical
allowance selection by default. Any replacement uses an explicit reviewed
selection in that same ledger. No deployment or live call is part of this work.
Ordinary responses also recheck authority/Stop immediately before worker IPC
release, including replies without narrative context; existing per-family commit
guards retain their ordering. Provider startup requires the existing initialized
schema before any store constructor, matching the research startup rule.

Tests first: extend connected offline real-IPC fixtures with a controllable clock,
then test multi-hour memory/narrative/research/restart/recall, exact counts,
replacement/history, concurrent admission, uncertain holds, expiry/clock changes,
privacy and Stop. Full local CI and current-HEAD remote review remain required.

## Artifact and permission formats

Use the exact JSON fields in [INT22](int22-private-pilot.md#contract-and-limits),
changing `version` to `private-operating-grant-v1`. Choose a new `pilot_id`, exact
approved peer UID/project/organization, `not_before`, `expires_at` and
`max_operations`. No `task` field is accepted. UTC timestamps use
`YYYY-MM-DDTHH:MM:SSZ`, start must precede end, and the quota must be an integer
from 1 through 9223372036854775807 (SQLite's representational limit, not a
recommended operating quota). There is no 600/3600-second acceptance ceiling.
The fixed provider config/model, request bounds and one-attempt policy remain
unchanged. This grants private inference only, with no research, sharing or
publication authority. Existing participant consent and privacy gates still apply.
No grant is shipped or enabled by default.

Evolution and typed memory retain their existing permission formats, matching
`pilot_id` and positive `max_operations` no greater than the grant quota. Repeated
narrative requires this new closed format in both the app-readable protected file
and worker-owned protected file:

```json
{"version":"private-narrative-permission-v3","pilot_id":"OPERATOR_SELECTED_ID","max_operations":100}
```

The number is an example, not approval. The worker bounds it by the current
grant; it caps **all additional operations together**, including evolution,
typed memory, extraction, narrative and shared proposals. The application checks
the version, ID, positive integer, protected file and existing release checks;
the worker enforces the durable count. Legacy narrative v1 stays exactly one,
v2 stays exactly 20 and is still selected only by the B03E worker activation.
Shared proposals remain incompatible with narrative in the application.

## Credential-free planning and explicit preparation

Run as the existing worker UID, with trusted directories and protected candidate
artifact, without mounting credentials. These commands never contact providers:

```sh
python -B -m app.ops.private_pilot_ops plan --activation /protected/control/grant-NEW.json
python -B -m app.ops.private_pilot_ops inspect \
  --activation /protected/control/activation-OLD.json \
  --allowance /var/lib/pilot-worker/HISTORICALLY_SELECTED.sqlite3 \
  --stop /protected/control/emergency-stop.json \
  --provider-database /var/lib/pilot-worker/provider.sqlite3
```

`plan` reads only the candidate and reports digest, interval, quota and calendar
validity. It does not install authority or infer remaining allowance. `inspect`
requires the existing allowance and current provider schema; it reads no secret,
initializes no store and reports remaining operations, current authority, Stop,
spend suspension and readiness even for expired/exhausted or retired grants.
Status is a bounded snapshot, not permission to send a request. It does not renew
authority or change quotas. Retain original artifacts at distinct paths so they
can still be inspected against historical ledger state.

Only after a separate operator decision approving the new artifact, keep both
Stops asserted and stop inference. Back up the complete consistent state and
archive the original activation/permissions and resolved configuration. Select
the reviewed new grant in the **existing selected allowance file**:

```sh
python -B -m app.ops.private_pilot_ops replace \
  --activation /protected/control/grant-NEW.json \
  --allowance /var/lib/pilot-worker/HISTORICALLY_SELECTED.sqlite3
```

This is the only replacement action. It requires an intact existing allowance,
adds the ordinary ledger extension transactionally, retires older ordinary
grants and refuses reuse of any prepared ordinary ID/digest. It preserves the
original legacy authority and consumed rows and every ordinary reservation.
Repeated invocation fails rather than replenishing. For a genuinely new offline
installation only, `prepare` exclusively creates a new allowance; it refuses an
existing path. Never use `prepare` to recover a lost production ledger or select
a fresh allowance to bypass exhausted state. The historical two-operation
allowance remains exhausted after conversion; a new independently approved
ordinary grant has its own finite quota, with historical operation identities
still excluded. Selecting a new allowance outside this procedure requires a
separate reviewed migration retaining all deduplication history; startup does
not provide one.

## Existing ordinary deployment

Follow [B05D's retained-stack procedure](b05d-research-deployment.md#later-operator-runbook).
Keep the original project/env and all four inputs, in order: base pilot Compose,
`int22.proxy.yaml`, `int22.network.yaml`, `int22.multiturn-20261022-02.yaml`.
Preserve their full effective settings, especially historical allowance selection,
provider.sqlite3, UID/GID separation, credential mounts, network isolation, proxy
flags, image pins, app state, IPC and owner controls. No Compose service definition
changes are required by the grant format.

Prepare a new protected override from the **resolved effective commands**, for
human review. Keep the worker's selected provider and allowance arguments; append
`--activation /run/almost-reasonable-private-pilot/control/grant-NEW.json` (a new
optional CLI argument, whose default remains the historical activation path).
Keep/add its existing `--memory-evolution-permission`, `--typed-memory-permission`
and `--narrative-permission` paths pointing to the newly reviewed permissions.
Use no `--memory-acceptance`. Keep/add the app's `--enable-memory-evolution`,
`--enable-typed-memory`, `--enable-narrative`, `--narrative-permission` and matching
`--narrative-pilot-id`. Preserve all unrelated flags and existing protected mounts;
app-readable permissions need their own existing safe app-owned projection,
never a new mount exposing worker credentials or allowance. Compare the resolved
configuration against all four original inputs before any later deployment.

The existing `app.ops.private_research_config` helper continues to derive the
research app command from this resolved stack, retaining every memory/narrative
flag and the worker command/allowance unchanged. It needs no grant-specific mode:
its tests cover preserving narrative permission flags. Keep its exclusive-create
output rule. Research still requires the separate reviewed participant/source
profile, canonical public catalog, journal, expiry and quotas. The operating
grant cannot activate that authority or approve a new research topic.

No preparation or startup clears either Stop. An ordinary worker with missing,
unprepared, future, retired or observed-expired authority fails before SDK
construction. Exhaustion rejects new operations without contacting the provider.
Actual image upgrade, authority replacement, protected host verification,
independent Stop release, paid calls and browser validation require later operator
authorization; none is performed by this implementation.

## Consumption, expiry and recovery

With evolution plus typed memory, an ordinary turn uses up to three inference
operations: evolution, typed update and reply. Evolution suppresses redundant
extraction. A cued fictional turn uses up to four: those three plus a narrative
proposal. An explicit no-change proposal still costs its operation. Narrative
admission reserves two slots atomically for proposal and reply; the matching
reply claims that hold once. Failure/interruption conservatively consumes the
held capacity. Quota is finite even when the interval is long.

A research decision consumes the already-started inference stages, then consumes
its **separate** catalog reservation and provider accounting. It does not add an
extra inference reply, refund inference capacity or commit a projected fictional
episode. Research has independent expiry, uncertainty and participant/source
binding. Provider tokens and cost estimates remain telemetry, not dollar-budget
enforcement.

Each authorization/release check records a per-grant UTC high-water mark in the
worker allowance. Backward time blocks until the clock catches up; a forward
jump past expiry permanently latches expiry when observed. Restart does not
reset either field. Read-only inspection does not record observations; the next
worker startup/request does. No application can establish elapsed real time if
the trusted host clock was changed during downtime before any worker observed
expiry; correct host time remains an operational prerequisite. An operator with
power to restore every protected artifact/database is outside this boundary.

On expiry or exhaustion, keep state and inspect it. A replacement is a separately
approved new grant in the same ledger, not a rewrite of the exhausted allowance.
It cannot repeat any historical started operation or claim another grant's
narrative hold. Existing workers using retired grants fail admission and commit
checks; restart explicitly selects the reviewed artifact. Lost/uncertain paid
operations retain their identities and are never retried, including after
replacement. Preserve app IPC receipts, provider ledger, source/session/privacy
state and all grant files. Recover missing/corrupt stores from a consistent,
reviewed recovery procedure; never initialize substitutes, delete reservations,
restore capacity from old snapshots or clear Stop as a repair. Stale sockets may
be removed only after verifying their worker is stopped, as in B05D.

## Evidence and remaining validation

`test_b05e_combined.py` runs the connected authenticated HTTP + real AF_UNIX
journeys under both B03E and ordinary authority. The ordinary success journey
advances three hours, restarts inference and application, then recalls generic and typed factual
memory and untrusted fiction. It uses exactly 18 inference operations and one
separately catalog-authorized research operation, with 19 provider ledger rows.
It checks actual requests, tools disabled, factual/fiction separation, public-only
research inputs, durable narrative state, deletion and duplicate-message replay.
The same fixture covers lost responses, interrupted receipt/research/commit paths,
participant binding, controls changed in flight, privacy leakage and Stop.
`test_private_operating_grant.py` covers selected bounds, legacy exhausted state,
replacement, historical operation exclusion, competition, holds, expiry and
clock rollback. Plain-reply IPC regressions change expiry, artifacts, selected
grant or Stop after the paid fake call and verify no model reply is released. Legacy INT22/B03D/B03E regressions remain in the full gate.

All evidence is credential-free with fake providers and synthetic data. It does
not prove live model memory quality, narrative truthfulness, current host mounts,
production timing, a physical iPhone experience or paid-provider behavior.
Those checks remain separately authorized live/human work.
