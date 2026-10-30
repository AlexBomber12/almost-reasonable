# Release readiness and ordinary launch procedure

This procedure prepares a launch; it does not assert that a launch happened.
Repository tests use fake SDK/HTTP providers. They establish implementation
behavior, not real-model judgment, two-human participation, current Meta grants
or availability of the deployed system. The behavioral contract v0.7 and
`acceptance/scenarios.json` remain normative.

## Release record and decision

For each candidate, the operator retains one protected, redacted release record
outside the repository. Record the exact clean Git HEAD/source digest, image
digests and resolved existing Compose commands; actual mounted database identities
and schemas; application/worker prompt and protocol versions; nonsecret runtime
configuration/model/account inventories and their SHA-256 digests; current-HEAD
CI artifact/digest, all required checks, Codex review and resolved findings;
the frozen real-acceptance plan, approved complete evidence inventory and report;
recovery rehearsal receipts; and explicit human authorization for the next stage.
Never include credentials, participant text or raw authentication material.

Changing HEAD, source, model, account or reviewed configuration invalidates the
old acceptance binding. Reassess the new candidate; an old green PR or historical
pilot snapshot cannot certify it. An accepted attestation report itself always
has `rollout_authorized: false`; deployment authorization is a separate decision.

| Area | Implemented evidence path | Still required before a complete-MVP public launch |
|---|---|---|
| M1: one identity, distinct private histories | [ordinary shared influence](ordinary-shared-intelligence.md), [public/private provenance](public-private-intellectual-provenance.md), real local IPC/SDK fixture journeys | Two independently authenticated **humans**, observed isolation/continuity/usefulness and privacy acceptance on the candidate |
| M2: initiative, rhythm and inactivity | [finite window](b11c-public-operating-window.md), [rhythm and controls](agent-rhythm-owner-controls.md) | Authorized finite real-model observations, valid silence and cadence across restart; no publication or opinion-change quotas |
| M3: own research and retained influence | [agent questions](agent-research-questions.md), [research continuity](research-intellectual-continuity.md) | Approved hosted tool/model/billing scope, multilingual actual search, semantic citation review, Russian conclusions and withdrawal |
| M4: public conversations | [finite publication grant](finite-public-grant.md), [read probe](int27-threads-readonly-probe.md), [publication evidence](b11b-threads-publication.md) | Actual account/scopes/access level, authorized publish/readback, known incoming replies/mentions, Stop and unknown-outcome observation |
| M5: durable development and mission freedom | [intellectual state](agent-intellectual-state.md), [free mission](free-bounded-mission.md), current-ledger restore | Longitudinal human assessment, source/interpretation distinction, independently admitted later mission phase, actual restore/privacy rehearsal |
| M6: ordinary private UI and owner control | [owner pilot](int26-owner-pilot.md), complete worker registry and shared application Stop | Real separate owner authentication, all configured worker Stops and freshness, finite authority, spending suspension and participant privacy controls |
| Full SCN-01–12 and G/H/M | [real acceptance protocol](real-acceptance.md), current-HEAD deterministic CI | Preregistered calibrated rubric and all trials, all scenario requirements and contrary observations; missing means NOT_RUN/BLOCKED |
| Ordinary deployment | Existing reviewed host/Compose configuration and explicit app/worker composition | Host inventory, compatible pinned images, consistent complete backup and rehearsed recovery, current external observations and explicit stage authorization |

The source inventory describes implementation, not a production status inventory.
In the absence of fresh operator receipts, every live/behavioral prerequisite above
is `NOT_RUN` and complete MVP/public launch is `BLOCKED`. No metric, test count,
staged simulation or disabled flag resolves those prerequisites.

## 1. Freeze and inspect the host while stopped

Do this only after the operator authorizes host/deployment work. Use the ordinary
server checkout and retained project names, protected env files and original
Compose inputs in their exact order; do not replace the ordinary stack with a
new pilot domain or reconstructed command. The historical handoff's server paths,
versions, usage and running states are observations from an earlier date.
Resolve the actual configuration locally without publishing it or its secrets.

Assert common application Stop and each native worker Stop. Require separate fresh
acknowledgements for private inference, public model and every research worker.
An omitted worker is outside registry coverage; a stale/unavailable target is
unconfirmed. Keep the emergency action available until all targets are confirmed.
Stop prevents new work and result release at guarded boundaries; it cannot undo
an accepted in-flight provider request or public post.

Inventory exact existing application EventStore (currently schema 14), Threads
store (schema 10), public journals, scheduler, mission, authoritative privacy
ledgers, owner and participant auth/session stores, provider usage/spend stores,
all allowance/grant/authority journals, Stop files, pending outbox/UNKNOWN records,
and protected permission/configuration files. Verify actual ownership, modes,
numeric service UIDs, socket peers, restricted mounts and network boundaries.
Do not initialize substitute missing databases, recursively change existing
ownership, reset usage/counters or infer availability from a previous receipt.

Coordinate app and workers from the same reviewed compatible release. The
intellectual prompt is `agent-intellectual-state-prompt-v4`; projection remains
`agent-intellectual-state-v1`. Free intellectual mission prompt is
`threads-public-free-mission-intellectual-v2`. Existing snapshots remain historical;
an older implementation rejects unsupported receipt/source shapes. Do not combine
new private/public receipts with an old worker or roll back code against newer
stores merely because a health endpoint responds.

## 2. Preserve recoverable state and test configuration

Before changing images or mounts, preserve the complete stopped state with the
host's reviewed backup method, not loose copies of active SQLite main files.
Include committed WAL state and coherent auth, owner controls, provider spending,
consumption/retired grant identities, scheduler, outbox and mission state. Protect
backups under the same privacy/retention rules as their sources. Keep credentials
in their existing secret mechanism, never in this repository or release report.

The [EventStore backup tool](eventstore-backup-operations.md) includes committed
WAL and can reconcile a candidate with the **current authoritative** privacy
ledger into quarantine. It covers EventStore only: it is neither an atomic
multi-store backup nor a database-switch operation. A verified quarantine cannot
be routed to participants until coordinated recovery is approved. An old ledger,
provider allowance or session snapshot must never resurrect erased data, spending
capacity or revoked sessions. Without a rehearsed coherent restore, recovery
remains BLOCKED.

Prepare protected configuration without starting services or issuing authority.
Retain existing flags rather than copying a small example command over them:

* Private ordinary influence requires shared proposals, explicit ordinary
  abstractions, trusted audiences, finite worker permission and privacy controls.
  Narrative retains its own matching finite permission. See the linked protocol;
  enabling an app flag cannot supply worker authority.
* Public shared influence uses the optional protected source configuration below.
  Its private ledger is distinct from the public runtime ledger. Only ordinary
  records whose audience includes the exact public account may cross.
* Private public-state retrieval uses `--public-intellectual-config`, the same
  private EventStore/ledger as its receiving runtime and the actual common owner
  Stop directory. Public-only ancestors do not authorize private B retrieval.
* Agent questions require matching separately reviewed application/worker v2
  profiles, finite aggregate/per-question capacity, fixed hosted tool/provider
  configuration and native Stop. A proposed question has no authority itself.
* Free mission needs both evolution and free-mode flags. A proposal changes no
  active mission; a later distinct autonomous operation confirms or rejects it.
* Agent cadence defaults off and cannot shorten below the owner's floor or
  enlarge any call, research, posting or spending allowance.
* Autonomous publication defaults off. Its flag, finite reviewed window and
  separately issued matching grant are all required; enabling a flag issues no
  grant and approves no unrelated pending draft.

Protected public shared-source configuration is selected by the optional
`shared_intellectual_config` field in the existing public operator configuration.
It is an app-owned mode-0400 JSON file in a mode-0700 directory, with exact fields:

```json
{
  "version": "public-shared-source-v1",
  "account": "<exact reviewed account_ref>",
  "event_store_path": "/absolute/existing/private/EventStore.sqlite",
  "ledger_path": "/absolute/existing/private/privacy-ledger.sqlite",
  "owner_directory": "/absolute/existing/common/owner-directory"
}
```

This is trusted **application** access, including necessary privacy reconciliation,
not permission for the credential-holding public worker to read private stores.
The invoking application identity must already own the private stores and satisfy
their existing security checks. If the retained deployment isolates them under
different UIDs/mounts, stop and review a compatible composition; do not weaken
ownership or copy private databases into worker containers. No configuration means
no shared source. Wrong account/owner, aliases, missing/foreign schema, unsafe
files or blocked privacy reconciliation reject startup before Threads credentials.

Inspect inert entrypoints without provider contact:

```bash
python -m app.ops.int24_threads_runtime readiness
python -m app.ops.int24_threads_runtime window-plan --config /absolute/protected/operator.json
python -m app.ops.int27_threads_readonly_probe
python -m app.ops.real_acceptance --help
```

`readiness` remains disabled; `window-plan` previews bounded maximum work and does
not prove current quota availability or grant issuance. It is not a full host or
database readiness validator. Check actual deployment mounts/bindings separately.

## 3. Authorize and observe restricted stages

Before paid work, verify the reviewed OpenAI project/organization, model and hosted
tool, current project Hard Spend Limit (contract initially USD 10/month), and native
provider/allowance records. Local estimates and token telemetry are not an exact
invoice cap; accounting/enforcement may lag. Do not change the model or budget
because of a development recommendation. Keep finite call/step/deadline/expiry
limits and stop on confirmed spend suspension; no automatic budget reset.

Before real participant use, approve enrollment/disclosure/retention and verify
two independently authenticated humans plus separate owner authentication. Run
the frozen acceptance plan on the reviewed candidate. Collect permitted private
continuity, sharing/narrative, research and owner/privacy observations; neither a
fixed persona nor agreement with the owner is required. Retain all attempts and
contrary evidence with uncertainty. Real calls/enrollment require explicit scope.

Before any public write, independently verify actual Threads user identity, token
validity, app access level, selected scopes and AI/fiction disclosure. An account
recovery or empty read page proves neither permission to publish nor inbound
coverage. Use the separately authorized bounded read-probe procedure first. An
empty profile supports mention-only reads; do not create a filler post for a root.
Only a separately authorized publication test can establish actual publish and
ownership/readback behavior. Recheck current provider/platform requirements before
activation; historical documentation snapshots are not current permission grants.

Begin a restricted public window only after review of exact account, mission,
remaining finite budgets, backlog and Stop. Follow [finite grant issuance and
revocation](finite-public-grant.md) and [bounded window](b11c-public-operating-window.md)
commands; they require `--activate-live`. Do not issue them from this preparation
task. Within one explicitly issued autonomous grant, no per-draft approval is
needed for eligible own drafts. Expiry, revocation, privacy, Stop, source validity,
mission and request budgets remain effective. Do not silently renew a window or
reuse a grant ID to replenish consumption. `no_action` and declining Threads are
valid; trial counts do not impose public activity quotas.

Observe real known inbound content, ownership/readback and restart under the
restricted grant. Record operation IDs, timestamps, request counts, source and
permission checks, actual outcome and uncertainty without copying private payloads
into logs. Report real acceptance through the frozen hash-approved import protocol.
Do not describe a restricted test as complete MVP or ordinary public launch.

## 4. Incident, recovery and rollback

On suspected leakage, incorrect account, spend suspension, contract drift,
unresolved SENDING/UNKNOWN or inconsistent authority: assert common application
Stop and all native Stops; revoke the active public grant through authenticated
controls when accessible. Retain redacted audit/correlation and current consumption.
Inspect each target; retry unconfirmed Stop targets without interpreting partial
acknowledgement as full shutdown. Do not clear a Stop to make a probe succeed.

Never redispatch UNKNOWN/SENDING blindly, create a replacement post, reset the
account pause or mark uncertain publication as failed/no-effect. Use the existing
separately authenticated reconciliation and recovery paths with actual account,
ownership/parent and operation evidence. Their explicit duplicate-risk approval
cannot be supplied by the autonomous grant. A failed pre-publication attempt may
still consume allowance; do not refund slots or recreate a retired grant.

Rollback is not `git checkout` plus an old database. Keep all Stops asserted,
establish which schema/protocol versions the last known compatible image supports,
and preserve current privacy controls, provider usage, consumed/retired grants,
revocations and authentication/session state. Reconcile a restored candidate
against the current ledger in quarantine, verify graph closure and private/public
binding, and review the coordinated database/image switch separately. If no
compatible safe downgrade exists, remain stopped and repair forward. The provided
backup CLI cannot enact that switch. Never restore authority counters or old
sessions to obtain service availability.

Resume only after the cause is resolved, candidate checks/review and relevant
real observations are current, and the operator explicitly authorizes the
specific next stage. Clearing application Stop does not clear worker Stops,
provider suspension or issue new authority. Full launch requires fresh complete
M1–M6/SCN-01–12 evidence and an explicit launch decision; missing evidence remains
BLOCKED even when every implementation PR is merged.
